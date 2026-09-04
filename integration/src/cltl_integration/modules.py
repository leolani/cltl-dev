"""The registry of platform modules — one source of truth for the harness.

Today the same information is transcribed by hand in four places that have
already drifted apart: the container MRO in ``app/py-app/app.py``, the services
in ``app/docker-app/docker-compose.yml``, ``app/requirements.txt``, and each
component's ``project_dependencies``. Everything the harness needs — the
in-process MRO, the compose service selection, the venv requirements — is
derived from this list instead.

Ordering is the container MRO order used by ``app/py-app/app.py``. Two things
about it are load-bearing and easy to get wrong:

* The event-bus override must be the **first** base when a container type is
  synthesised. ``KombuEventBusContainer.event_bus`` is a plain (non-singleton)
  property, so anything later in the MRO wins by default.
* **Start order is the reverse of this list.** Every container calls
  ``super().start()`` *before* starting its own services, so the tuple
  ``(Infra, Eliza, Context, ChatUI, ASR, VAD, Emissor, Backend)`` actually starts
  Backend first and Eliza last. Nothing is order-sensitive today, but do not
  reorder this list into topic-flow order under the illusion that it is start
  order.
"""
from dataclasses import dataclass
from typing import Dict, Tuple


@dataclass(frozen=True)
class Module:
    """One platform component, in both its in-process and containerised form."""

    key: str
    """Short name used by topologies, compose services and test ids."""

    container: str
    """DI container, as ``"module.path:ClassName"``."""

    image: str
    """Container image built by the component's ``docker-ghcr-build`` target."""

    requirement: str
    """pip requirement, with the extras this harness needs."""

    workdir: str
    """The image's WORKDIR, where its ``config/`` and ``storage/`` are mounted.

    Not derivable from the key: cltl-chat-ui builds to ``/cltl-chatui``, without
    the second hyphen. Every image loads its configuration relative to the
    working directory (``config/default.config`` plus ``config/custom.config``),
    so a wrong path here means a container that starts, finds no configuration
    and silently falls back to the legacy defaults baked into its own
    ``config/default.config``.
    """

    mounts: Tuple[Tuple[str, str], ...] = ()
    """HTTP mounts as ``(url_prefix, container_attribute)`` pairs.

    Only backend (storage), chat-ui and emissor-data serve anything; vad, asr and
    eliza return ``None`` from ``service.app``. The prefixes match the ones
    ``app/py-app/app.py`` mounts under its ``DispatcherMiddleware`` and the paths
    the components' own configs point at, so they are not free to change.

    The attribute may resolve to ``False`` rather than a service: ``@singleton``
    cannot hold ``None``, so containers return ``False`` for an intentionally
    absent optional service. Always test truthiness, never ``is not None``.
    """

    @property
    def http(self) -> bool:
        return bool(self.mounts)

    @property
    def service(self) -> str:
        """Compose service name, which is also its DNS name on the network."""
        return self.key

    @property
    def config_dir(self) -> str:
        return f"{self.workdir}/config"

    @property
    def storage_dir(self) -> str:
        return f"{self.workdir}/storage"

    def container_ref(self) -> Tuple[str, str]:
        """Split ``container`` into ``(module_path, class_name)``."""
        module_path, separator, class_name = self.container.partition(":")
        if not separator:
            raise ValueError(
                f"{self.key}: container must be 'module.path:ClassName', got {self.container!r}")
        return module_path, class_name


MODULES: Tuple[Module, ...] = (
    Module(
        key="eliza",
        container="cltl_service.eliza.container:ElizaContainer",
        image="ghcr.io/leolani/cltl-eliza",
        requirement="cltl.eliza",
        workdir="/cltl-eliza",
    ),
    Module(
        key="context",
        container="cltl_service.context.container:ContextComponentsContainer",
        image="ghcr.io/leolani/cltl-context",
        requirement="cltl.context[service]",
        workdir="/cltl-context",
    ),
    Module(
        key="chatui",
        container="cltl_service.chatui.container:ChatUIContainer",
        image="ghcr.io/leolani/cltl-chat-ui",
        requirement="cltl.chat-ui",
        workdir="/cltl-chatui",
        mounts=(("/chatui", "chatui_service"),),
    ),
    Module(
        key="asr",
        container="cltl_service.asr.container:ASRContainer",
        image="ghcr.io/leolani/cltl-asr",
        requirement="cltl.asr[service]",
        workdir="/cltl-asr",
    ),
    Module(
        key="vad",
        container="cltl_service.vad.container:VADContainer",
        image="ghcr.io/leolani/cltl-vad",
        requirement="cltl.vad[impl,service]",
        workdir="/cltl-vad",
    ),
    Module(
        key="emissor",
        container="cltl_service.emissordata.container:EmissorStorageContainer",
        image="ghcr.io/leolani/cltl-emissor-data",
        requirement="cltl.emissor-data[service,client]",
        workdir="/cltl-emissor-data",
        mounts=(("/emissor", "emissor_data_service"),),
    ),
    Module(
        key="backend",
        container="cltl_service.backend.backend_container:BackendContainer",
        image="ghcr.io/leolani/cltl-backend",
        requirement="cltl.backend[impl]",
        workdir="/cltl-backend",
        mounts=(("/storage", "storage_service"),),
    ),
)

BY_KEY: Dict[str, Module] = {module.key: module for module in MODULES}
