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

from cltl_integration.topology import TEXT_PIPELINE


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
