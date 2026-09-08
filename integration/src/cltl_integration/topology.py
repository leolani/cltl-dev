"""Topologies: a named set of modules plus the configuration that enables them.

A topology is the artefact shared across the three things the harness produces
for a scenario — an in-process test, a container test, and a demo. Adding a
scenario means adding one ``Topology``, not three parallel definitions.
"""
import configparser
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple

from cltl.combot.infra.config.local import LocalConfigurationContainer

from cltl_integration.modules import BY_KEY, MODULES, Module

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"
BASE_CONFIG = CONFIG_DIR / "base.config"
TIER_CONFIG = {
    "inprocess": CONFIG_DIR / "tier-inprocess.config",
    "compose": CONFIG_DIR / "tier-compose.config",
}

# A tenant id becomes one word of an AMQP routing key, so it may not contain a
# separator or a wildcard. See TenantDeployment.
_TENANT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class TopologyError(ValueError):
    """A topology is internally inconsistent, or its configuration would hang."""


@dataclass(frozen=True)
class Topology:
    name: str
    modules: Tuple[str, ...]
    overlay: Optional[str] = None
    """Config overlay filename, relative to ``config/topologies/``."""

    def __post_init__(self):
        unknown = [key for key in self.modules if key not in BY_KEY]
        if unknown:
            raise TopologyError(
                f"{self.name}: unknown module(s) {unknown}; known: {sorted(BY_KEY)}")
        if len(set(self.modules)) != len(self.modules):
            raise TopologyError(f"{self.name}: duplicate modules in {self.modules}")

    def ordered_modules(self) -> Tuple[Module, ...]:
        """The topology's modules in registry (container MRO) order.

        The order a topology is *written* in is irrelevant; only the registry
        order is safe to build a container type from. Remember that services
        start in the reverse of this order — see :mod:`cltl_integration.modules`.
        """
        selected = set(self.modules)
        return tuple(module for module in MODULES if module.key in selected)

    def overlay_path(self) -> Optional[Path]:
        if not self.overlay:
            return None
        path = CONFIG_DIR / "topologies" / self.overlay
        if not path.exists():
            raise TopologyError(f"{self.name}: overlay not found: {path}")
        return path

    def config_files(self, tier: str) -> Tuple[Path, Tuple[Path, ...]]:
        """Return ``(base, additional)`` config paths for *tier*, in merge order.

        ``ConfigParser.read()`` applies later files last, so the ordering is
        base < tier < topology overlay.
        """
        if tier not in TIER_CONFIG:
            raise TopologyError(f"unknown tier {tier!r}; known: {sorted(TIER_CONFIG)}")
        tier_config = TIER_CONFIG[tier]
        if not tier_config.exists():
            raise TopologyError(f"tier config not found: {tier_config}")

        additional = [tier_config]
        overlay = self.overlay_path()
        if overlay:
            additional.append(overlay)

        return BASE_CONFIG, tuple(additional)


@dataclass(frozen=True)
class Deployment:
    """Two stacks that make one system, on two networks.

    The client/server split is not a topology with more modules in it — it is the
    same modules cut in half and deployed apart, which is why it needs its own
    type. What the cut adds, and nothing single-stack can cover:

    * the event bus crosses a machine boundary, so every intention, desire and
      utterance is routed by a broker the client does not own;
    * the client stores no audio. ``[cltl.backend] audio_storage: remote`` sends
      it over HTTP to the server, and the server's cltl-vad and cltl-asr read it
      back from their own side of that same store. A misconfigured URL there
      fails silently in the monolith, where the local fallback happens to be
      correct;
    * the server runs ``storage_main.py`` — ``StorageContainer``, a different DI
      container from the ``BackendContainer`` every other topology exercises.

    Only tier 2 can express it. In one process there is one event bus and one
    resource manager, so a split has no meaning.
    """

    name: str
    server: Topology
    client: Topology

    def __post_init__(self):
        if "backend" not in self.server.modules:
            raise TopologyError(
                f"{self.name}: the server half must run cltl-backend — it is the "
                f"storage endpoint the client uploads to.")
        shared = set(self.server.modules) & set(self.client.modules) - {"backend"}
        if shared:
            raise TopologyError(
                f"{self.name}: {sorted(shared)} appear on both halves. Both bind a "
                f"queue to the same routing key on the same bus, and each queue is "
                f"its own — see _EventBusConsumer in cltl.combot.infra.event.kombu — "
                f"so every event is delivered to both and processed twice.")

    def topologies(self) -> Tuple[Topology, Topology]:
        """Server first, which is also the order the two stacks start in."""
        return self.server, self.client


