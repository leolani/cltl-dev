"""Tier 2, N+1 stacks: one shared server and several tenant deployments.

::

    tenant-a project            server project           tenant-b project
    --------------------        --------------------     --------------------
    cltl-context          --->  cltl-eliza        <---    cltl-context
    cltl-chat-ui                rabbitmq                  cltl-chat-ui
    cltl-emissor-data                                     cltl-emissor-data
      tenant: tenant-a            tenant: (empty)           tenant: tenant-b

Where the client/server split cuts one system in two along a network, this cuts
it along a *routing key*. All three projects share one broker and one exchange;
what keeps them apart is ``[cltl.event.kombu] tenant``:

* a **tenanted** bus publishes on ``<topic>.<tenant>`` and binds
  ``<topic>.<tenant>``, so it can neither reach nor be reached by another tenant;
* an **untenanted** bus binds ``<topic>.#``, which in RabbitMQ matches zero or
  more words and therefore every tenant — and it publishes on the *event's*
  tenant rather than its own
  (``cltl.combot.infra.event.kombu.KombuEventBus.publish``).

Those two rules are what make a shared server work at all. A tenant's utterance
reaches the one cltl-eliza in the deployment; eliza's reply is built with
``source=event``, which copies the tenant across (``Event.with_source``); the
untenanted bus routes that reply on the tenant it is carrying, and it lands back
in the one tenant that asked.

The ordering is ``SplitRunner``'s, for its reasons and one more. The server is a
:class:`ComposeRunner` — it owns the broker, this process's configuration and the
untenanted probe that can see every tenant; the tenants are plain
:class:`ComposeStack` s, because only one ``ComposeRunner`` may exist at a time
(``LocalConfigurationContainer`` keeps its configuration in a class attribute, so
a second ``load_configuration`` replaces the first rather than adding to it).

To *act* as a tenant, the test process needs a tenanted bus of its own: an
untenanted publish lands on the bare ``<topic>`` key, which no tenant binds, so
without one a test could not so much as open a tenant's scenario. That is what
:class:`TenantView` carries, and it is why the view exposes the same three things
a runner does — ``event_bus``, ``probe`` and ``url`` — so that
``drivers.conversation.Conversation`` drives a tenant unchanged.
"""
import functools
import logging
from collections import Counter
from pathlib import Path
from typing import Dict, Mapping, Optional, Tuple

from cltl.combot.infra.config import ConfigurationManager
from cltl.combot.infra.event import EventBus
from cltl.combot.infra.event.kombu import KombuEventBus
from kombu.serialization import register

from cltl_integration.runner.api import EventProbe
from cltl_integration.runner.compose import (BROKER_PASSWORD, BROKER_USER,
                                             STARTUP_TIMEOUT, ComposeError,
                                             ComposeRunner, ComposeStack,
                                             missing_images)
from cltl_integration.serialization import (deserializer, register_event_types,
                                            serializer)
from cltl_integration.topology import TenantDeployment, Topology

logger = logging.getLogger(__name__)

HOST_GATEWAY = "host.docker.internal"

SERIALIZER = "cltl-json"

# The topics a tenant deployment must be listening on before a test may drive it.
# `up --wait` gates on /health, which a module serves once its container has
# started — but KombuEventBus.subscribe returns before RabbitMQ has bound the
# queue, so an init intention published into that window is dropped and the
# conversation waits for a greeting that was never triggered.
TENANT_READY_TOPICS = ("cltl.topic.intention", "cltl.topic.text_in")


class _TenantConfigurationManager(ConfigurationManager):
    """The one section :class:`KombuEventBus` reads, and nothing else.

    A plain dict satisfies what it asks of a ``Configuration``: ``config.get(key)``
    and ``'tenant' in config``. Going through ``LocalConfigurationContainer`` is
    not an option — it holds a single configuration for the whole process, and
    that one belongs to the untenanted server runner.
    """

    def __init__(self, sections: Mapping[str, Mapping[str, str]]):
        self._sections = dict(sections)

    def has_config(self, name: str) -> bool:
        return name in self._sections

    def get_config(self, name: str, callback=None) -> Mapping[str, str]:
        if name not in self._sections:
            raise ValueError(f"No configuration for {name}")

        return self._sections[name]


def tenant_event_bus(config_manager, tenant: str) -> KombuEventBus:
    """A bus on the deployment's broker, scoped to one tenant.

    Broker address, exchange and compression are read from the configuration this
    process has already loaded rather than restated, so there is one place where
    they live and a tenanted bus cannot drift onto a different broker than the
    probe it is compared against. Only ``tenant`` is overridden.

    The serializer is registered here rather than relied upon. ``KombuEventBus``
    takes a serializer *name* and fails opaquely if kombu's registry does not hold
    it; ``KombuEventBusContainer`` registers this name as a side effect of building
    its own bus, with exactly the two functions used below
    (``HarnessInfraContainer.event_bus_serializer``), so registering them again is
    a no-op in effect and makes this function independent of what has been built
    before it.
    """
    register_event_types()
    register(SERIALIZER, serializer, deserializer,
             content_type='application/json', content_encoding='utf-8')

    kombu_config = config_manager.get_config("cltl.event.kombu")

    return KombuEventBus(SERIALIZER, _TenantConfigurationManager({
        "cltl.event.kombu": {
            "server": kombu_config.get("server"),
            "exchange": kombu_config.get("exchange"),
            "compression": kombu_config.get("compression"),
            "tenant": tenant,
        }}))


