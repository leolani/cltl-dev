"""The control surface both runner tiers implement, and the event probe.

Tests are written against :class:`Runner`. What differs between tiers is
deliberately narrow: how a stimulus is injected (an event publish and an HTTP
call are shared; a hardware-shaped audio source is not) and what a payload
assertion actually proves (tier 2 round-trips through marshal/unmarshal, tier 1
hands the live object to the handler).
"""
import logging
import threading
import time
from contextlib import nullcontext
from typing import Callable, List, Optional, Protocol, Sequence

from cltl.combot.infra.event import Event, EventBus

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10.0


class ProbeTimeout(AssertionError):
    """No matching event arrived in time. Carries what *did* arrive."""


class EventProbe:
    """Records events off the bus so tests can assert on them.

    The test process is a first-class participant on the event bus in both
    tiers, which is what lets a slice assertion be written once. In tier 1 the
    bus dispatches synchronously on the publishing thread, so recording is
    lossless the moment :meth:`subscribe` returns. Tier 2 will need a readiness
    handshake here, because ``KombuEventBus.subscribe`` returns before its queue
    is bound and silently drops anything published in the interim.
    """

    def __init__(self, event_bus: EventBus, default_timeout: float = DEFAULT_TIMEOUT,
                 readiness: Optional[Callable[[Sequence[str]], object]] = None):
        self._event_bus = event_bus
        self._default_timeout = default_timeout
        self._readiness = readiness
        self._lock = threading.Lock()
        self._events: List[Event] = []
        self._arrived = threading.Event()
        self._topics: List[str] = []

    def subscribe(self, *topics: str) -> "EventProbe":
        """Subscribe, and do not return until the subscription can receive.

        In tier 1 that is the moment ``subscribe`` returns. In tier 2 it is not:
        ``KombuEventBus.subscribe`` constructs a ``ConsumerMixin`` thread and
        calls ``Thread.start()``, so the queue is declared and bound to the
        exchange somewhere in that thread's future. Anything published before
        the binding exists is routed to no queue and dropped — silently, because
        that is what a topic exchange does with an unroutable message. A probe
        that misses the first event of a pipeline reports a timeout listing no
        events at all, which reads exactly like a module that never ran.

        The *readiness* hook closes that window. It is a context manager around
        the subscribe rather than a callback after it, because the only way to
        recognise *this* subscription is to compare the broker's state before
        and after: the modules are subscribed to the same topics, so a check
        that merely asks "is anything bound to cltl.topic.scenario" is answered
        yes by somebody else's queue and returns before ours exists.
        ``ComposeRunner`` supplies one; tier 1 needs none.
        """
        pending = tuple(topic for topic in topics if topic not in self._topics)
        if not pending:
            return self

        readiness = self._readiness(pending) if self._readiness else nullcontext()
        with readiness:
            for topic in pending:
                self._event_bus.subscribe(topic, self._record)
                self._topics.append(topic)
                logger.debug("Probe subscribed to %s", topic)

        return self

    def _record(self, event: Event) -> None:
        with self._lock:
            self._events.append(event)
        self._arrived.set()

    def events(self, topic: Optional[str] = None) -> List[Event]:
        with self._lock:
            return [event for event in self._events
                    if topic is None or event.metadata.topic == topic]

    def clear(self) -> None:
        with self._lock:
            self._events.clear()
        self._arrived.clear()

    def await_event(self, topic: str,
                    predicate: Optional[Callable[[Event], bool]] = None,
                    timeout: Optional[float] = None) -> Event:
        """Block until an event on *topic* satisfies *predicate*, or fail.

        On timeout the error lists every event the probe saw, grouped by topic.
        Without that, a failure here says only "nothing arrived", which is the
        least useful thing to know when a multi-module pipeline goes quiet.
        """
        timeout = self._default_timeout if timeout is None else timeout
        end = time.monotonic() + timeout
        while True:
            for event in self.events(topic):
                if predicate is None or predicate(event):
                    return event

            remaining = end - time.monotonic()
            if remaining <= 0:
                raise ProbeTimeout(self._timeout_message(topic, predicate, timeout))

            self._arrived.wait(min(remaining, 0.05))
            self._arrived.clear()

    def _timeout_message(self, topic, predicate, timeout) -> str:
        seen = self.events()
        lines = [
            f"No event on {topic!r}"
            + (" matching the predicate" if predicate else "")
            + f" within {timeout}s.",
            f"Probe subscribed to: {self._topics}",
        ]
        if not seen:
            lines.append("No events were observed at all.")
        else:
            lines.append(f"{len(seen)} event(s) observed:")
            for event in seen:
                lines.append(f"  [{event.metadata.topic}] {_describe(event.payload)}")
        return "\n".join(lines)


def _describe(payload) -> str:
    signal = getattr(payload, "signal", None)
    text = getattr(signal, "text", None)
    if text is not None:
        return f"{type(payload).__name__}(text={text!r})"
    return f"{type(payload).__name__}({payload!r})"[:200]


class Runner(Protocol):
    """What a test may rely on, regardless of tier."""

    def url(self, module_key: str) -> str:
        """Host-reachable base URL for a module's HTTP mount."""

    @property
    def probe(self) -> EventProbe:
        ...

    @property
    def event_bus(self) -> EventBus:
        ...

    @property
    def topics(self) -> Sequence[str]:
        ...
