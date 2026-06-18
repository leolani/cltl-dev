"""
End-to-end integration tests for the audio pipeline in the client/server split deployment.

Verifies the full cross-stack audio flow:

  Stub server (host:9876) → client eliza-backend mic thread
  → uploads audio to server storage (localhost:8001)
  → server VAD → server Whisper ASR → cltl.topic.text_in
  → InitService → cltl.topic.text_out → ChatUI (localhost:8003)

The stub loops "Hello" then "yes". The first "Hello" recognised by ASR triggers
the InitService greeting, which is what the test asserts on.

ASR (Whisper) adds significant latency, so RESPONSE_TIMEOUT is set to 120 seconds.

Run in a separate pytest session from all other test modules — all bind the same
host ports (8003, 5672, 8001):
  pytest tests/integration/test_csplit_audio_conversation.py
"""
import pytest

from tests.integration.helpers.chat_client import ChatClient

pytestmark = pytest.mark.usefixtures("csplit_audio_stack")

RESPONSE_TIMEOUT = 120.0

# The InitService greeting always ends with the configured greeting text.
INIT_GREETING_MARKER = "do you want to talk to me"


class TestCsplitAudioSingleTurn:
    def test_eliza_responds_to_spoken_greeting(
        self, csplit_audio_stack, stub_server_greeting, chat_client: ChatClient
    ):
        """The first spoken 'Hello' triggers the InitService greeting.

        We poll from sequence 0 — not skipping initial messages — because the
        InitService greeting is the agent's direct response to the first audio
        utterance and may have arrived before start_session() was called.
        """
        chat_id = chat_client.start_session()
        responses = chat_client.receive(chat_id, from_sequence=0, timeout=RESPONSE_TIMEOUT)

        assert responses, "Expected at least one response from Leolani after spoken greeting"
        assert any(
            INIT_GREETING_MARKER in r.lower() for r in responses
        ), f"Expected init greeting (containing '{INIT_GREETING_MARKER}'), got: {responses}"
