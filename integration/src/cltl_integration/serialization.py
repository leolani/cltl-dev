"""Event serialization setup shared by both runner tiers.

Every component's ``src/main.py`` opens with the same three
``register_type_var`` calls before anything marshals an :class:`Event`. Without
them ``marshal(event, cls=Event)`` raises
``TypeError: PAYLOAD is not a dataclass and cannot be turned into one``, because
emissor cannot resolve the generic type variables.

The harness needs the same setup in both tiers: tier 2's probe joins RabbitMQ and
so must serialise exactly as the containers do, and tier 1 marshals whenever a
test asserts on a persisted signal. Keeping it here means one definition instead
of one per test.
"""
import threading

from cltl.combot.event.emissor import MEN, SIG
from cltl.combot.infra.event.api import PAYLOAD, Event
from emissor.representation.util import marshal, register_type_var, unmarshal

_registered = False
_lock = threading.Lock()


def register_event_types() -> None:
    """Register the generic type variables emissor needs to (un)marshal events.

    Idempotent: emissor keeps a process-global registry, and the harness resets
    DI state between topologies without touching it.
    """
    global _registered
    with _lock:
        if _registered:
            return
        for type_var in (PAYLOAD, SIG, MEN):
            register_type_var(type_var)
        _registered = True


def serializer(event: Event) -> str:
    register_event_types()
    return marshal(event, cls=Event)


def deserializer(raw: str) -> Event:
    register_event_types()
    return unmarshal(raw, cls=Event)
