"""The compose file, as Docker Compose itself reads it.

Asserted through ``docker compose config`` rather than a YAML parser, so what is
checked is compose's own interpretation — anchors merged, variables interpolated,
defaults applied. That is the only reading that matters, and it needs no
dependency the harness does not already require.

Marked ``compose`` because it shells out to the Docker CLI. It brings nothing up,
so it costs about a second; it is here rather than in tier 1 only because tier 1
must run without Docker.
"""
import json
import os
import subprocess

import pytest

from cltl_integration.modules import BY_KEY, MODULES
from cltl_integration.runner.compose import COMPOSE_FILE
from cltl_integration.runner.split import HOST_GATEWAY, SERVER_ENTRY_POINT

pytestmark = pytest.mark.compose

# The required variables, so that `config` can interpolate at all. Values are
# irrelevant here: nothing is started.
#
# CLTL_TENANT is not required for interpolation — `${CLTL_TENANT:-}` defaults on
# its own — but it is pinned to "" because `_config` starts from os.environ, and
# so does ComposeStack._compose_env, which assigns "" there for exactly this
# reason. Without it a CLTL_TENANT exported in a developer's shell would fail the
# untenanted-by-default assertion below while a real run stayed untenanted.
REQUIRED = {
    "CLTL_CONFIG_DIR": "/tmp/config",
    "CLTL_STORAGE_DIR": "/tmp/storage",
    "CLTL_MODEL_CACHE": "/tmp/whisper",
    "CLTL_TENANT": "",
}

TENANT = "tenant-a"

SPLIT = {
    "CLTL_CONTAINER_AMQP_URL": f"amqp://eliza:eliza123@{HOST_GATEWAY}:32768/",
    "CLTL_CONTAINER_STORAGE_URL": f"http://{HOST_GATEWAY}:32769/storage/",
    "CLTL_BACKEND_MAIN": SERVER_ENTRY_POINT,
}


def _config(**overrides) -> dict:
    env = {**os.environ, **REQUIRED, **overrides}
    result = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "config", "--format", "json"],
        env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr

    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def default_config():
    return _config()


@pytest.fixture(scope="module")
def split_config():
    return _config(**SPLIT)


def _modules(config: dict):
    return {key: service for key, service in config["services"].items()
            if key in BY_KEY}


class TestServices:
    def test_the_file_holds_exactly_the_registered_modules_and_a_broker(self, default_config):
        """One file, all services — a topology selects from it on the command line.

        A module in the registry with no service here fails at `up` with
        "no such service", three minutes into a tier-2 run.
        """
        assert set(default_config["services"]) == {m.key for m in MODULES} | {"rabbitmq"}

    def test_each_module_mounts_config_and_storage_at_its_own_workdir(self, default_config):
        """Every image loads config/ relative to its WORKDIR, and they differ.

        cltl-chat-ui builds to /cltl-chatui, without the second hyphen. A wrong
        path here produces a container that starts, finds no configuration and
        falls back to the legacy defaults baked into its own image — healthy,
        and wired to topics nothing else uses.
        """
        for key, service in _modules(default_config).items():
            targets = {volume["target"] for volume in service["volumes"]}
            assert BY_KEY[key].config_dir in targets, key
            assert BY_KEY[key].storage_dir in targets, key

    def test_nothing_claims_a_global_name(self, default_config):
        """No container_name and no fixed host port, so two stacks can coexist.

        This is what lets the client/server split run two projects at once, and
        what removed the prior art's serialized sessions and `up` retries.
        """
        for key, service in default_config["services"].items():
            assert "container_name" not in service, key
            for port in service.get("ports", []):
                assert "published" not in port, f"{key} pins host port {port}"

    def test_every_module_can_reach_the_host(self, default_config):
        """host.docker.internal is how a container reaches the stub microphone,
        and how a client stack reaches the server stack's published ports."""
        for key, service in _modules(default_config).items():
            assert f"{HOST_GATEWAY}=host-gateway" in service["extra_hosts"], key


class TestSingleStackDefaults:
    def test_modules_are_pointed_at_this_project_s_broker_and_storage(self, default_config):
        for key, service in _modules(default_config).items():
            assert service["environment"]["CLTL_AMQP_URL"] == \
                "amqp://eliza:eliza123@rabbitmq:5672/", key
            # Trailing slash is load-bearing: CltlAudioAdapter resolves
            # cltl-storage: URLs with urljoin, which drops the last segment when
            # the base does not end in one.
            assert service["environment"]["CLTL_STORAGE_URL"] == \
                "http://backend:8000/storage/", key
            # The two stubs that live outside the compose network. Declared even
            # when empty: EnvInterpolation warns on every unexpanded $VAR, and a
            # log full of expected warnings is a log nobody reads.
            assert service["environment"]["CLTL_AUDIO_URL"] == "", key
            assert service["environment"]["CLTL_TTS_URL"] == "", key
            # No tenant: KombuEventBus then binds `<topic>.#` and sees every
            # tenant, which is what a single stack and a shared server both want.
            assert service["environment"]["CLTL_TENANT"] == "", key

    def test_the_backend_runs_the_full_container_by_default(self, default_config):
        assert default_config["services"]["backend"]["command"] == ["python", "src/main.py"]


class TestSplitOverrides:
    """What the client half of a split sets, and the server half's entry point."""

    def test_every_module_follows_the_broker_and_storage_off_this_network(self, split_config):
        for key, service in _modules(split_config).items():
            assert service["environment"]["CLTL_AMQP_URL"] == \
                SPLIT["CLTL_CONTAINER_AMQP_URL"], key
            assert service["environment"]["CLTL_STORAGE_URL"] == \
                SPLIT["CLTL_CONTAINER_STORAGE_URL"], key

    def test_the_backend_can_be_switched_to_the_storage_entry_point(self, split_config):
        """StorageContainer rather than BackendContainer — see runner/split.py."""
        assert split_config["services"]["backend"]["command"] == \
            ["python", SERVER_ENTRY_POINT]


class TestTenantOverrides:
    """What one tenant deployment of a multi-tenant setup sets.

    Two tenants are two compose projects over this same file, differing in one
    variable. Checked here rather than only in tests/compose/test_multitenant.py
    because this costs a second and that costs three minutes, and because the
    failure it catches — the variable reaching some services and not others — is
    invisible from the outside: the modules that missed it keep working, on
    another tenant's traffic. See runner/tenants.py.
    """

    def test_a_tenant_deployment_tenants_every_module(self):
        for key, service in _modules(_config(CLTL_TENANT=TENANT)).items():
            assert service["environment"]["CLTL_TENANT"] == TENANT, key