@dataclass(frozen=True)
class TenantDeployment:
    """One shared server and N deployments of one tenant topology.

    Not a :class:`Deployment` with more halves. A Deployment is one system cut in
    two along a network; this is one *tenant* topology deployed several times over
    the same broker, and what separates the copies is a routing key. Each tenant
    deployment configures ``[cltl.event.kombu] tenant`` with its own id, so its bus
    publishes on ``<topic>.<tenant>`` and binds ``<topic>.<tenant>``; the server
    leaves the setting empty, binds ``<topic>.#`` and therefore serves every
    tenant. A reply built with ``source=event`` carries the asking tenant back out
    (``Event.with_source``), which is what closes the loop.

    The validation rules are the inverse of a Deployment's, and for the same
    underlying reason. A Deployment refuses one module on both halves because both
    would receive every event; here the same module on two *tenants* is the entire
    point, because the two bind different keys and neither matches the other's
    traffic. What must not be shared is a module across the server/tenant line.

    Only tier 2 can express it. ``SynchronousEventBus`` stamps ``"local"`` on
    anything untenanted and hands every event to every handler, so in one process
    a tenant means nothing.
    """

    name: str
    server: Topology
    tenant: Topology
    tenants: Tuple[str, ...]

    def __post_init__(self):
        if not self.server.modules or not self.tenant.modules:
            raise TopologyError(f"{self.name}: both halves must run something.")
        if len(self.tenants) < 2:
            raise TopologyError(
                f"{self.name}: needs at least two tenants. With one there is nothing "
                f"for the isolation to hold against, and a single tenant talking to a "
                f"shared server is a client/server split with extra configuration.")
        if len(set(self.tenants)) != len(self.tenants):
            raise TopologyError(
                f"{self.name}: duplicate tenant ids in {self.tenants}. Two stacks on "
                f"one id are not two tenants — they bind the same keys and split the "
                f"traffic between them.")
        for tenant in self.tenants:
            if not _TENANT_ID.match(tenant):
                raise TopologyError(
                    f"{self.name}: tenant id {tenant!r} is not a single AMQP routing-key "
                    f"word. KombuEventBus builds `<topic>.<tenant>`, so a '.', '*' or "
                    f"'#' quietly changes which keys that pattern matches.")
        shared = set(self.server.modules) & set(self.tenant.modules)
        if shared:
            raise TopologyError(
                f"{self.name}: {sorted(shared)} run both on the server and in every "
                f"tenant. The server's `<topic>.#` and the tenant's `<topic>.<tenant>` "
                f"both match the tenant's events, so each one is processed twice.")

    def topologies(self) -> Tuple[Topology, Topology]:
        """Server first, which is also the order the stacks start in.

        The tenant topology appears once however many times it is deployed: the
        deployments differ only in an environment variable, so there is one set of
        images to check and one configuration to write.
        """
        return self.server, self.tenant


