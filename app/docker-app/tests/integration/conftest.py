"""
Session-scoped fixtures for integration tests.

The docker-compose stack is started once per test session and torn down
automatically at the end. Individual tests receive a fresh ChatClient
and a fresh chat session so they remain fully isolated from each other.
"""
import logging
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests

from tests.integration.helpers.chat_client import ChatClient

logger = logging.getLogger(__name__)

_DOCKER_APP_DIR = Path(__file__).parent.parent.parent
_INTEGRATION_DIR = Path(__file__).parent
COMPOSE_FILE = _DOCKER_APP_DIR / "docker-compose.yml"
COMPOSE_TEST_OVERRIDE = _INTEGRATION_DIR / "docker-compose.test.yml"
assert COMPOSE_FILE.exists(), f"Docker Compose file not found: {COMPOSE_FILE}"
assert COMPOSE_TEST_OVERRIDE.exists(), f"Test compose override not found: {COMPOSE_TEST_OVERRIDE}"
assert (_DOCKER_APP_DIR / "docker-compose.server.yml").exists(), \
    f"Server compose not found: {_DOCKER_APP_DIR / 'docker-compose.server.yml'}"
assert (_DOCKER_APP_DIR / "docker-compose.client.yml").exists(), \
    f"Client compose not found: {_DOCKER_APP_DIR / 'docker-compose.client.yml'}"
CHATUI_READY_URL = "http://localhost:8003/chatui/chat/current"
SERVER_BACKEND_READY_URL = "http://localhost:8001/health"
CLIENT_BACKEND_READY_URL = "http://localhost:9001/health"
STACK_STARTUP_TIMEOUT = 180  # seconds
STACK_POLL_INTERVAL = 3  # seconds

_STUB_PORT = 9876
_STUB_SCRIPT = _INTEGRATION_DIR / "audio" / "stub_audio_server.py"
_STUB_READY_TIMEOUT = 15  # seconds

COMPOSE_AUDIO_OVERRIDE = _INTEGRATION_DIR / "docker-compose.audio-test.yml"

_COMPOSE_CMD = [
    "docker", "compose",
    "--project-directory", str(_DOCKER_APP_DIR),
    "-f", str(COMPOSE_FILE),
    "-f", str(COMPOSE_TEST_OVERRIDE),
]

_COMPOSE_AUDIO_CMD = [
    "docker", "compose",
    "--project-directory", str(_DOCKER_APP_DIR),
    "-f", str(COMPOSE_FILE),
    "-f", str(COMPOSE_TEST_OVERRIDE),
    "-f", str(COMPOSE_AUDIO_OVERRIDE),
]


# ---------------------------------------------------------------------------
# Stack lifecycle
# ---------------------------------------------------------------------------

_UP_RETRIES = 5
_UP_RETRY_DELAY = 3  # seconds


def _compose_up(cmd: list) -> None:
    """Tear down any leftover stack, then bring it up fresh.

    A previous interrupted run may have left containers running (or a
    compose down still in flight), causing RabbitMQ to receive a forced
    shutdown mid-test.  Always down first to guarantee a clean slate.

    A stack torn down moments ago (by a different Compose project reusing the
    same host port, e.g. RabbitMQ's 5672) can still fail the first `up` with
    "port is already allocated" — Docker's userland-proxy/iptables cleanup for
    the old container is asynchronous and can lag behind `down`'s exit. Retry
    with a short delay rather than failing immediately on that transient race.
    """
    subprocess.run(cmd + ["down", "--remove-orphans"], check=False)

    for attempt in range(1, _UP_RETRIES + 1):
        result = subprocess.run(cmd + ["up", "-d", "--wait"])
        if result.returncode == 0:
            return
        if attempt < _UP_RETRIES:
            logger.warning(
                "docker compose up failed (exit %s), attempt %d/%d — "
                "retrying in %ds (likely a port-release race from a just-torn-down stack)",
                result.returncode, attempt, _UP_RETRIES, _UP_RETRY_DELAY,
            )
            subprocess.run(cmd + ["down", "--remove-orphans"], check=False)
            time.sleep(_UP_RETRY_DELAY)

    subprocess.run(cmd + ["logs", "--no-color"])
    raise RuntimeError(
        f"docker compose up failed (exit {result.returncode}) after {_UP_RETRIES} attempts. "
        "Container logs printed above."
    )

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
    _compose_up(_COMPOSE_CMD)
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
    _compose_up(_COMPOSE_AUDIO_CMD)
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
# Client/server split stack fixtures
# ---------------------------------------------------------------------------

