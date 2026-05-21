"""
End-to-end integration tests for audio conversation flow.

Each test starts the stub audio server with a specific utterance, waits for the
full pipeline to produce a ChatUI response, and asserts on the response text:

  Stub server → BackendService mic → AudioStorage → VAD → Whisper ASR
  → cltl.topic.text_in → ElizaService → cltl.topic.text_out → ChatUI

The ChatUI assertions are identical to the text-based tests.  ASR (Whisper) adds
significant latency, so RESPONSE_TIMEOUT is set to 60 seconds.
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
RESPONSE_TIMEOUT = 60.0   # seconds — Whisper inference can take a while


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _stub_is_ready() -> bool:
    try:
        r = requests.get(f"http://localhost:{STUB_PORT}/health", timeout=1)
        return r.status_code == 200
    except requests.RequestException:
        return False


def _start_stub(utterance: str) -> subprocess.Popen:
    """Start the stub audio server and wait until it is ready to serve requests."""
    proc = subprocess.Popen(
        [sys.executable, str(STUB_SCRIPT), "--port", str(STUB_PORT), utterance],
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
    """Session-scoped stub serving 'Hello' — started before the audio stack so
    the backend mic thread can connect to it immediately after scenario creation.

    The stub serves the same audio on every reconnect. Since the backend mic
    thread loops (reconnects after each stream ends), all audio tests in this
    session share a continuously looping 'Hello' utterance. This is sufficient
    to verify the full VAD → ASR → Eliza pipeline end-to-end.
    """
    proc = _start_stub("Hello")
    yield "Hello"
    proc.terminate()
    proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestAudioSingleTurn:
    def test_eliza_responds_to_spoken_greeting(
        self, docker_stack_audio, stub_server_greeting, chat_client: ChatClient
    ):
        chat_id = chat_client.start_session()
        # Poll from initial_sequence to skip any pre-existing messages (e.g. the
        # agent greeting published by InitService before this test started).
        responses = chat_client.receive(
            chat_id, from_sequence=chat_client.initial_sequence, timeout=RESPONSE_TIMEOUT
        )

        assert responses, "Expected at least one response from Leolani after spoken greeting"
        assert all(len(r) > 0 for r in responses), "Responses must be non-empty strings"
        print("XXX 1", responses)

    def test_eliza_responds_to_second_audio_utterance(
        self, docker_stack_audio, stub_server_greeting, chat_client: ChatClient
    ):
        """Verify the pipeline produces a second response as the stub keeps looping."""
        chat_id = chat_client.start_session()
        responses = chat_client.receive(
            chat_id, from_sequence=chat_client.initial_sequence, timeout=RESPONSE_TIMEOUT
        )

        assert responses, "Expected a second response from the looping audio stream"
        print("XXX 2", responses)