def load_config(topology: Topology, tier: str,
                extra_config: Sequence[Path] = ()) -> None:
    """Load the merged configuration for *topology* into the process.

    Setting the environment variables the tier config interpolates
    (``CLTL_HTTP_BASE``, ``CLTL_STORAGE_DIR``) is the caller's job, and they must
    stay set for the topology's lifetime: ``EnvInterpolation`` substitutes
    ``os.environ`` when a value is *read*, not when the file is parsed, and
    services read their configuration lazily.

    ``LocalConfigurationContainer`` is used rather than the K8 subclass that
    ``InfraContainer`` mixes in: the latter appends to the caller's list in place
    and logs a warning about a missing /cltl_k8_config on every load.
    """
    base, additional = topology.config_files(tier)
    # A fresh list every call - see the aliasing note above.
    files = [str(path) for path in additional] + [str(path) for path in extra_config]
    LocalConfigurationContainer.load_configuration(str(base), files)


def merged_config(topology: Topology, tier: str) -> configparser.ConfigParser:
    """The topology's three config layers, merged, without a running container.

    Interpolation is off on purpose: these files are full of ``$CLTL_*``
    placeholders that only mean something once a runner has set them, and
    resolving them here would answer with this process's view of a network the
    containers are not on. Read keys that are decided by the files themselves —
    which topic is enabled, which implementation is chosen — never a URL.
    """
    base, additional = topology.config_files(tier)
    parser = configparser.ConfigParser(interpolation=None)
    parser.read([str(base)] + [str(path) for path in additional])

    return parser


def needs_microphone(topology: Topology, tier: str) -> bool:
    """Whether this topology's backend will try to open an audio stream.

    ``BackendService`` starts its recording thread when ``[cltl.backend.mic]
    topic`` is set, and that thread opens a streaming GET against
    ``server_audio_url``. With nothing listening there it retries in a tight
    loop and logs a connection error per attempt, so a demo has to start a stub
    microphone before the topology rather than after.
    """
    if "backend" not in topology.modules:
        return False

    return bool(merged_config(topology, tier).get("cltl.backend.mic", "topic", fallback=""))


def needs_speaker(topology: Topology, tier: str) -> bool:
    """Whether this topology's backend will try to speak to a remote loudspeaker.

    ``BackendService`` starts its TTS worker when ``[cltl.backend.tts] topic`` is
    set, and ``AnimatedRemoteTextOutput`` POSTs every reply to
    ``[cltl.backend.text_output] remote_url``. With nothing listening there the
    failure is invisible — ``SynchronizedTextToSpeech.say`` catches it and only
    logs — so a demo has to start a stub loudspeaker rather than let the agent
    be silently mute.
    """
    if "backend" not in topology.modules:
        return False

    return bool(merged_config(topology, tier).get("cltl.backend.tts", "topic", fallback=""))


ELIZA = Topology(name="eliza", modules=("eliza",), overlay="eliza.config")

CONTEXT = Topology(name="context", modules=("context",), overlay="context.config")

ELIZA_CHATUI = Topology(
    name="eliza_chatui",
    modules=("eliza", "chatui"),
    overlay="eliza_chatui.config",
)

EMISSOR = Topology(name="emissor", modules=("emissor",), overlay="emissor.config")

BACKEND = Topology(name="backend", modules=("backend",), overlay="backend.config")

BACKEND_VAD = Topology(
    name="backend_vad",
    modules=("backend", "vad"),
    overlay="backend_vad.config",
)

VAD_ASR = Topology(
    name="vad_asr",
    modules=("backend", "asr"),
    overlay="vad_asr.config",
)

AUDIO_PIPELINE = Topology(
    name="audio_pipeline",
    modules=("backend", "vad", "asr", "eliza", "chatui"),
    overlay="audio_pipeline.config",
)

TEXT_PIPELINE = Topology(
    name="text_pipeline",
    modules=("eliza", "context", "chatui"),
    overlay="text_pipeline.config",
)

# The agent's reply, spoken. Two topologies rather than one, because the
# microphone is not a detail here: the speaker and the microphone share an audio
# resource, and the whole point of SynchronizedTextToSpeech is what happens when
# both want it. See tests/slices/test_backend_tts.py.
BACKEND_TTS = Topology(
    name="backend_tts",
    modules=("backend", "eliza"),
    overlay="backend_tts.config",
)

