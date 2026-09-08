"""How intention gating is wired onto the event bus, for every module at once.

Intention gating is the mechanism that decides which service is allowed to act
at any moment: ``TopicWorker`` starts inactive when it is given ``intentions``
and flips on an ``IntentionEvent`` carrying one of them. Every service in the
conversation depends on it, and a fault in the wiring does not raise — it makes
a service answer twice, or not at all.

This looks at the subscriber lists directly rather than at behaviour. The
consequences of the defect below are timing-dependent (see the reason on the
xfail), so an assertion on what reaches the bus is the only formulation that
fails the same way every run.
"""
from collections import Counter

import pytest
from cltl.combot.infra.event import Event
from cltl.combot.infra.event.memory import SynchronousEventBus
from cltl.combot.infra.topic_worker import TopicWorker

from cltl_integration.drivers.bdi import intention
from cltl_integration.topology import TEXT_PIPELINE

INTENTION_TOPIC = "cltl.topic.intention"
TEXT_IN = "cltl.topic.text_in"


def _subscribers(runner):
    """Map topic -> Counter of the worker names subscribed to it.

    ``SynchronousEventBus`` keeps a plain list of handlers per topic, and every
    ``TopicWorker`` subscribes its own bound ``__accept_event``, so the worker's
    name identifies the subscriber.
    """
    counts = {}
    for topic, handlers in runner.event_bus._handlers.items():
        names = Counter()
        for handler in handlers:
            owner = getattr(handler, "__self__", None)
            names[getattr(owner, "name", type(owner).__name__)] += 1
        counts[topic] = names

    return counts


@pytest.fixture
def pipeline(inprocess):
    return inprocess(TEXT_PIPELINE)


class TestSubscriptions:
    def test_every_gated_service_listens_to_the_intention_topic(self, pipeline):
        """Without this subscription a gated worker never activates at all."""
        subscribers = _subscribers(pipeline)["cltl.topic.intention"]

        assert {"ElizaService", "KeywordService", "InitService"} <= set(subscribers)

    @pytest.mark.xfail(
        strict=True,
        reason="TopicWorker.run() subscribes the intention topic twice whenever "
               "a service also lists it among its `topics`: the `for topic in "
               "self._topics` loop covers it, and the following `if "
               "self._intention_topic` subscribes the same bound method again. "
               "ElizaService passes [input_topic, intention_topic] as its topics, "
               "so it is delivered every IntentionEvent twice and processes both "
               "— its container log shows each activation logged twice. The "
               "visible consequence is a blank message from the agent: "
               "ElizaService._process answers an activation with "
               "Eliza.respond(None) and publishes the result without the `if "
               "response:` guard its other branch has, and Eliza.respond returns "
               "its greeting only the first time. Whether the second delivery "
               "survives is a race — the worker's 1-slot OVERWRITE buffer often "
               "swallows it, more often in-process than over AMQP — which is why "
               "this test asserts on the subscription rather than the symptom. "
               "Fix: drop the intention topic from the loop, or from "
               "ElizaService's topics list.")
    def test_no_service_subscribes_a_topic_twice(self, pipeline):
        duplicates = {topic: [name for name, count in names.items() if count > 1]
                      for topic, names in _subscribers(pipeline).items()}
        duplicates = {topic: names for topic, names in duplicates.items() if names}

        assert not duplicates, f"duplicate subscriptions: {duplicates}"


class TestTenantScopedGating:
    """Whether a module shared across tenants can be gated at all.

    Not part of the pipeline above: this drives one ``TopicWorker`` directly,
    because the question is about the worker and not about how the modules are
    wired to each other. It is here because it is the reason
    ``config/topologies/multitenant_server.config`` runs the shared cltl-eliza
    ungated, and because it costs milliseconds where reproducing it through the
    real deployment costs a second cltl-eliza and three more minutes.

    See tests/compose/test_multitenant.py for the deployment this constrains.
    """

    @staticmethod
    def _worker(event_bus, processed):
        return TopicWorker([TEXT_IN, INTENTION_TOPIC], event_bus,
                           intentions=["eliza"], intention_topic=INTENTION_TOPIC,
                           processor=processed.append, name="Shared")

    @staticmethod
    def _publish(event_bus, topic, payload, tenant):
        """Publish as *tenant* would.

        ``SynchronousEventBus`` stamps ``"local"`` only on an event that has no
        tenant yet (cltl-combot/src/cltl/combot/infra/event/memory.py:28), so a
        tenant set here survives to the worker exactly as it would after a
        tenanted ``KombuEventBus`` had stamped it and AMQP had carried it.
        """
        event_bus.publish(topic, Event.with_tenant(Event.for_payload(payload), tenant))

    @pytest.mark.xfail(
        strict=True,
        reason="TopicWorker._check_intention "
               "(cltl-combot/src/cltl/combot/infra/topic_worker.py:222-242) keeps ONE "
               "active flag for the worker and never reads event.metadata.tenant, so "
               "a module deployed once and shared by several tenants is gated by "
               "whichever tenant published an intention last. Tenant B returning to "
               "its `init` intention deactivates the shared module for tenant A, "
               "mid-conversation and with nothing logged on A's side; A's next "
               "utterance is simply never answered. This is why the shared "
               "cltl-eliza in config/topologies/multitenant_server.config has to run "
               "ungated, which in turn is why its topic_intention is emptied — see "
               "that file's header. Fix: key the active flag by "
               "event.metadata.tenant, defaulting to the untenanted flag.")
    def test_one_tenant_does_not_gate_another(self):
        event_bus = SynchronousEventBus()
        processed = []
        worker = self._worker(event_bus, processed)
        worker.start().wait()
        try:
            # Tenant A hands the conversation to the shared module...
            self._publish(event_bus, INTENTION_TOPIC, intention("eliza"), "tenant-a")
            # ...and tenant B, independently, goes back to its own init phase.
            self._publish(event_bus, INTENTION_TOPIC, intention("init"), "tenant-b")

            self._publish(event_bus, TEXT_IN, object(), "tenant-a")

            assert [event for event in processed if event.metadata.topic == TEXT_IN], (
                "tenant-b's intention deactivated the shared worker for tenant-a")
        finally:
            worker.stop()
            worker.await_stop()
