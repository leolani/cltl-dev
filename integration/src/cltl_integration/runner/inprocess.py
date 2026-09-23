"""Tier 1: compose the platform's DI containers in a single process.

Fast enough to run on every build, and the only tier that runs in a development
environment without Docker. It exercises module wiring, configuration and
packaging; it does *not* exercise event serialization, the container images, or
anything that depends on modules living in separate processes.
"""
import importlib
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import flask
from cltl.combot.infra.container import InfraContainer
from cltl.combot.infra.di_container import DIContainer, singleton
from cltl.combot.infra.event import EventBus
from cltl.combot.infra.event.memory import SynchronousEventBus
from werkzeug.middleware.dispatcher import DispatcherMiddleware
from werkzeug.serving import make_server

from cltl_integration.modules import Module
from cltl_integration.runner.api import EventProbe
from cltl_integration.serialization import deserializer, serializer
from cltl_integration.topology import Topology, load_config

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8000

# Every variable tier-inprocess.config interpolates must be set before the
# configuration is read, whether or not the topology uses it: EnvInterpolation
# logs a warning for each unexpanded $VAR, and a log full of warnings that are
# expected is a log nobody reads. Topologies that need a real value pass it
# through InProcessRunner(environment=...).
TIER_ENVIRONMENT_DEFAULTS = {
    "CLTL_AUDIO_URL": "",
    "CLTL_IMAGE_URL": "",
    "CLTL_TTS_URL": "",
}


class HarnessInfraContainer(InfraContainer):
    """Selects the event bus from configuration, exactly as every main.py does.

    This must be the FIRST base of a synthesised container type.
    ``KombuEventBusContainer.event_bus`` is a plain property, not a singleton, so
    any component container later in the MRO would otherwise win and the topology
    would quietly talk to RabbitMQ.
    """

    @property
    @singleton
    def event_bus_serializer(self):
        return serializer, deserializer

    @property
    @singleton
    def event_bus(self) -> EventBus:
        implementation = self.config_manager.get_config("cltl.event").get("implementation")
        if implementation == "internal":
            return SynchronousEventBus()
        if implementation == "kombu":
            return super().event_bus
        raise ValueError(f"Unsupported event bus implementation: {implementation!r}")


def build_container_type(modules: Sequence[Module],
                        overrides: Optional[Mapping[str, Any]] = None) -> type:
    """Synthesise an ApplicationContainer from the topology's containers.

    Every component container derives from ``InfraContainer`` and nothing else,
    so C3 linearises any subset in registry order without conflict.

    *overrides* are placed in the synthesised class' own namespace, so they win
    over every base regardless of MRO position. That is how a topology swaps in
    a test double for a service the harness cannot run — the ASR backends all
    pull in torch, and the microphone reads from real hardware. It is the same
    mechanism ``app/py-app/app.py`` uses to replace ``context_service``.
    """
    bases: List[type] = [HarnessInfraContainer]
    for module in modules:
        module_path, class_name = module.container_ref()
        imported = importlib.import_module(module_path)
        bases.append(getattr(imported, class_name))

    return type("ApplicationContainer", tuple(bases), dict(overrides or {}))


def reset_process_state(environ_backup: Optional[Dict[str, str]] = None) -> None:
    """Undo the process-global state a started topology leaves behind.

    ``DIContainer._reset()`` alone is not enough:

    * It must be called on ``DIContainer`` itself. ``_reset`` is a classmethod
      doing ``cls._singletons = dict()``, so calling it on a subclass shadows the
      attribute instead of clearing the one ``@singleton`` actually writes to.
    * ``LocalConfigurationContainer`` holds the parsed config in a name-mangled
      *class* attribute, not a singleton, so it survives the reset. Callers must
      reload configuration after this, never before.
    * ``load_configuration`` copies the config's ``[environment]`` section into
      ``os.environ`` permanently, and ``EnvInterpolation`` substitutes
      ``os.environ`` into every value on read — so one topology leaks into the
      next topology's *interpolation* unless the environment is restored.
    """
    DIContainer._reset()
    if environ_backup is not None:
        os.environ.clear()
        os.environ.update(environ_backup)


def leaked_threads(baseline: Sequence[str], settle: float = 2.0) -> List[str]:
    """Names of threads alive now but not in *baseline*, after letting them settle.

    Worth checking between topologies: ``VadService.stop()`` shuts its
    ``ThreadPoolExecutor`` down with ``wait=False``, so detect tasks can outlive
    the topology and keep talking to a storage server that has since moved.

    The grace period is not optional. ``TopicWorker.await_stop`` waits on an
    event that the worker sets just *before* its thread terminates, so a check
    made immediately after ``stop()`` reports every worker as leaked. A detector
    that cries wolf on every teardown is worse than no detector.
    """
    end = time.monotonic() + settle
    while True:
        leaked = _extra_threads(baseline)
        if not leaked or time.monotonic() >= end:
            return leaked
        time.sleep(0.02)


def _extra_threads(baseline: Sequence[str]) -> List[str]:
    remaining: Dict[str, int] = {}
    for name in baseline:
        remaining[name] = remaining.get(name, 0) + 1

    extra = []
    for thread in threading.enumerate():
        if remaining.get(thread.name, 0) > 0:
            remaining[thread.name] -= 1
        else:
            extra.append(thread.name)
    return extra


