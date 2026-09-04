"""Tier 2: the text conversation, across containers and a real broker.

Same steps as ``tests/pipelines/test_text_pipeline.py`` — the ``Conversation``
driver is shared — but the assertions mean something different here. Every event
between the chat UI, cltl-context and cltl-eliza is serialised to JSON, routed
through RabbitMQ's topic exchange and reconstructed on the other side, and each
module holds its own resource manager in its own process.

The last class is a defect this pair of tiers found together: the behaviour is
correct in process and wrong in containers, which is the exact shape of bug
neither an in-process test nor an end-to-end smoke test would have caught alone.
"""
import time

import pytest

from cltl_integration.drivers.conversation import (GREETING, TEXT_OUT, TOPICS,
                                                   Conversation)
from cltl_integration.topology import TEXT_PIPELINE

pytestmark = pytest.mark.compose


@pytest.fixture
def conversation(compose):
    runner = compose(TEXT_PIPELINE)
    runner.probe.subscribe(*TOPICS)

    return Conversation(runner)


class TestConversation:
    def test_the_handshake_completes_across_containers(self, conversation):
        """Four processes and a broker, in place of four objects and a list."""
        conversation.open().confirm()

        assert conversation.scenario_id
        assert conversation.client.current()["scenario_id"] == conversation.scenario_id

    def test_utterance_gets_a_reply(self, conversation):
        conversation.open().confirm()

        conversation.say("I feel very anxious today")

        assert conversation.replies(), "Eliza never answered"

    def test_the_reply_survives_serialization_intact(self, conversation):
        """What tier 1 cannot test, because nothing is serialised there.

        The payload asserted on here was marshalled by cltl-eliza, routed by
        RabbitMQ and unmarshalled by this process. A field that does not survive
        that round trip is a field the chat UI never sees either.
        """
        conversation.open().confirm()
        conversation.runner.probe.clear()

        conversation.say("My mother never listens to me")

        reply = conversation.runner.probe.await_event(
            TEXT_OUT, lambda event: bool(event.payload.signal.text), timeout=30)

        assert reply.payload.signal.text
        assert reply.payload.signal.time.container_id == conversation.scenario_id
        assert "".join(reply.payload.signal.seq) == reply.payload.signal.text


class TestDuplicateDelivery:
    """The tier-2 half of the duplicate-intention defect.

    ``tests/slices/test_intention_routing.py`` pins the cause: ElizaService is
    subscribed to the intention topic twice. Whether the duplicate survives to
    be *processed* is a race against the worker's 1-slot OVERWRITE buffer, and
    it is lost far more often here than in-process, because two AMQP deliveries
    arrive milliseconds apart rather than back to back on one thread. The
    symptom is a blank line from the agent in the chat UI.

    Neither of those is asserted here, because neither is deterministic. What is
    asserted is the path that stays correct regardless — so that a future fix to
    the duplicate cannot silently break the greeting instead.
    """

    SETTLE = 3.0

    def test_the_greeting_is_offered_once(self, conversation):
        """``InitService`` sets a timeout before greeting and checks it after.

        That guard is why the duplicate is invisible until after the handover:
        the second delivery falls through it. Remove the guard while fixing the
        duplicate and the user is greeted twice instead.
        """
        conversation.open()
        time.sleep(self.SETTLE)

        greetings = [event for event in conversation.runner.probe.events(TEXT_OUT)
                     if GREETING in event.payload.signal.text]

        assert len(greetings) == 1, f"{len(greetings)} greetings were published"

    def test_the_user_is_never_shown_an_unattributed_message(self, conversation):
        """Every message the chat UI holds is attributed to somebody.

        A blank *body* from the agent is the race above; a blank *speaker* would
        be a serialization fault, and that is deterministic.
        """
        conversation.open().confirm()
        time.sleep(self.SETTLE)

        history = conversation.client.fetch_all(conversation.chat_id)

        agent = [utterance for utterance in history if utterance["speaker"]]
        assert agent, f"no agent utterance at all in {history}"
        assert all(utterance["text"] is not None for utterance in agent)
