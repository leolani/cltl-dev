"""Tier 2, two stacks: the client/server split deployment.

The platform is meant to be deployable with the person-facing half on a robot
and the expensive half on a server. That is not a topology with different
modules in it — it is the same modules cut in two and run apart, and the cut
changes what the modules have to get right:

* **The bus crosses a machine boundary.** cltl-context runs on the client and
  cltl-eliza on the server, so the whole BDI handshake — init intention,
  greeting, ``initialized`` desire, handover to ``eliza`` — is routed by a broker
  the client does not own and cannot see the inside of.
* **The client stores nothing.** ``[cltl.backend] audio_storage: remote`` sends
  every recording over HTTP to the server, and the server's cltl-vad and cltl-asr
  read it back from their own side of that same store. In a monolith a wrong
  storage URL still works, because the local fallback happens to be correct.
* **The server runs a different entry point**, ``src/storage_main.py``:
  ``StorageContainer`` without ``BackendContainer`` around it.

Only one of the two stacks may be a :class:`ComposeRunner`. The test process's
configuration lives in a class attribute of ``LocalConfigurationContainer``, so
a second ``load_configuration`` replaces the first rather than adding to it. The
server owns it, because the server owns the broker this process probes; the
client half is a plain :class:`ComposeStack`, which brings containers up and
otherwise keeps out of the way.

How the two find each other: each compose project gets its own bridge network,
so ``rabbitmq`` and ``backend`` do not resolve across the split any more than
they would across a room. The client reaches the server the way a client on
another machine would — through the host, at the ports the server project
published. ``host.docker.internal`` is the address of that host from inside a
container, supplied by the ``extra_hosts`` entry every service in
``compose/docker-compose.yml`` carries.
"""
from pathlib import Path
from typing import Mapping, Optional, Tuple

from cltl.combot.infra.event import EventBus

from cltl_integration.runner.api import EventProbe
from cltl_integration.runner.compose import (BROKER_PASSWORD, BROKER_USER,
                                             STARTUP_TIMEOUT, ComposeError,
                                             ComposeRunner, ComposeStack,
                                             missing_images)
from cltl_integration.topology import Deployment

HOST_GATEWAY = "host.docker.internal"

# StorageContainer rather than BackendContainer: the server half has no devices.
SERVER_ENTRY_POINT = "src/storage_main.py"
# The client half does, so it runs the image's own default.
CLIENT_ENTRY_POINT = "src/main.py"


