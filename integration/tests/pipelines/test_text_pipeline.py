"""Pipeline: chat UI -> context/BDI -> eliza -> chat UI.

The slice tests each pin one boundary with everything else disabled. This one
runs the three modules together with **intention gating on**, which is how the
application actually runs and which nothing else in the suite covers.

That gating is the reason a pipeline test earns its place here rather than being
the sum of its slices. ``ElizaService``'s topic worker starts inactive and stays
inactive until ``BDIService`` publishes the ``eliza`` intention, which only
happens once ``InitService`` has seen a scenario, offered its greeting and had it
accepted. Every one of those steps is a separate module reacting to the last
one's event. Get the ordering wrong — or drop one event — and the system comes
up perfectly healthy and answers nothing, with no error anywhere.

So the assertions here are as much about *when* Eliza is silent as about when it
speaks.
"""
import pytest

from cltl_integration.drivers.conversation import (GREETING, TEXT_IN, TEXT_OUT,
                                                   TOPICS, Conversation)
from cltl_integration.topology import TEXT_PIPELINE


@pytest.fixture
def conversation(inprocess):
    runner = inprocess(TEXT_PIPELINE)
    runner.probe.subscribe(*TOPICS)

    return Conversation(runner)


class TestOpening:
    def test_init_intention_brings_the_chat_ui_online(self, conversation):
        """The chat UI cannot post at all until a scenario reaches it."""
        conversation.open()

        assert conversation.client.current()["scenario_id"] == conversation.scenario_id

    def test_the_greeting_reaches_the_user(self, conversation):
        conversation.open()

        history = conversation.client.fetch_all(conversation.chat_id)

        assert any(GREETING in utterance["text"] for utterance in history), (
            f"the init greeting never reached the chat: {history}")


class TestGating:
    def test_eliza_is_silent_before_the_handshake_completes(self, conversation):
        """The failure this pipeline exists to catch, in its positive form.

        Eliza is running, subscribed and correctly configured here. It answers
        nothing because ``BDIService`` has not handed it the conversation yet.
        A regression that activates it early is just as wrong as one that never
        activates it — it would answer over the top of the init dialogue.
        """
        conversation.open()
        conversation.runner.probe.clear()

        conversation.say("I feel very anxious today")

        conversation.runner.probe.await_event(
            TEXT_IN, lambda event: "anxious" in event.payload.signal.text)
        with pytest.raises(AssertionError):
            conversation.runner.probe.await_event(TEXT_OUT, timeout=2.0)

    def test_confirmation_hands_the_conversation_to_eliza(self, conversation):
        """On activation ElizaService opens with a line of its own."""
        conversation.open().confirm()

        opening = conversation.runner.probe.await_event(
            TEXT_OUT, lambda event: GREETING not in event.payload.signal.text)

        assert opening.payload.signal.text


class TestConversation:
    def test_utterance_gets_a_reply_over_http(self, conversation):
        conversation.open().confirm()

        conversation.say("I feel very anxious today")

        assert conversation.replies(), "Eliza never answered"

    def test_reply_carries_the_scenario_of_the_conversation(self, conversation):
        """cltl-emissor-data files every signal by scenario; losing it loses the turn."""
        conversation.open().confirm()
        conversation.runner.probe.clear()

        conversation.say("My mother never listens to me")

        reply = conversation.runner.probe.await_event(TEXT_OUT)

        assert reply.payload.signal.time.container_id == conversation.scenario_id

    def test_the_conversation_continues(self, conversation):
        """A second turn, to catch a pipeline that only ever handles one.

        Asserted on distinct events rather than distinct text: Eliza repeats
        itself often enough that comparing transcripts would fail for the wrong
        reason.
        """
        conversation.open().confirm()
        conversation.runner.probe.clear()

        conversation.say("I feel very anxious today")
        first = conversation.runner.probe.await_event(TEXT_OUT)
        conversation.say("It has been going on for weeks")
        second = conversation.runner.probe.await_event(
            TEXT_OUT, lambda event: event.id != first.id)

        assert second.payload.signal.text