_COMPOSE_CSPLIT_SERVER_CMD = [
    "docker", "compose",
    "--project-directory", str(_DOCKER_APP_DIR),
    "--project-name", "eliza-csplit-server",
    "-f", str(_DOCKER_APP_DIR / "docker-compose.server.yml"),
    "-f", str(_INTEGRATION_DIR / "docker-compose.csplit-server-test.yml"),
]

_COMPOSE_CSPLIT_CLIENT_CMD = [
    "docker", "compose",
    "--project-directory", str(_DOCKER_APP_DIR),
    "--project-name", "eliza-csplit-client",
    "-f", str(_DOCKER_APP_DIR / "docker-compose.client.yml"),
    "-f", str(_INTEGRATION_DIR / "docker-compose.csplit-client-test.yml"),
]


def _clean_rabbitmq_data(storage_dir: Path) -> None:
    """Remove stale RabbitMQ mnesia data so each run starts with a clean broker."""
    import shutil
    mnesia_dir = storage_dir / "rabbitmq"
    if mnesia_dir.exists():
        shutil.rmtree(mnesia_dir)
        logger.info("Removed stale RabbitMQ data at %s", mnesia_dir)


@pytest.fixture(scope="session")
def csplit_server_stack():
    """Bring the server side of the client/server split stack up for the test session."""
    _clean_rabbitmq_data(_DOCKER_APP_DIR / "tests/integration/storage-csplit-server")
    _compose_up(_COMPOSE_CSPLIT_SERVER_CMD)
    _wait_for_server_backend(STACK_STARTUP_TIMEOUT)

    yield

    log_path = _DOCKER_APP_DIR / "tests/integration/storage-csplit-server/docker-server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "w") as f:
        subprocess.run(
            _COMPOSE_CSPLIT_SERVER_CMD + ["logs", "--no-color"],
            stdout=f,
            stderr=subprocess.STDOUT,
        )
    logger.info("csplit server logs written to %s", log_path)

    subprocess.run(_COMPOSE_CSPLIT_SERVER_CMD + ["down"], check=True)


