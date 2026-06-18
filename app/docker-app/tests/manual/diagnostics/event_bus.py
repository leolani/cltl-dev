"""Thin publish/subscribe wrapper around the KombuEventBus for use in diagnostics."""
import logging
import threading
import time
from contextlib import contextmanager
from typing import Iterator, List

from cltl.combot.event.emissor import SIG, MEN
from cltl.combot.infra.event.api import Event, PAYLOAD
from cltl.combot.infra.event.kombu import KombuEventBus
from emissor.representation.util import marshal, unmarshal, register_type_var

logger = logging.getLogger(__name__)

register_type_var(PAYLOAD)
register_type_var(SIG)
register_type_var(MEN)


def _serializer(obj):
    return marshal(obj, cls=Event)


def _deserializer(obj):
    return unmarshal(obj, cls=Event)


def create_event_bus(config_manager) -> KombuEventBus:
    """Create a KombuEventBus from *config_manager* using the cltl-json serializer."""
    from cltl.combot.infra.event import kombu as kombu_module
    from kombu.serialization import register

    kombu_module._current_serializer_func = _serializer
    kombu_module._current_deserializer_func = _deserializer

    register(
        'cltl-json', _serializer, _deserializer,
        content_type='application/json', content_encoding='utf-8',
    )

    return KombuEventBus('cltl-json', config_manager)


@contextmanager
def collect_events(bus: KombuEventBus, topic: str, timeout: float) -> Iterator[List[Event]]:
    """Subscribe to *topic* and collect arriving events for *timeout* seconds.

    Yields the list so callers can inspect it after the context manager exits.
    Usage::

        with collect_events(bus, "cltl.topic.vad", timeout=10.0) as events:
            bus.publish("cltl.topic.microphone", my_event)
        assert events  # filled after the ``with`` block
    """
    collected: List[Event] = []
    done = threading.Event()

    def handler(event: Event) -> None:
        collected.append(event)
        done.set()

    bus.subscribe(topic, handler)
    # Give the consumer thread time to connect and declare its queue before
    # the caller publishes — without this the publish races the AMQP handshake
    # and the response arrives before the subscriber is ready.
    time.sleep(0.5)
    try:
        yield collected
        done.wait(timeout=timeout)
    finally:
        bus.unsubscribe(topic, handler)
