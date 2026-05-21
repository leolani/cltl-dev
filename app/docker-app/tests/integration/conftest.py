"""
Session-scoped fixtures for integration tests.

The docker-compose stack is started once per test session and torn down
automatically at the end. Individual tests receive a fresh ChatClient
and a fresh chat session so they remain fully isolated from each other.
"""
import logging
import subprocess
import time
from pathlib import Path

import pytest
import requests

from tests.integration.helpers.chat_client import ChatClient

logger = logging.getLogger(__name__)

_DOCKER_APP_DIR = Path(__file__).parent.parent.parent
COMPOSE_FILE = _DOCKER_APP_DIR / "docker-compose.yml"
COMPOSE_TEST_OVERRIDE = _DOCKER_APP_DIR / "docker-compose.test.yml"
assert COMPOSE_FILE.exists(), f"Docker Compose file not found: {COMPOSE_FILE}"
assert COMPOSE_TEST_OVERRIDE.exists(), f"Test compose override not found: {COMPOSE_TEST_OVERRIDE}"
CHATUI_READY_URL = "http://localhost:8003/chatui/chat/current"
STACK_STARTUP_TIMEOUT = 180  # seconds
STACK_POLL_INTERVAL = 3  # seconds

COMPOSE_AUDIO_OVERRIDE = _DOCKER_APP_DIR / "docker-compose.audio-test.yml"

_COMPOSE_CMD = [
    "docker", "compose",
    "-f", str(COMPOSE_FILE),
    "-f", str(COMPOSE_TEST_OVERRIDE),
]

_COMPOSE_AUDIO_CMD = [
    "docker", "compose",
    "-f", str(COMPOSE_FILE),
    "-f", str(COMPOSE_TEST_OVERRIDE),
    "-f", str(COMPOSE_AUDIO_OVERRIDE),
]


# ---------------------------------------------------------------------------
# Stack lifecycle
# ---------------------------------------------------------------------------

def _scenario_is_ready() -> bool:
    """Return True only after ChatUI has received a ScenarioStarted event.

    GET /chatui/chat/current includes a "scenario_id" field that is null until
    ChatUiService._scenario_id is set by a ScenarioStarted event. Checking this
    field is the correct signal — the "id" field (chat id) is always present
    because MemoryChats creates a chat id eagerly on first call.
    """
    try:
        response = requests.get(CHATUI_READY_URL, timeout=2)
        logger.debug("Readiness check: status=%s body=%r", response.status_code, response.text)
        if response.status_code != 200:
            return False
        data = response.json()
        scenario_id = data.get("scenario_id")
        if scenario_id:
            logger.info("Scenario ready: scenario_id=%s chat_id=%s", scenario_id, data.get("id"))
        return bool(scenario_id)
    except requests.RequestException as e:
        logger.debug("Readiness check failed: %s", e)
        return False


def _wait_for_stack(timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _scenario_is_ready():
            return
        time.sleep(STACK_POLL_INTERVAL)
    raise RuntimeError(
        f"Docker Compose stack did not become ready within {timeout}s. "
        "Check container logs: docker compose logs"
    )


@pytest.fixture(scope="session")
def docker_stack():
    """Bring the text-only Eliza docker-compose stack up for the test session."""
    subprocess.run(_COMPOSE_CMD + ["up", "-d", "--wait"], check=True)
    _wait_for_stack(STACK_STARTUP_TIMEOUT)

    yield

    log_path = _DOCKER_APP_DIR / "tests/integration/storage/docker.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "w") as f:
        subprocess.run(
            _COMPOSE_CMD + ["logs", "--no-color"],
            stdout=f,
            stderr=subprocess.STDOUT,
        )
    logger.info("Container logs written to %s", log_path)

    subprocess.run(_COMPOSE_CMD + ["down"], check=True)


@pytest.fixture(scope="session")
def docker_stack_audio(stub_server_greeting):
    """Bring the audio-enabled Eliza stack up for the audio test session.

    Depends on stub_server_greeting so the stub is running before the stack
    starts — the backend mic thread connects to the stub immediately after the
    scenario is created, so the stub must be available at that point.

    This fixture is NOT autouse — only audio tests request it explicitly.
    Run audio tests in a separate session from text tests (they bind the same ports):
      pytest tests/integration/test_audio_conversation.py
    """
    subprocess.run(_COMPOSE_AUDIO_CMD + ["up", "-d", "--wait"], check=True)
    _wait_for_stack(STACK_STARTUP_TIMEOUT)

    yield

    log_path = _DOCKER_APP_DIR / "tests/integration/storage/docker_audio.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "w") as f:
        subprocess.run(
            _COMPOSE_AUDIO_CMD + ["logs", "--no-color"],
            stdout=f,
            stderr=subprocess.STDOUT,
        )
    logger.info("Audio stack logs written to %s", log_path)

    subprocess.run(_COMPOSE_AUDIO_CMD + ["down"], check=True)


# ---------------------------------------------------------------------------
# Per-test fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def chat_client() -> ChatClient:
    """Return a ChatClient bound to the running stack."""
    return ChatClient()