class InProcessRunner:
    """Start a :class:`Topology` in this process; use as a context manager."""

    def __init__(self, topology: Topology, storage_dir: Path,
                 port: int = DEFAULT_PORT,
                 extra_config: Sequence[Path] = (),
                 environment: Optional[Mapping[str, str]] = None,
                 container_overrides: Optional[Mapping[str, Any]] = None):
        self._topology = topology
        self._storage_dir = Path(storage_dir).resolve()
        self._port = port
        self._extra_config = tuple(extra_config)
        self._environment = dict(environment or {})
        self._container_overrides = dict(container_overrides or {})

        self._modules = topology.ordered_modules()
        self._container = None
        self._server = None
        self._server_thread = None
        self._probe: Optional[EventProbe] = None
        self._started = False
        self._environ_backup: Optional[Dict[str, str]] = None
        self._thread_baseline: Tuple[str, ...] = ()

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "InProcessRunner":
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()

    def start(self) -> "InProcessRunner":
        self._environ_backup = dict(os.environ)
        self._thread_baseline = tuple(thread.name for thread in threading.enumerate())
        try:
            self._start()
        except BaseException:
            # A half-started topology is worse than none: it holds the HTTP port
            # and leaves the process-global singleton registry populated, so the
            # next test in this process silently reuses it.
            self.stop()
            raise
        return self

    def _start(self) -> None:
        reset_process_state()
        self._make_storage_dirs()

        # Set before loading: EnvInterpolation expands these when values are read.
        os.environ.update(TIER_ENVIRONMENT_DEFAULTS)
        os.environ["CLTL_HTTP_BASE"] = self.base_url
        os.environ["CLTL_STORAGE_DIR"] = str(self._storage_dir)
        # Where the *browser* reaches cltl-monitoring, for the chat UI's
        # Monitoring tab. Root-relative here because every module in this tier
        # shares one DispatcherMiddleware on one port. Assigned rather than
        # defaulted, and to the empty string when the topology has no monitoring
        # module: an unexpanded $VAR would otherwise warn on every config read,
        # and an empty value is what leaves the tab out.
        os.environ["CLTL_MONITORING_URL"] = (
            "/monitoring" if "monitoring" in self._topology.modules else "")
        os.environ.update(self._environment)

        load_config(self._topology, "inprocess", self._extra_config)

        container_type = build_container_type(self._modules, self._container_overrides)
        self._container = container_type()

        # Serve before starting the workers. Both VadService and AsrService
        # resolve cltl-storage: URLs over real HTTP against [cltl.backend]
        # storage_url, so the storage endpoint must be live before any event can
        # reach them.
        self._serve()

        logger.info("Starting topology %s with modules %s",
                    self._topology.name, [module.key for module in self._modules])
        self._container.start()
        self._started = True

        self._probe = EventProbe(self._container.event_bus)

    def _make_storage_dirs(self) -> None:
        """Create the directories the tier config points the modules at.

        This is the deployment's job, not the application's, and the harness is
        the deployment here. The storage classes now create their own directory
        too (``CachedAudioStorage.__init__`` used to call ``os.makedirs`` on
        ``os.path.dirname(storage_path)``, creating the *parent* of the
        directory it writes into — see tests/slices/test_backend_storage.py),
        so this is no longer load-bearing for audio and images. It is kept
        because it also covers the paths no storage class owns, ``emissor`` and
        the root itself. These names must stay in step with
        config/tier-inprocess.config.
        """
        for name in ("", "audio", "image"):
            (self._storage_dir / name).mkdir(parents=True, exist_ok=True)

    def stop(self) -> None:
        try:
            # Only stop what was actually started. Container stop() methods
            # assume their services are running and raise otherwise.
            if self._started and self._container is not None:
                self._container.stop()
        finally:
            try:
                self._shutdown_server()
            finally:
                self._container = None
                self._probe = None
                self._started = False
                reset_process_state(self._environ_backup)
                leaked = leaked_threads(self._thread_baseline)
                if leaked:
                    logger.warning("Topology %s leaked threads: %s",
                                   self._topology.name, leaked)

    # -- HTTP --------------------------------------------------------------

    def _serve(self) -> None:
        routes = {}
        for module in self._modules:
            for mount, attribute in module.mounts:
                # @singleton cannot hold None, so an absent optional service is
                # False rather than None. Truthiness, not `is not None`.
                service = getattr(self._container, attribute, False)
                if not service:
                    logger.info("Module %s has no %s to mount at %s",
                                module.key, attribute, mount)
                    continue
                app = service.app
                if app is None:
                    continue
                routes[mount] = app

        if not routes:
            logger.debug("Topology %s serves no HTTP endpoints", self._topology.name)
            return

        dispatcher = DispatcherMiddleware(flask.Flask("cltl-integration"), routes)
        self._server = make_server("127.0.0.1", self._port, dispatcher, threaded=True)
        self._server_thread = threading.Thread(
            target=self._server.serve_forever,
            name=f"integration-http-{self._topology.name}",
            daemon=True)
        self._server_thread.start()
        logger.info("Serving %s at %s", sorted(routes), self.base_url)

    def _shutdown_server(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._server_thread is not None:
            self._server_thread.join(timeout=5)
        self._server = None
        self._server_thread = None

    # -- Runner protocol ---------------------------------------------------

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    def url(self, module_key: str) -> str:
        for module in self._modules:
            if module.key != module_key:
                continue
            if not module.mounts:
                raise KeyError(f"module {module_key!r} serves no HTTP endpoint")
            return self.base_url + module.mounts[0][0]
        raise KeyError(
            f"module {module_key!r} is not in topology {self._topology.name!r} "
            f"({[m.key for m in self._modules]})")

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

    @property
    def storage_path(self) -> Path:
        return self._storage_dir

    @property
    def container(self):
        return self._container
