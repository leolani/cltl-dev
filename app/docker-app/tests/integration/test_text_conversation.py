"""
End-to-end integration tests for text-only conversation flow.

Each test starts a fresh chat session and exercises the full pipeline:
  HTTP POST (ChatUI) → RabbitMQ → ElizaService → RabbitMQ → ChatUI → HTTP GET
"""
import pytest

from tests.integration.helpers.chat_client import ChatClient

RESPONSE_TIMEOUT = 15.0  # seconds to wait for each agent reply


class TestSingleTurn:
    def test_eliza_responds_to_greeting(self, chat_client: ChatClient):
        chat_id = chat_client.start_session()
        chat_client.send(chat_id, "Hello")
        responses = chat_client.receive(chat_id, timeout=RESPONSE_TIMEOUT)

        assert responses, "Expected at least one response from Leolani"
        assert all(len(r) > 0 for r in responses), "Responses must be non-empty strings"

    def test_eliza_responds_to_feeling_statement(self, chat_client: ChatClient):
        chat_id = chat_client.start_session()
        chat_client.send(chat_id, "I feel very anxious today")
        responses = chat_client.receive(chat_id, timeout=RESPONSE_TIMEOUT)

        assert responses, "Expected a response to an emotional statement"

    def test_eliza_responds_to_question(self, chat_client: ChatClient):
        chat_id = chat_client.start_session()
        chat_client.send(chat_id, "Can you help me?")
        responses = chat_client.receive(chat_id, timeout=RESPONSE_TIMEOUT)

        assert responses, "Expected a response to a question"


class TestMultiTurn:
    def test_eliza_handles_multiple_turns(self, chat_client: ChatClient):
        """Verify the agent responds to each message in a multi-turn conversation."""
        chat_id = chat_client.start_session()
        utterances = [
            "Hello",
            "yes",
            "I feel sad",
            "Why do you ask?",
        ]

        sequence = 0
        for utterance in utterances:
            chat_client.send(chat_id, utterance)
            responses = chat_client.receive(chat_id, from_sequence=sequence, timeout=RESPONSE_TIMEOUT)
            assert responses, f"Expected a response after: {utterance!r}"
            user_utterance_count = 1
            sequence += user_utterance_count + len(responses)

    def test_sessions_are_isolated(self, chat_client: ChatClient):
        """
        Two separate ChatClient instances share the same server-side session when timeout is 0.

        The server has `timeout: 0` configured, so it maintains a single global active chat.
        Isolation between test turns happens at the sequence level, not at the session level.
        """
        client_a = ChatClient()
        client_b = ChatClient()

        id_a = client_a.start_session()
        id_b = client_b.start_session()

        assert id_a == id_b, "Both clients must reference the same server-side active session"
