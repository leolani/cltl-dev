"""Tier 2: the platform as the container images it ships.

Where tier 1 asks "do these modules wire up", tier 2 asks "does the thing we
deploy work". The differences are not incidental:

* every event round-trips through ``marshal``/``unmarshal``, so a payload that
  cannot be serialised passes tier 1 and fails here;
* each module gets its own ``ThreadedResourceManager``, so the resource locks
  that synchronise microphone and TTS in one process do nothing across
  containers;
* the modules read their configuration from the images' own working directories,
  which is the only place a missing or misspelled config path shows up.

The test process joins the same RabbitMQ bus as the containers, which is what
lets a slice assertion written for tier 1 run here unchanged.
"""
import collections
import configparser
import contextlib
import json
import logging
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence, Tuple

import requests
from cltl.combot.infra.event import EventBus

from cltl_integration.runner.api import EventProbe
from cltl_integration.runner.inprocess import (HarnessInfraContainer,
                                               reset_process_state)
from cltl_integration.topology import CONFIG_DIR, Topology, load_config

logger = logging.getLogger(__name__)

COMPONENT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
COMPOSE_FILE = COMPONENT_ROOT / "compose" / "docker-compose.yml"
# Shared across runs and kept out of the per-run storage directory, so that
# `down -v` and a fresh tmp_path do not cost another 140 MB model download.
MODEL_CACHE = COMPONENT_ROOT / ".cache" / "whisper"
LOGGING_CONFIG = CONFIG_DIR / "logging.config"
BASE_CONFIG = CONFIG_DIR / "base.config"

BROKER_USER = "eliza"
BROKER_PASSWORD = "eliza123"
EXCHANGE = "cltl.combot"

STARTUP_TIMEOUT = 300.0
BINDING_TIMEOUT = 30.0


class ComposeError(RuntimeError):
    """The stack could not be brought up, or did not come up healthy."""


# -- images ----------------------------------------------------------------

def required_images(topology: Topology, tag: str = "latest") -> Tuple[str, ...]:
    images = [f"{module.image}:{tag}" for module in topology.ordered_modules()]

    return tuple(images + ["rabbitmq:3.12-management"])