def _wait_for_backend(url: str, label: str, timeout: float) -> None:
    """Block until the given backend health endpoint responds with 200."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = requests.get(url, timeout=2)
            if response.status_code == 200:
                logger.info("%s healthy at %s", label, url)
                return
        except requests.RequestException as e:
            logger.debug("%s not yet ready: %s", label, e)
        time.sleep(STACK_POLL_INTERVAL)
    raise RuntimeError(
        f"{label} did not become healthy within {timeout}s ({url}). Check container logs."
    )


def _wait_for_server_backend(timeout: float) -> None:
    _wait_for_backend(SERVER_BACKEND_READY_URL, "Server backend", timeout)


def _wait_for_client_backend(timeout: float) -> None:
    _wait_for_backend(CLIENT_BACKEND_READY_URL, "Client backend", timeout)


@pytest.fixture(scope="session")
def csplit_client_stack(csplit_server_stack):
    """Bring the client side of the split stack up, after the server is ready.

    Depends on csplit_server_stack so pytest guarantees the server stack (including
    RabbitMQ on 5672 and storage backend on 8001) is running before the client starts.
    Waits for the client backend health endpoint (localhost:9001) rather than the
    ChatUI scenario endpoint, which requires both stacks to be fully connected.
    """
    _compose_up(_COMPOSE_CSPLIT_CLIENT_CMD)
    _wait_for_client_backend(STACK_STARTUP_TIMEOUT)

    yield

    log_path = _DOCKER_APP_DIR / "tests/integration/storage-csplit-client/docker-client.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "w") as f:
        subprocess.run(
            _COMPOSE_CSPLIT_CLIENT_CMD + ["logs", "--no-color"],
            stdout=f,
            stderr=subprocess.STDOUT,
        )
    logger.info("csplit client logs written to %s", log_path)

    subprocess.run(_COMPOSE_CSPLIT_CLIENT_CMD + ["down"], check=True)


@pytest.fixture(scope="session")
def csplit_stack(csplit_client_stack):
    """Combined fixture: server and client stacks are both running.

    Tests should request this fixture rather than the individual halves.
    csplit_client_stack already depends on csplit_server_stack, so requesting
    csplit_stack transitively ensures both are up. Teardown order is the reverse:
    client tears down before server.
    """
    yield


# ---------------------------------------------------------------------------
# Stub audio server helpers (shared by monolithic and csplit audio tests)
# ---------------------------------------------------------------------------

def _stub_is_ready() -> bool:
    try:
        r = requests.get(f"http://localhost:{_STUB_PORT}/health", timeout=1)
        return r.status_code == 200
    except requests.RequestException:
        return False


def _start_stub(*utterances: str) -> subprocess.Popen:
    """Start the stub audio server serving *utterances* in sequence and wait until ready."""
    proc = subprocess.Popen(
        [sys.executable, str(_STUB_SCRIPT), "--port", str(_STUB_PORT), *utterances],
    )
    deadline = time.monotonic() + _STUB_READY_TIMEOUT
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
        f"Stub audio server did not become ready within {_STUB_READY_TIMEOUT}s "
        f"(port {_STUB_PORT} — check it is not already in use)."
    )


@pytest.fixture(scope="session")
def stub_server_greeting():
    """Session-scoped stub serving 'Hello' then 'yes' to drive through the InitService handshake.

    Request 0 ('Hello') triggers the InitService greeting. Request 1+ ('yes') passes
    the consent gate so subsequent utterances reach Eliza.
    """
    proc = _start_stub("Hello", "yes")
    yield
    proc.terminate()
    proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# Client/server split audio stack fixtures
# ---------------------------------------------------------------------------

_COMPOSE_CSPLIT_AUDIO_SERVER_CMD = [
    "docker", "compose",
    "--project-directory", str(_DOCKER_APP_DIR),
    "--project-name", "eliza-csplit-server",
    "-f", str(_DOCKER_APP_DIR / "docker-compose.server.yml"),
    "-f", str(_INTEGRATION_DIR / "docker-compose.csplit-server-test.yml"),
    "-f", str(_INTEGRATION_DIR / "docker-compose.csplit-audio-server-test.yml"),
]

_COMPOSE_CSPLIT_AUDIO_CLIENT_CMD = [
    "docker", "compose",
    "--project-directory", str(_DOCKER_APP_DIR),
    "--project-name", "eliza-csplit-client",
    "-f", str(_DOCKER_APP_DIR / "docker-compose.client.yml"),
    "-f", str(_INTEGRATION_DIR / "docker-compose.csplit-client-test.yml"),
    "-f", str(_INTEGRATION_DIR / "docker-compose.csplit-audio-client-test.yml"),
]


@pytest.fixture(scope="session")
def csplit_audio_server_stack(stub_server_greeting):
    """Bring the audio-enabled server side of the split stack up.

    Depends on stub_server_greeting so the stub is running before the server
    stack starts — the client backend mic thread connects to the stub immediately
    after the scenario is created, so the stub must be available at that point.
    """
    _clean_rabbitmq_data(_DOCKER_APP_DIR / "tests/integration/storage-csplit-server")
    _compose_up(_COMPOSE_CSPLIT_AUDIO_SERVER_CMD)
    _wait_for_server_backend(STACK_STARTUP_TIMEOUT)

    yield

    log_path = _DOCKER_APP_DIR / "tests/integration/storage-csplit-server/docker-audio-server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "w") as f:
        subprocess.run(
            _COMPOSE_CSPLIT_AUDIO_SERVER_CMD + ["logs", "--no-color"],
            stdout=f,
            stderr=subprocess.STDOUT,
        )
    logger.info("csplit audio server logs written to %s", log_path)

    subprocess.run(_COMPOSE_CSPLIT_AUDIO_SERVER_CMD + ["down"], check=True)


@pytest.fixture(scope="session")
def csplit_audio_client_stack(csplit_audio_server_stack):
    """Bring the audio-enabled client side of the split stack up, after the server is ready."""
    _compose_up(_COMPOSE_CSPLIT_AUDIO_CLIENT_CMD)
    _wait_for_client_backend(STACK_STARTUP_TIMEOUT)

    yield

    log_path = _DOCKER_APP_DIR / "tests/integration/storage-csplit-client/docker-audio-client.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "w") as f:
        subprocess.run(
            _COMPOSE_CSPLIT_AUDIO_CLIENT_CMD + ["logs", "--no-color"],
            stdout=f,
            stderr=subprocess.STDOUT,
        )
    logger.info("csplit audio client logs written to %s", log_path)

    subprocess.run(_COMPOSE_CSPLIT_AUDIO_CLIENT_CMD + ["down"], check=True)


@pytest.fixture(scope="session")
def csplit_audio_stack(csplit_audio_client_stack):
    """Combined fixture: audio-enabled server and client stacks are both running."""
    yield


# ---------------------------------------------------------------------------
# Per-test fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def chat_client() -> ChatClient:
    """Return a ChatClient bound to the running stack."""
    return ChatClient()