class TenantView:
    """One tenant, as the test process sees it: its stack and its own bus.

    Exposes what ``drivers.conversation.Conversation`` needs — ``event_bus``,
    ``probe`` and ``url`` — so a conversation held inside a tenant is driven with
    the same driver every other pipeline test uses. Both the bus and the probe are
    tenanted, which is the point of each: the bus can address the tenant's modules,
    and the probe *cannot* observe any other tenant, by construction rather than by
    filtering after the fact.
    """

    def __init__(self, tenant: str, stack: ComposeStack,
                 event_bus: KombuEventBus, probe: EventProbe):
        self._tenant = tenant
        self._stack = stack
        self._event_bus = event_bus
        self._probe = probe

    @property
    def tenant(self) -> str:
        return self._tenant

    @property
    def stack(self) -> ComposeStack:
        return self._stack

    @property
    def event_bus(self) -> EventBus:
        return self._event_bus

    @property
    def probe(self) -> EventProbe:
        return self._probe

    @property
    def topology(self) -> Topology:
        return self._stack.topology

    @property
    def storage_path(self) -> Path:
        return self._stack.storage_path

    def url(self, module_key: str) -> str:
        return self._stack.url(module_key)

    def base_url(self, module_key: str) -> str:
        return self._stack.base_url(module_key)

    def logs(self) -> str:
        return self._stack.logs()

    def close(self) -> None:
        """Close the tenanted bus. Must happen before the broker goes down.

        ``ConsumerMixin.run`` treats a lost connection as something to retry, for
        ever. A bus still open when the server project is torn down leaves a
        consumer thread reconnecting in a loop for the rest of the session, and
        the log it writes belongs to no test.
        """
        try:
            self._event_bus.close()
        except Exception:
            logger.exception("Failed to close the bus for tenant %s", self._tenant)

    def __repr__(self) -> str:
        return f"<TenantView {self._tenant} {self._stack.project}>"


