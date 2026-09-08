"""Shared fixtures and process-wide setup for the integration harness.

Logging is configured exactly once here. Every CLTL ``main.py`` calls
``logging.config.fileConfig(..., disable_existing_loggers=False)``, which re-adds
handlers on each call — doing that per test multiplies log output on every
subsequent test.
"""
import logging

import pytest

from cltl_integration.runner.compose import ComposeRunner
from cltl_integration.runner.inprocess import InProcessRunner, reset_process_state
from cltl_integration.runner.split import SplitRunner
from cltl_integration.runner.tenants import TenantRunner
from cltl_integration.topology import Deployment, TenantDeployment, Topology

COMPOSE_TIMEOUT = 900


def pytest_configure(config):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # These three are pure noise in a test log: the config module dumps the
    # entire merged configuration at INFO on every load, the event bus logs each
    # subscribe/unsubscribe, and the AMQP client logs a line per frame.
    logging.getLogger("cltl.combot.infra.config.local").setLevel(logging.WARNING)
    logging.getLogger("cltl.combot.infra.event.memory").setLevel(logging.WARNING)
    logging.getLogger("amqp.connection").setLevel(logging.INFO)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


@pytest.fixture
def inprocess(tmp_path):
    """Factory starting topologies in this process, torn down in reverse order.

    A factory rather than a plain fixture because the topology is chosen per
    test, and because teardown must run even when the test fails — a topology
    left running holds port 8000 and poisons every subsequent test through the
    process-global DI singleton registry.
    """
    started = []

    def _start(topology: Topology, **kwargs) -> InProcessRunner:
        runner = InProcessRunner(
            topology, storage_dir=tmp_path / topology.name, **kwargs)
        started.append(runner)
        return runner.start()

    yield _start

    for runner in reversed(started):
        runner.stop()


def pytest_collection_modifyitems(config, items):
    """Give tier-2 tests room to pull images and wait for health checks.

    The global 300s in pytest.ini is sized for tier 1, where anything that slow
    is a hang worth failing on. A compose test spends most of its time in
    `docker compose up --wait`, so the same limit would fail the honest case.
    """
    for item in items:
        if item.get_closest_marker("compose") and not item.get_closest_marker("timeout"):
            item.add_marker(pytest.mark.timeout(COMPOSE_TIMEOUT))


@pytest.fixture
def compose(tmp_path):
    """Factory starting topologies as containers, torn down in reverse order.

    Function-scoped like ``inprocess``, and for the same reason: a stack left
    running holds its images, its ports and a RabbitMQ full of the previous
    test's events. Bringing one up costs ~12s, so tier-2 tests are deliberately
    few and each asserts several things.
    """
    started = []

    def _start(topology: Topology, **kwargs) -> ComposeRunner:
        runner = ComposeRunner(topology, storage_dir=tmp_path / topology.name, **kwargs)
        started.append(runner)
        return runner.start()

    yield _start

    for runner in reversed(started):
        runner.stop()


@pytest.fixture
def split(tmp_path):
    """Factory starting client/server split deployments, torn down in reverse.

    Two compose projects rather than one, so it costs roughly twice a ``compose``
    fixture to bring up. Same function scope for the same reason: the server half
    is a ``ComposeRunner``, and only one of those may own the process's
    configuration at a time.
    """
    started = []

    def _start(deployment: Deployment, **kwargs) -> SplitRunner:
        runner = SplitRunner(
            deployment, storage_dir=tmp_path / deployment.name, **kwargs)
        started.append(runner)
        return runner.start()

    yield _start

    for runner in reversed(started):
        runner.stop()


@pytest.fixture
def tenants(tmp_path):
    """Factory starting multi-tenant deployments, torn down in reverse.

    One compose project per tenant plus one for the shared server, so a
    two-tenant deployment costs about half again what ``split`` does. Same
    function scope, and for the same reason: the server half is a
    ``ComposeRunner``, and only one of those may own the process's configuration
    at a time.
    """
    started = []

    def _start(deployment: TenantDeployment, **kwargs) -> TenantRunner:
        runner = TenantRunner(
            deployment, storage_dir=tmp_path / deployment.name, **kwargs)
        started.append(runner)
        return runner.start()

    yield _start

    for runner in reversed(started):
        runner.stop()


@pytest.fixture
def clean_di():
    """Reset the process-global DI registry around a test that builds containers.

    ``DIContainer._singletons`` is a class attribute shared by every container in
    the process, so a test that instantiates one without the ``inprocess``
    fixture must clean up after itself or leak services into the next test.
    """
    reset_process_state()
    yield
    reset_process_state()


@pytest.fixture(autouse=True)
def offline_location(monkeypatch):
    """Stop ``ContextService`` from geolocating the test machine.

    ``ContextService._create_scenario`` calls ``requests.get("https://ipinfo.io")``
    on every scenario start — no timeout, bare ``except``. In a test suite that
    is an unbounded external dependency for a value nothing asserts on: on a host
    that blackholes outbound traffic the call hangs until the socket gives up,
    and every context topology pays for it.

    Autouse, because a context topology can be started from any test and the
    failure mode of forgetting this is a hang, not an error. The patch replaces
    the ``requests`` name *in that module only* — patching ``requests.get``
    itself would break ``ChatClient``, which talks to the harness over real HTTP.
    """
    try:
        from cltl_service.context import service as context_service
    except ImportError:  # cltl-context not installed; nothing to patch
        return

    class _Response:
        @staticmethod
        def json():
            return {"country": "NL", "region": "North Holland", "city": "Amsterdam"}

    class _Requests:
        @staticmethod
        def get(*args, **kwargs):
            return _Response()

    monkeypatch.setattr(context_service, "requests", _Requests)
