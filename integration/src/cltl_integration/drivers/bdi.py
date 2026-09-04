"""Drive the BDI loop from outside, the way ``app/py-app/app.py`` does.

Nothing inside the platform publishes the first intention. The application does
it once at startup — see ``app/py-app/app.py``, which sleeps a second after
``container.start()`` and then puts ``IntentionEvent(["init"])`` on the bus.
Anything that wants a running conversation has to do the same.

That sleep is not decoration. ``TopicWorker._check_intention`` runs on the
publishing thread, so a worker gated on ``init`` is activated synchronously by
the publish — but ``ContextService`` reacts to the same event on *its* worker
thread and publishes ``ScenarioStarted`` from there. Publishing the intention
before every worker has finished subscribing loses the scenario event, and a
service that misses ``ScenarioStarted`` waits forever with no error.
"""
from typing import Sequence

from cltl.combot.event.bdi import DesireEvent, Intention, IntentionEvent
from cltl.combot.infra.event import Event, EventBus


def intention(*labels: str) -> IntentionEvent:
    return IntentionEvent([Intention(label, None) for label in labels])


def desire(*achieved: str) -> DesireEvent:
    return DesireEvent(list(achieved))


def publish_intention(event_bus: EventBus, intention_topic: str, *labels: str) -> None:
    event_bus.publish(intention_topic, Event.for_payload(intention(*labels)))


def labels(event: Event) -> set:
    """The intention labels carried by an ``IntentionEvent``."""
    return {item.label for item in event.payload.intentions}


def is_desire(event: Event, *achieved: str) -> bool:
    """Predicate for ``EventProbe.await_event`` on the desire topic."""
    if not isinstance(event.payload, DesireEvent):
        return False

    return all(item in event.payload.achieved for item in achieved)


def is_intention(event: Event, *expected: Sequence[str]) -> bool:
    """Predicate for ``EventProbe.await_event`` on the intention topic."""
    if not hasattr(event.payload, "intentions"):
        return False

    return all(item in labels(event) for item in expected)
