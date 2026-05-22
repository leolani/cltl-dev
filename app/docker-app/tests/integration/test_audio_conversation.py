"""
End-to-end integration tests for audio conversation flow.

Each test verifies the full audio pipeline:

  Stub server → BackendService mic → AudioStorage → VAD → Whisper ASR
  → cltl.topic.text_in → InitService → cltl.topic.text_out → ChatUI

The stub loops "Hello". The first "Hello" recognised by ASR triggers the InitService,
which responds with its greeting ("... Do you want to talk to me?"). That greeting is
the agent's response to the first audio utterance and is what the tests assert on.

ASR (Whisper) adds significant latency, so RESPONSE_TIMEOUT is set to 120 seconds.
"""
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests

from tests.integration.helpers.chat_client import ChatClient

STUB_PORT = 9876
STUB_SCRIPT = Path(__file__).parent / "audio" / "stub_audio_server.py"
STUB_READY_TIMEOUT = 15   # seconds to wait for Flask to be ready
RESPONSE_TIMEOUT = 120.0  # seconds — Whisper inference can take a while

# The InitService greeting always ends with the configured greeting text.
# This marker is present in every possible init response and cannot appear
# in the stub utterance "Hello" itself.
INIT_GREETING_MARKER = "do you want to talk to me"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _stub_is_ready() -> bool:
    try:
        r = requests.get(f"http://localhost:{STUB_PORT}/health", timeout=1)
        return r.status_code == 200
    except requests.RequestException:
        return False


def _start_stub(*utterances: str) -> subprocess.Popen:
    """Start the stub audio server serving *utterances* in sequence and wait until ready."""
    proc = subprocess.Popen(
        [sys.executable, str(STUB_SCRIPT), "--port", str(STUB_PORT), *utterances],
    )
    deadline = time.monotonic() + STUB_READY_TIMEOUT
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"Stub audio server exited early (code {proc.returncode}). "
                "Check that flask, gtts, and pydub are installed in the test venv."
            )
        if _stub_is_ready():
            return proc
        time.sleep(0.2)

    proc.terminate()
    raise RuntimeError(
        f"Stub audio server did not become ready within {STUB_READY_TIMEOUT}s "
        f"(port {STUB_PORT} — check it is not already in use)."
    )


@pytest.fixture(scope="session")
def stub_server_greeting():
    """Session-scoped stub looping 'Hello' to continuously trigger the audio pipeline."""
    proc = _start_stub("Hello")
    yield
    proc.terminate()
    proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestAudioSingleTurn:
    def test_eliza_responds_to_spoken_greeting(
        self, docker_stack_audio, stub_server_greeting, chat_client: ChatClient
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

    def test_eliza_responds_to_second_audio_utterance(
        self, docker_stack_audio, stub_server_greeting, chat_client: ChatClient
    ):
        """A subsequent 'Hello' triggers a second agent response.

        We poll from initial_sequence to skip responses already seen in the first test.
        """
        chat_id = chat_client.start_session()
        responses = chat_client.receive(
            chat_id, from_sequence=chat_client.initial_sequence, timeout=RESPONSE_TIMEOUT
        )

        assert responses, "Expected a second response from the looping audio stream"
        assert all(len(r) > 0 for r in responses), "Responses must be non-empty strings"