class SplitRunner:
    """A :class:`Deployment` as two compose projects; use as a context manager.

    Delegates to the server runner for everything about the bus, and resolves
    URLs against whichever half actually runs the module.
    """

    def __init__(self, deployment: Deployment, storage_dir: Path,
                 image_tag: str = "latest",
                 server_environment: Optional[Mapping[str, str]] = None,
                 client_environment: Optional[Mapping[str, str]] = None,
                 timeout: float = STARTUP_TIMEOUT):
        self._deployment = deployment
        self._root = Path(storage_dir).resolve()
        self._image_tag = image_tag
        self._server_environment = dict(server_environment or {})
        self._client_environment = dict(client_environment or {})
        self._timeout = timeout

        self._server: Optional[ComposeRunner] = None
        self._client: Optional[ComposeStack] = None

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "SplitRunner":
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()

    def start(self) -> "SplitRunner":
        try:
            self._start()
        except BaseException:
            self.stop()
            raise

        return self

    def _start(self) -> None:
        self._check_images()

        server_env = dict(self._server_environment)
        server_env.setdefault("CLTL_BACKEND_MAIN", SERVER_ENTRY_POINT)
        self._server = ComposeRunner(
            self._deployment.server, self._root / "server",
            image_tag=self._image_tag, environment=server_env, timeout=self._timeout)
        self._server.start()

        # Only now are the server's ports known, and they are what the client
        # stack is configured against — which is why the two cannot be started
        # in parallel however tempting the wall clock makes it.
        client_env = dict(self._client_environment)
        client_env.setdefault("CLTL_CONTAINER_AMQP_URL", self.container_amqp_url)
        client_env.setdefault("CLTL_CONTAINER_STORAGE_URL", self.container_storage_url)
        # Stated rather than left to the compose file's default, because the
        # server runner has already put its own value in os.environ — which is
        # where ComposeStack._compose_env starts from. Inherited, it gives the
        # client a backend with no microphone: every other test still passes and
        # the spoken one waits 300 seconds for audio that is never recorded.
        client_env.setdefault("CLTL_BACKEND_MAIN", CLIENT_ENTRY_POINT)
        self._client = ComposeStack(
            self._deployment.client, self._root / "client",
            image_tag=self._image_tag, environment=client_env,
            timeout=self._timeout, broker=False)
        self._client.start()

    def _check_images(self) -> None:
        """Both halves, before either is up.

        ``ComposeStack`` checks its own images, but by the time the client got
        that far the server would already be running and would have to be torn
        down again — three minutes to report a missing image.
        """
        missing = set()
        for topology in self._deployment.topologies():
            missing.update(missing_images(topology, self._image_tag))
        if missing:
            raise ComposeError(
                f"{self._deployment.name}: images not present: "
                f"{', '.join(sorted(missing))}. Build them with "
                f"`make docker-ghcr-build`, or see integration/README.md.")

    def stop(self) -> None:
        """Client first: it is the half that dials in."""
        try:
            if self._client is not None:
                self._client.stop()
        finally:
            self._client = None
            try:
                if self._server is not None:
                    self._server.stop()
            finally:
                self._server = None

    # -- the address of the server, seen from a client container -----------

    @property
    def container_amqp_url(self) -> str:
        return (f"amqp://{BROKER_USER}:{BROKER_PASSWORD}@{HOST_GATEWAY}:"
                f"{self.server.port('rabbitmq', 5672)}/")

    @property
    def container_storage_url(self) -> str:
        return f"http://{HOST_GATEWAY}:{self.server.port('backend')}/storage/"

    # -- the two halves ----------------------------------------------------

    @property
    def server(self) -> ComposeRunner:
        if self._server is None:
            raise RuntimeError("split runner is not started")
        return self._server

    @property
    def client(self) -> ComposeStack:
        if self._client is None:
            raise RuntimeError("split runner is not started")
        return self._client

    @property
    def deployment(self) -> Deployment:
        return self._deployment

    # -- Runner protocol ---------------------------------------------------

    def url(self, module_key: str) -> str:
        """Resolve a module's HTTP mount on whichever half runs it.

        A module on both halves is refused rather than guessed at. cltl-backend
        is the case that matters — the client's ``/storage`` is a proxy for the
        server's, and a test that mixes them up passes for the wrong reason.
        """
        return self._resolve(module_key).url(module_key)

    def base_url(self, module_key: str) -> str:
        return self._resolve(module_key).base_url(module_key)

    def _resolve(self, module_key: str) -> ComposeStack:
        halves = [half for half in (self.client, self.server)
                  if module_key in half.topology.modules]
        if not halves:
            raise KeyError(
                f"module {module_key!r} is in neither half of "
                f"{self._deployment.name!r}")
        if len(halves) > 1:
            raise KeyError(
                f"module {module_key!r} runs on both halves of "
                f"{self._deployment.name!r}. Say which one: "
                f"runner.server.url({module_key!r}) or "
                f"runner.client.url({module_key!r}).")

        return halves[0]

    @property
    def probe(self) -> EventProbe:
        return self.server.probe

    @property
    def event_bus(self) -> EventBus:
        return self.server.event_bus

    @property
    def config_manager(self):
        return self.server.config_manager

    @property
    def storage_path(self) -> Path:
        """The server's. The client half is deliberately not a storage location."""
        return self.server.storage_path

    @property
    def projects(self) -> Tuple[str, str]:
        return self.server.project, self.client.project