BACKEND_TTS_MIC = Topology(
    name="backend_tts_mic",
    modules=("backend", "eliza"),
    overlay="backend_tts_mic.config",
)

# audio_pipeline plus the BDI handshake: the shape a robot actually runs, and
# the only topology where consent is given by voice rather than over HTTP.
# audio_pipeline is deliberately left as it is — adding cltl-context to it would
# put the init greeting on text_out and break its tests for the wrong reason.
SPOKEN_PIPELINE = Topology(
    name="spoken_pipeline",
    modules=("backend", "vad", "asr", "context", "eliza", "chatui"),
    overlay="spoken_pipeline.config",
)

CHATUI_IMAGE = Topology(
    name="chatui_image",
    modules=("chatui", "emissor", "backend"),
    overlay="chatui_image.config",
)

CHATUI_MONITORING = Topology(
    name="chatui_monitoring",
    modules=("chatui", "monitoring", "backend"),
    overlay="chatui_monitoring.config",
)

# -- client/server split ----------------------------------------------------
#
# Four topologies rather than two, because text and audio need different modules
# on the server: the text split has nothing to transcribe, and pulling Whisper
# into it would cost minutes for coverage the audio split already provides.
# The client half is the same shape in both — the near side of the split is
# where a person stands — apart from the microphone.

CSPLIT_SERVER = Topology(
    name="csplit_server",
    modules=("backend", "emissor", "eliza"),
    overlay="csplit_server.config",
)

CSPLIT_CLIENT = Topology(
    name="csplit_client",
    modules=("backend", "context", "chatui"),
    overlay="csplit_client.config",
)

CSPLIT = Deployment(name="csplit", server=CSPLIT_SERVER, client=CSPLIT_CLIENT)

CSPLIT_AUDIO_SERVER = Topology(
    name="csplit_audio_server",
    modules=("backend", "emissor", "vad", "asr", "eliza"),
    overlay="csplit_audio_server.config",
)

CSPLIT_AUDIO_CLIENT = Topology(
    name="csplit_audio_client",
    modules=("backend", "context", "chatui"),
    overlay="csplit_audio_client.config",
)

CSPLIT_AUDIO = Deployment(
    name="csplit_audio",
    server=CSPLIT_AUDIO_SERVER,
    client=CSPLIT_AUDIO_CLIENT,
)

MULTITENANT_SERVER = Topology(
    name="multitenant_server",
    modules=("eliza",),
    overlay="multitenant_server.config",
)

MULTITENANT_TENANT = Topology(
    name="multitenant_tenant",
    modules=("context", "chatui", "emissor"),
    overlay="multitenant_tenant.config",
)

MULTITENANT = TenantDeployment(
    name="multitenant",
    server=MULTITENANT_SERVER,
    tenant=MULTITENANT_TENANT,
    tenants=("tenant-a", "tenant-b"),
)

TOPOLOGIES: Dict[str, Topology] = {
    topology.name: topology
    for topology in (ELIZA, CONTEXT, ELIZA_CHATUI, EMISSOR, BACKEND,
                     BACKEND_VAD, VAD_ASR, AUDIO_PIPELINE, TEXT_PIPELINE,
                     BACKEND_TTS, BACKEND_TTS_MIC, SPOKEN_PIPELINE,
                     CHATUI_IMAGE, CHATUI_MONITORING,
                     CSPLIT_SERVER, CSPLIT_CLIENT,
                     CSPLIT_AUDIO_SERVER, CSPLIT_AUDIO_CLIENT,
                     MULTITENANT_SERVER, MULTITENANT_TENANT)
}

DEPLOYMENTS: Dict[str, Deployment] = {
    deployment.name: deployment for deployment in (CSPLIT, CSPLIT_AUDIO)
}

TENANT_DEPLOYMENTS: Dict[str, TenantDeployment] = {
    deployment.name: deployment for deployment in (MULTITENANT,)
}