class TenantRunner:
    """A :class:`TenantDeployment` as one server project and N tenant projects.

    Use as a context manager. Delegates to the server for everything untenanted —
    the bus, the probe that sees every tenant, the configuration — and hands out a
    :class:`TenantView` per tenant for everything that is not.
    """

    def __init__(self, deployment: TenantDeployment, storage_dir: Path,
                 image_tag: str = "latest",
                 server_environment: Optional[Mapping[str, str]] = None,
                 tenant_environment: Optional[Mapping[str, Mapping[str, str]]] = None,
                 timeout: float = STARTUP_TIMEOUT):
        self._deployment = deployment
        self._root = Path(storage_dir).resolve()
        self._image_tag = image_tag
        self._server_environment = dict(server_environment or {})
        self._tenant_environment = {tenant: dict(env) for tenant, env
                                    in (tenant_environment or {}).items()}
        self._timeout = timeout

        self._server: Optional[ComposeRunner] = None
        self._stacks: Dict[str, ComposeStack] = {}
        self._views: Dict[str, TenantView] = {}
        self._deployment_bindings: Mapping[str, int] = {}

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "TenantRunner":
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()

    def start(self) -> "TenantRunner":
        try:
            self._start()
        except BaseException:
            self.stop()
            raise

        return self

    def _start(self) -> None:
        self._check_images()

        server_env = dict(self._server_environment)
        # The shared half is untenanted: it binds `<topic>.#` and serves everyone.
        server_env.setdefault("CLTL_TENANT", "")
        self._server = ComposeRunner(
            self._deployment.server, self._root / "server",
            image_tag=self._image_tag, environment=server_env, timeout=self._timeout)
        self._server.start()

        # Only now is the broker's published port known, and it is what every
        # tenant stack is configured against.
        for tenant in self._deployment.tenants:
            tenant_env = dict(self._tenant_environment.get(tenant, {}))
            tenant_env.setdefault("CLTL_CONTAINER_AMQP_URL", self.container_amqp_url)
            # No cltl-backend anywhere in this deployment, so the compose file's
            # default would point every module at a `backend` host that does not
            # resolve. Nothing in the text pipeline fetches from it, but a name
            # that cannot be resolved is a worse thing to leave lying around than
            # an empty string that is obviously unset.
            tenant_env.setdefault("CLTL_CONTAINER_STORAGE_URL", "")
            # Assigned last and not defaulted: the tenant id is what the stack
            # *is*, so a caller's tenant_environment may configure it but must not
            # be able to rename it. ComposeStack._compose_env keeps the value from
            # being inherited from os.environ, where the server runner has already
            # written its own; between them, every stack states its tenant and
            # none picks one up. A tenant that came up as the server would leave
            # the isolation under test absent, with nothing logged.
            tenant_env["CLTL_TENANT"] = tenant
            stack = ComposeStack(
                self._deployment.tenant, self._root / tenant,
                image_tag=self._image_tag, environment=tenant_env,
                timeout=self._timeout, broker=False)
            stack.start()
            self._stacks[tenant] = stack

        # Taken after every stack is up and before any probe subscribes, so that
        # an assertion about what the *deployment* routes on is not answered
        # partly by the test process's own queues.
        self._await_tenant_bindings()
        self._deployment_bindings = Counter(self.server.binding_counts())

        for tenant, stack in self._stacks.items():
            bus = tenant_event_bus(self.server.config_manager, tenant)
            probe = EventProbe(bus, readiness=functools.partial(
                self.server.binding_readiness, tenant=tenant))
            self._views[tenant] = TenantView(tenant, stack, bus, probe)

    def _check_images(self) -> None:
        """Both halves, before either is up — see SplitRunner._check_images."""
        missing = set()
        for topology in self._deployment.topologies():
            missing.update(missing_images(topology, self._image_tag))
        if missing:
            raise ComposeError(
                f"{self._deployment.name}: images not present: "
                f"{', '.join(sorted(missing))}. Build them with "
                f"`make docker-ghcr-build`, or see integration/README.md.")

    def _await_tenant_bindings(self) -> None:
        """Wait until every tenant's modules are actually bound on the broker.

        ``ComposeRunner`` closes this window for its own probe and for nothing
        else. Here the containers are the subscribers that matter: a test drives a
        tenant the moment ``start()`` returns, and the tenant that came up last has
        had the least time.
        """
        for tenant in self._deployment.tenants:
            # An all-zero baseline turns the increment check into a presence
            # check, which is what is wanted here: these queues are bound by
            # containers that started before this call, not by a subscribe it
            # wraps.
            self.server.await_bindings(TENANT_READY_TOPICS, {}, tenant)

    def stop(self) -> None:
        """Strict reverse of start: buses, then tenants, then the server.

        The buses go first because they are connections to a broker that the last
        step tears down, and each stage is in its own ``finally`` because a failure
        to close one of them must not leave containers running.
        """
        try:
            for view in self._views.values():
                view.close()
        finally:
            self._views = {}
            try:
                for tenant in reversed(self._deployment.tenants):
                    stack = self._stacks.pop(tenant, None)
                    if stack is not None:
                        stack.stop()
            finally:
                self._stacks = {}
                try:
                    if self._server is not None:
                        self._server.stop()
                finally:
                    self._server = None

    # -- the address of the broker, seen from a tenant container -----------

    @property
    def container_amqp_url(self) -> str:
        return (f"amqp://{BROKER_USER}:{BROKER_PASSWORD}@{HOST_GATEWAY}:"
                f"{self.server.port('rabbitmq', 5672)}/")

    # -- the halves --------------------------------------------------------

    @property
    def server(self) -> ComposeRunner:
        if self._server is None:
            raise RuntimeError("tenant runner is not started")
        return self._server

    @property
    def tenants(self) -> Tuple[str, ...]:
        return self._deployment.tenants

    def tenant(self, tenant: str) -> TenantView:
        if tenant not in self._views:
            raise KeyError(
                f"{tenant!r} is not a tenant of {self._deployment.name!r}: "
                f"{list(self._deployment.tenants)}")
        return self._views[tenant]

    def stack(self, tenant: str) -> ComposeStack:
        return self.tenant(tenant).stack

    @property
    def deployment(self) -> TenantDeployment:
        return self._deployment

    @property
    def deployment_bindings(self) -> Mapping[str, int]:
        """What the deployment binds, snapshotted before any probe subscribed.

        The routing keys are the whole mechanism, so this is the one assertion
        that looks at it directly rather than at what came out the other end.
        """
        return self._deployment_bindings

    # -- Runner protocol ---------------------------------------------------

    def url(self, module_key: str) -> str:
        """Resolve a module's HTTP mount on the server half.

        A tenant module is refused rather than guessed at: there are N of it, one
        per tenant, and every one of them answers.
        """
        if module_key in self._deployment.tenant.modules:
            raise KeyError(
                f"module {module_key!r} runs in every tenant of "
                f"{self._deployment.name!r}. Say which one: "
                f"runner.tenant(<tenant>).url({module_key!r}).")

        return self.server.url(module_key)

    def base_url(self, module_key: str) -> str:
        if module_key in self._deployment.tenant.modules:
            raise KeyError(
                f"module {module_key!r} runs in every tenant of "
                f"{self._deployment.name!r}. Say which one: "
                f"runner.tenant(<tenant>).base_url({module_key!r}).")

        return self.server.base_url(module_key)

    @property
    def probe(self) -> EventProbe:
        """The server's: untenanted, and so the only view that sees every tenant."""
        return self.server.probe

    @property
    def event_bus(self) -> EventBus:
        return self.server.event_bus

    @property
    def config_manager(self):
        return self.server.config_manager

    @property
    def storage_path(self) -> Path:
        """The server's. Each tenant's is `runner.tenant(id).storage_path`."""
        return self.server.storage_path

    @property
    def projects(self) -> Tuple[str, ...]:
        return (self.server.project,
                *(stack.project for stack in self._stacks.values()))
