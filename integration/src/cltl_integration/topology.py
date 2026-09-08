"""Topologies: a named set of modules plus the configuration that enables them.

A topology is the artefact shared across the three things the harness produces
for a scenario — an in-process test, a container test, and a demo. Adding a
scenario means adding one ``Topology``, not three parallel definitions.
"""
import configparser
import logging
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
                f"{self.name}: {sorted(shared)} appear on both halves. Two instances "
                f"of one module on the same bus both consume its topics, so events "
                f"are shared out between them rather than delivered to each.")

    def topologies(self) -> Tuple[Topology, Topology]:
        """Server first, which is also the order the two stacks start in."""
        return self.server, self.client


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


def validate_config(config_manager, topology: Topology) -> None:
    """Reject configurations that are known to hang rather than fail.

    Reads through a live ``ConfigurationManager``, so what is checked is the
    interpolated value a service would actually receive.
    """
    if "backend" not in topology.modules:
        return

    _check_audio_lock(topology,
                      config_manager.get_config("cltl.backend.tts").get("topic"),
                      config_manager.get_config("cltl.backend.mic").get("topic"))


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


def validate_topology(topology: Topology, tier: str) -> None:
    """The same rule as :func:`validate_config`, read straight from the files.

    For a stack this process does not attach to — the client half of a
    client/server split loads its configuration inside its own containers, so
    there is no ``ConfigurationManager`` here to ask.
    """
    if "backend" not in topology.modules:
        return

    parser = merged_config(topology, tier)
    _check_audio_lock(topology,
                      parser.get("cltl.backend.tts", "topic", fallback=""),
                      parser.get("cltl.backend.mic", "topic", fallback=""))


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


def _check_audio_lock(topology: Topology, tts_topic: str, mic_topic: str) -> None:
    """The one configuration that hangs instead of failing.

    ``SynchronizedTextToSpeech.say`` acquires a write lock on the audio resource
    with ``timeout=-1``, which ``ThreadedResourceManager._await_resource`` turns
    into ``event.wait(timeout=None)``. The only thing that ever provides that
    resource is ``SynchronizedMicrophone.start``. So a topology that routes
    text_out to TTS without running the microphone blocks the backend's worker
    forever on the first agent reply — with no error, no timeout and no log.

    This is the exact combination ``app/docker-app``'s test ``custom.config``
    sets up, so it is not hypothetical.
    """
    if not tts_topic or mic_topic:
        return

    raise TopologyError(
        f"{topology.name}: [cltl.backend.tts] topic is set to {tts_topic!r} but "
        f"[cltl.backend.mic] topic is empty. SynchronizedTextToSpeech.say would "
        f"block forever waiting for the audio resource that only the microphone "
        f"provides. Either enable the microphone or clear the TTS topic.")


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

TOPOLOGIES: Dict[str, Topology] = {
    topology.name: topology
    for topology in (ELIZA, CONTEXT, ELIZA_CHATUI, EMISSOR, BACKEND,
                     BACKEND_VAD, VAD_ASR, AUDIO_PIPELINE, TEXT_PIPELINE,
                     CHATUI_IMAGE, CHATUI_MONITORING,
                     CSPLIT_SERVER, CSPLIT_CLIENT,
                     CSPLIT_AUDIO_SERVER, CSPLIT_AUDIO_CLIENT)
}

DEPLOYMENTS: Dict[str, Deployment] = {
    deployment.name: deployment for deployment in (CSPLIT, CSPLIT_AUDIO)
}
