"""Slice: cltl-chat-ui <-> cltl-eliza, over HTTP and `cltl.topic.text_*`.

Exercises the full round trip a user sees:

    HTTP POST /chatui/chat/<id>  ->  cltl.topic.text_in
                                 ->  ElizaService
                                 ->  cltl.topic.text_out
                                 ->  HTTP GET /chatui/chat/<id>

cltl-context is deliberately not in this topology. ChatUiService only needs a
ScenarioStarted event to function, so the test publishes one itself rather than
dragging in the BDI handshake it is not testing. See
``cltl_integration.drivers.scenario``.
"""
import pytest
import requests

from cltl_integration.drivers.chat import ChatClient
from cltl_integration.drivers.scenario import start_scenario
from cltl_integration.topology import ELIZA_CHATUI

TEXT_IN = "cltl.topic.text_in"
TEXT_OUT = "cltl.topic.text_out"
SCENARIO_TOPIC = "cltl.topic.scenario"


@pytest.fixture
def chat(inprocess):
    """A started topology plus a ChatClient with an active scenario."""
    runner = inprocess(ELIZA_CHATUI)
    runner.probe.subscribe(TEXT_IN, TEXT_OUT)

    scenario = start_scenario(runner.event_bus, SCENARIO_TOPIC)
    client = ChatClient(runner.url("chatui"))
    client.await_scenario()

    return runner, client, scenario


class TestChatUiToEliza:
    def test_posted_utterance_gets_an_answer(self, chat):
        _, client, _ = chat
        chat_id = client.start_session()

        client.send(chat_id, "I feel very anxious today")

        replies = client.receive(chat_id)
        assert replies, "expected at least one reply from the agent"
        assert all(reply for reply in replies), "replies must be non-empty"

    def test_post_publishes_on_text_in(self, chat):
        """Chat UI is the text_in producer; if it stops publishing, nothing downstream runs."""
        runner, client, _ = chat
        chat_id = client.start_session()

        client.send(chat_id, "Hello there")

        event = runner.probe.await_event(
            TEXT_IN, lambda e: e.payload.signal.text == "Hello there")
        assert event.payload.signal.text == "Hello there"

    def test_reply_is_attributed_to_the_agent(self, chat):
        """The UI splits the transcript by speaker; a wrong name hides the reply."""
        _, client, _ = chat
        chat_id = client.start_session()
        client.send(chat_id, "Hello")
        client.receive(chat_id)

        utterances = client.fetch_all(chat_id)
        speakers = {utterance["speaker"] for utterance in utterances}

        assert "Leolani" in speakers, f"no agent utterance among speakers {speakers}"

    def test_scenario_reaches_the_chat_ui(self, chat):
        _, client, scenario = chat

        assert client.current()["scenario_id"] == scenario.id

    def test_multi_turn_conversation(self, chat):
        _, client, _ = chat
        chat_id = client.start_session()

        sequence = 0
        for utterance in ("Hello", "I feel sad", "Why do you ask?"):
            client.send(chat_id, utterance)
            replies = client.receive(chat_id, from_sequence=sequence)
            assert replies, f"no reply after {utterance!r}"
            sequence += 1 + len(replies)

    def test_unknown_chat_id_is_rejected(self, chat):
        _, client, _ = chat
        client.start_session()

        with pytest.raises(requests.HTTPError) as error:
            client.send("not-a-chat-id", "Hello")

        assert "404" in str(error.value)