def missing_images(topology: Topology, tag: str = "latest") -> Tuple[str, ...]:
    """Images the daemon does not have.

    Checked up front rather than discovered as a pull halfway through a test:
    the compose file pins no digests and every component tags ``latest``, so a
    stack will happily come up on images built from a different commit. Failing
    fast with the build command is more useful than a green run of stale code.
    """
    missing = []
    for image in required_images(topology, tag):
        result = subprocess.run(["docker", "image", "inspect", image],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode != 0:
            missing.append(image)

    return tuple(missing)


# -- configuration ---------------------------------------------------------

def write_config_dir(topology: Topology, dest: Path) -> Path:
    """Materialise the config directory each container mounts.

    The platform's loader reads a fixed ``config/default.config`` plus a fixed
    two-element list of extra files, so the harness's three-layer scheme
    (base < tier < topology) has to be flattened into two before it reaches a
    container: base becomes ``default.config``, and tier plus overlay are merged
    into ``custom.config``.

    Merged with interpolation switched off, deliberately. These files are full
    of ``$CLTL_*`` placeholders that only mean something inside the container,
    where its own environment expands them; resolving them here would bake the
    test process's view of the network into every module's configuration.
    """
    dest.mkdir(parents=True, exist_ok=True)

    shutil.copyfile(BASE_CONFIG, dest / "default.config")
    shutil.copyfile(LOGGING_CONFIG, dest / "logging.config")

    _, additional = topology.config_files("compose")
    merged = configparser.ConfigParser(interpolation=None)
    merged.read([str(path) for path in additional])
    with open(dest / "custom.config", "w") as custom:
        merged.write(custom)

    return dest


# -- runner ----------------------------------------------------------------

class ComposeStack:
    """One compose project: images checked, config written, services up.

    Everything about bringing containers up and finding them again, and nothing
    about this process. That separation exists for the client/server split, where
    two stacks run at once but only one of them can own the test process's
    configuration and its connection to the bus — ``LocalConfigurationContainer``
    keeps its state in a class attribute, so a second ``load_configuration`` does
    not add a stack, it replaces one. The client half is a bare stack; the server
    half is a :class:`ComposeRunner`, which is this plus that ownership.

    Use as a context manager.
    """

    def __init__(self, topology: Topology, storage_dir: Path,
                 image_tag: str = "latest",
                 environment: Optional[Mapping[str, str]] = None,
                 timeout: float = STARTUP_TIMEOUT,
                 broker: bool = True):
        self._topology = topology
        self._root = Path(storage_dir).resolve()
        self._image_tag = image_tag
        self._environment = dict(environment or {})
        self._timeout = timeout
        self._broker = broker

        self._modules = topology.ordered_modules()
        # Unique per run so two stacks can coexist; nothing in the compose file
        # carries a global name that would collide anyway.
        self._project = f"cltl-it-{uuid.uuid4().hex[:10]}"

        self._ports: Dict[str, Dict[int, int]] = {}
        self._started = False

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "ComposeStack":
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()

    def start(self) -> "ComposeStack":
        try:
            self._start()
        except BaseException:
            # Leaving a stack up costs the next run its images, its ports and,
            # if it is a RabbitMQ, everything published on the bus.
            self.stop()
            raise

        return self

    def _start(self) -> None:
        missing = missing_images(self._topology, self._image_tag)
        if missing:
            raise ComposeError(
                f"{self._topology.name}: images not present: {', '.join(missing)}. "
                f"Build them with `make docker-ghcr-build` (component images) and "
                f"`docker pull` (rabbitmq), or see integration/README.md.")

        MODEL_CACHE.mkdir(parents=True, exist_ok=True)
        config_dir = write_config_dir(self._topology, self._root / "config")
        storage_dir = self._root / "storage"
        for name in ("", "audio", "image", "emissor", "event_log"):
            (storage_dir / name).mkdir(parents=True, exist_ok=True)
        # The containers run as root and write here; make sure they can.
        storage_dir.chmod(0o777)

        up = ["up", "-d", "--wait", "--wait-timeout", str(int(self._timeout))]
        if not self._broker:
            # This stack talks to a broker in another project, so compose must
            # not honour the `depends_on: rabbitmq` every service declares and
            # start a second one on this network.
            up.append("--no-deps")

        self._compose(*up, *self._service_names(),
                      env=self._compose_env(config_dir, storage_dir),
                      timeout=self._timeout + 60)
        self._started = True

        self._discover_ports()
        self._attach()

    def stop(self) -> None:
        try:
            if self._started:
                self._capture_logs()
        finally:
            try:
                self._detach()
            finally:
                try:
                    if self._started:
                        self._compose("down", "-v", "--remove-orphans",
                                      env=self._compose_env(), timeout=180, check=False)
                finally:
                    self._started = False
                    self._release()

    # -- hooks for a stack this process attaches to ------------------------

    def _attach(self) -> None:
        """Called once the containers are up and their ports are known."""

    def _detach(self) -> None:
        """Called before the stack comes down, while it is still reachable."""

    def _release(self) -> None:
        """Called after the stack is down, whether or not it ever came up."""

    # -- compose -----------------------------------------------------------

    def _service_names(self) -> Tuple[str, ...]:
        return tuple(module.service for module in self._modules)

    def _compose_env(self, config_dir: Optional[Path] = None,
                     storage_dir: Optional[Path] = None) -> Dict[str, str]:
        env = dict(os.environ)
        env["CLTL_IMAGE_TAG"] = self._image_tag
        env["CLTL_CONFIG_DIR"] = str(config_dir or (self._root / "config"))
        env["CLTL_STORAGE_DIR"] = str(storage_dir or (self._root / "storage"))
        env["CLTL_MODEL_CACHE"] = str(MODEL_CACHE)
        env.setdefault("CLTL_AUDIO_URL", "")
        env.setdefault("CLTL_TTS_URL", "")
        env.update(self._environment)

        return env

    def _compose(self, *args: str, env: Optional[Mapping[str, str]] = None,
                 timeout: float = 120, check: bool = True) -> subprocess.CompletedProcess:
        command = ["docker", "compose", "-p", self._project, "-f", str(COMPOSE_FILE), *args]
        logger.info("compose: %s", " ".join(args))
        result = subprocess.run(command, env=dict(env or os.environ), timeout=timeout,
                                capture_output=True, text=True)
        if check and result.returncode != 0:
            raise ComposeError(
                f"`docker compose {' '.join(args)}` failed ({result.returncode}) for "
                f"topology {self._topology.name}\n"
                f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}")

        return result

    def _discover_ports(self) -> None:
        services = (("rabbitmq",) if self._broker else ()) + self._service_names()
        for service in services:
            container_ports = [5672, 15672] if service == "rabbitmq" else [8000]
            for port in container_ports:
                result = self._compose("port", service, str(port), env=self._compose_env())
                published = result.stdout.strip().rsplit(":", 1)
                if len(published) != 2 or not published[1].isdigit():
                    raise ComposeError(
                        f"could not read the published port for {service}:{port} "
                        f"(got {result.stdout!r})")
                self._ports.setdefault(service, {})[port] = int(published[1])

        logger.info("Topology %s published %s", self._topology.name, self._ports)

    def service_command(self, service: str) -> Tuple[str, ...]:
        """The command a running container was actually started with.

        Read back from the daemon rather than from the compose file, because the
        compose file only says what the command would be *given the environment*
        — and the environment is exactly what a two-stack deployment gets wrong.
        """
        container = self._compose("ps", "-q", service, env=self._compose_env()).stdout.strip()
        if not container:
            raise ComposeError(f"{service} is not running in project {self._project}")

        result = subprocess.run(
            ["docker", "inspect", "--format", "{{json .Config.Cmd}}", container],
            capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise ComposeError(f"could not inspect {service}: {result.stderr}")

        return tuple(json.loads(result.stdout) or ())

    def _capture_logs(self) -> None:
        """Container logs are the only account of a tier-2 failure."""
        path = self._root / "docker.log"
        try:
            result = self._compose("logs", "--no-color", env=self._compose_env(),
                                   timeout=120, check=False)
            path.write_text(result.stdout + result.stderr)
            logger.info("Container logs written to %s", path)
        except Exception:
            logger.exception("Failed to capture container logs")

    # -- Runner protocol ---------------------------------------------------

    def port(self, service: str, container_port: int = 8000) -> int:
        try:
            return self._ports[service][container_port]
        except KeyError:
            raise KeyError(
                f"{service}:{container_port} is not published by topology "
                f"{self._topology.name} ({sorted(self._ports)})")

    def base_url(self, module_key: str) -> str:
        return f"http://127.0.0.1:{self.port(module_key)}"

    def url(self, module_key: str) -> str:
        for module in self._modules:
            if module.key != module_key:
                continue
            if not module.mounts:
                raise KeyError(f"module {module_key!r} serves no HTTP endpoint")
            return self.base_url(module_key) + module.mounts[0][0]
        raise KeyError(
            f"module {module_key!r} is not in topology {self._topology.name!r} "
            f"({[m.key for m in self._modules]})")

    @property
    def topology(self) -> Topology:
        return self._topology

    @property
    def storage_path(self) -> Path:
        return self._root / "storage"

    @property
    def config_path(self) -> Path:
        return self._root / "config"

    @property
    def project(self) -> str:
        return self._project


class ComposeRunner(ComposeStack):
    """A stack this process joins: its bus, its configuration, its probe.

    Exactly one of these may be running at a time. The configuration it loads and
    the DI singletons it resets are process-global, so a second one does not add
    a stack — it silently takes the first one's place.
    """

    def __init__(self, topology: Topology, storage_dir: Path,
                 image_tag: str = "latest",
                 environment: Optional[Mapping[str, str]] = None,
                 timeout: float = STARTUP_TIMEOUT):
        super().__init__(topology, storage_dir, image_tag=image_tag,
                         environment=environment, timeout=timeout, broker=True)
        self._container = None
        self._probe: Optional[EventProbe] = None
        self._environ_backup: Optional[Dict[str, str]] = None

    def start(self) -> "ComposeRunner":
        self._environ_backup = dict(os.environ)
        super().start()

        return self

    def _attach(self) -> None:
        # Point *this* process's copy of the configuration at the published
        # ports. The containers read the same files with the compose-network
        # values in their environment instead.
        reset_process_state()
        os.environ["CLTL_AMQP_URL"] = self.amqp_url
        # Every variable the tier config interpolates has to be defined even
        # when this topology has nothing behind it, or EnvInterpolation logs a
        # warning for each unexpanded $VAR on every read.
        os.environ["CLTL_STORAGE_URL"] = (
            self.base_url("backend") + "/storage/" if "backend" in self._topology.modules else "")
        os.environ["CLTL_AUDIO_URL"] = ""
        os.environ["CLTL_TTS_URL"] = ""
        os.environ.update(self._environment)

        load_config(self._topology, "compose")

        self._container = type("ProbeContainer", (HarnessInfraContainer,), {})()
        self._probe = EventProbe(self._container.event_bus,
                                 readiness=self._binding_readiness)

    def _detach(self) -> None:
        try:
            if self._container is not None:
                bus = self._container.event_bus
                if hasattr(bus, "close"):
                    bus.close()
        except Exception:
            logger.exception("Failed to close the probe's event bus")

    def _release(self) -> None:
        self._container = None
        self._probe = None
        reset_process_state(self._environ_backup)

    # -- broker ------------------------------------------------------------

    @contextlib.contextmanager
    def _binding_readiness(self, topics: Sequence[str]):
        """Wrap a probe subscribe; return once the broker has bound its queues.

        Asks RabbitMQ rather than round-tripping a sentinel through the
        exchange: a sentinel would have to travel on a real topic, where the
        modules would try to process it, log a traceback apiece and leave an
        event in the probe's own record that no assertion expects.

        Counts rather than tests for presence. The modules subscribe to these
        same topics, so their queues are already bound by the time a test
        subscribes, and "is anything bound to cltl.topic.scenario" is answered
        yes before the probe's own queue exists. Waiting for the count to rise
        is what makes the check about *this* subscription.
        """
        before = self._binding_counts()
        yield
        self._await_bindings(topics, before)

    def _await_bindings(self, topics: Sequence[str],
                        before: Mapping[str, int]) -> None:
        expected = {f"{topic}.#": before.get(f"{topic}.#", 0) + 1 for topic in topics}
        deadline = time.monotonic() + BINDING_TIMEOUT
        counts: Mapping[str, int] = {}
        while time.monotonic() < deadline:
            counts = self._binding_counts()
            if all(counts.get(key, 0) >= wanted for key, wanted in expected.items()):
                return
            time.sleep(0.1)

        short = {key: (counts.get(key, 0), wanted)
                 for key, wanted in expected.items() if counts.get(key, 0) < wanted}
        raise ComposeError(
            f"RabbitMQ did not bind the probe's queues within {BINDING_TIMEOUT}s; "
            f"it would silently miss those topics. Routing key -> (bound, wanted): "
            f"{short}")

    def _binding_counts(self) -> Mapping[str, int]:
        url = f"{self.management_url}/api/exchanges/%2F/{EXCHANGE}/bindings/source"
        try:
            response = requests.get(url, auth=(BROKER_USER, BROKER_PASSWORD), timeout=5)
        except requests.RequestException:
            return {}
        if response.status_code != 200:
            # 404 until the first consumer or producer declares the exchange.
            return {}

        return collections.Counter(binding["routing_key"] for binding in response.json())

    # -- Runner protocol ---------------------------------------------------

    @property
    def amqp_url(self) -> str:
        return (f"amqp://{BROKER_USER}:{BROKER_PASSWORD}@127.0.0.1:"
                f"{self.port('rabbitmq', 5672)}/")

    @property
    def management_url(self) -> str:
        return f"http://127.0.0.1:{self.port('rabbitmq', 15672)}"

    @property
    def probe(self) -> EventProbe:
        if self._probe is None:
            raise RuntimeError("runner is not started")
        return self._probe

    @property
    def event_bus(self) -> EventBus:
        return self._container.event_bus

    @property
    def config_manager(self):
        return self._container.config_manager
