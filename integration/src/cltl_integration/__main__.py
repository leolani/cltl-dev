"""Run a topology without asserting on it — the demo front end.

    python -m cltl_integration text_pipeline
    python -m cltl_integration audio_pipeline --tier compose --say "I feel sad"
    python -m cltl_integration csplit

Same ``Topology`` and ``Deployment`` objects the tests use, same runners, no
assertions: it starts the modules, opens a scenario, prints the URLs it
discovered and blocks until Ctrl-C. That is the whole point of the design — a
scenario is defined once and gets three front ends (a tier-1 test, a tier-2
test, and this) rather than three parallel definitions that drift.

Kept deliberately thin so it cannot grow into a second application. Everything
here is either argument parsing or something a test fixture already does:
starting a stub microphone, publishing the event that opens a scenario, and
printing where the HTTP mounts ended up.
"""
import argparse
import logging
import shutil
import signal
import sys
import threading
from pathlib import Path
from typing import List, Optional, Sequence

from cltl_integration.drivers import audio
from cltl_integration.drivers.audio import StubAudioServer
from cltl_integration.drivers.camera import StubImageServer
from cltl_integration.drivers.tts import StubTextOutput
from cltl_integration.drivers.bdi import publish_intention
from cltl_integration.drivers.scenario import start_scenario
from cltl_integration.runner.compose import (BROKER_PASSWORD, BROKER_USER,
                                             ComposeError, ComposeRunner)
from cltl_integration.runner.inprocess import InProcessRunner
from cltl_integration.runner.split import SplitRunner
from cltl_integration.runner.tenants import TenantRunner
from cltl_integration.topology import (DEPLOYMENTS, TENANT_DEPLOYMENTS,
                                       TOPOLOGIES, Deployment,
                                       TenantDeployment, Topology,
                                       needs_camera, needs_microphone,
                                       needs_speaker)

logger = logging.getLogger(__name__)

COMPONENT_ROOT = Path(__file__).resolve().parent.parent.parent
DEMO_ROOT = COMPONENT_ROOT / ".demo"

SCENARIO_TOPIC = "cltl.topic.scenario"
INTENTION_TOPIC = "cltl.topic.intention"

# The page a person actually opens, rather than the REST mount the tests drive.
CHAT_PAGE = "/static/chat.html"


def scenarios() -> dict:
    """Topologies and deployments in one namespace, keyed as the CLI takes them.

    A deployment is not a topology, but from a demo's point of view both are
    "a thing you can run", and a caller should not have to know which is which.
    """
    return {**TOPOLOGIES, **DEPLOYMENTS, **TENANT_DEPLOYMENTS}


def resolve(name: str):
    """Look up a scenario, accepting hyphens for underscores.

    ``make demo-text-pipeline`` reads better as a target than
    ``make demo-text_pipeline``, and both should reach the same place.
    """
    available = scenarios()
    for candidate in (name, name.replace("-", "_")):
        if candidate in available:
            return available[candidate]

    raise SystemExit(
        f"unknown scenario {name!r}\n\n{describe()}")


def describe() -> str:
    lines = ["Topologies (--tier inprocess or compose):"]
    lines += [f"  {name}" for name in sorted(TOPOLOGIES)]
    lines += ["", "Deployments (--tier compose only):"]
    lines += [f"  {name}" for name in sorted(DEPLOYMENTS)]
    lines += ["", "Multi-tenant deployments (--tier compose only):"]
    lines += [f"  {name}" for name in sorted(TENANT_DEPLOYMENTS)]

    return "\n".join(lines)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m cltl_integration",
        description="Run a topology or deployment interactively.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=describe())
    parser.add_argument("scenario", nargs="?",
                        help="the topology or deployment to run")
    parser.add_argument("--tier", choices=("inprocess", "compose"),
                        default="inprocess",
                        help="in this process (default) or as containers")
    parser.add_argument("--say", action="append", metavar="TEXT", default=[],
                        help="speak TEXT into the stub microphone; repeatable. "
                             "Needs espeak-ng; without it the microphone hears "
                             "a synthetic tone instead")
    parser.add_argument("--image-tag", default="latest",
                        help="image tag for --tier compose (default: latest)")
    parser.add_argument("--storage", type=Path, default=None,
                        help="where to put config and storage "
                             f"(default: {DEMO_ROOT}/<scenario>, wiped on start)")
    parser.add_argument("--list", action="store_true",
                        help="list the scenarios and exit")

    args = parser.parse_args(argv)
    if args.list:
        print(describe())
        raise SystemExit(0)
    if not args.scenario:
        parser.error("a scenario is required; --list shows them")

    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("cltl.combot.infra.config.local").setLevel(logging.WARNING)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    args = parse_args(argv)
    scenario = resolve(args.scenario)

    if isinstance(scenario, Deployment) and args.tier != "compose":
        raise SystemExit(
            f"{scenario.name} is a client/server split: two stacks on two "
            f"networks, which has no in-process meaning. Use --tier compose.")

    if isinstance(scenario, TenantDeployment) and args.tier != "compose":
        raise SystemExit(
            f"{scenario.name} is a multi-tenant deployment, and a tenant is a "
            f"routing key on a broker. SynchronousEventBus has no tenants — it "
            f"hands every event to every handler — so in one process the whole "
            f"scenario collapses into one. Use --tier compose.")

    storage = args.storage or (DEMO_ROOT / scenario.name)
    if args.storage is None and storage.exists():
        shutil.rmtree(storage)

    with _microphone(scenario, args) as mic, _speaker(scenario, args) as speaker, \
            _camera(scenario, args) as camera:
        runner = _build(scenario, args, storage, mic, speaker, camera)
        print(f"Starting {scenario.name} ({args.tier})...", flush=True)
        try:
            # Every runner tears its own half-started self down before raising,
            # so this is a message, not a cleanup. A missing image is the one
            # failure a person meets routinely, and a traceback buries the
            # sentence that says which image and how to build it.
            runner.start()
        except ComposeError as error:
            raise SystemExit(str(error))
        try:
            _open_scenario(scenario, runner)
            report(scenario, runner, storage, mic, speaker=speaker)
            _wait()
        finally:
            print("\nStopping...", flush=True)
            runner.stop()

    return 0


# -- assembling the run ------------------------------------------------------

class _Nothing:
    """Stand-in for a stub server the topology has no use for."""

    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc_val, exc_tb):
        return None


def _microphone(scenario, args):
    """A stub microphone, if anything in the scenario will go looking for one.

    Started before the runner, because ``[cltl.backend] server_audio_url`` is
    read when the backend's services are constructed and the port is only known
    once the socket is bound.
    """
    topologies = _topologies(scenario)
    if not any(needs_microphone(topology, _tier_of(scenario, args))
               for topology in topologies):
        return _Nothing()

    # In tier 2 the containers reach this through host.docker.internal, which is
    # not the loopback interface, so it has to be bound on all of them.
    host = "0.0.0.0" if _tier_of(scenario, args) == "compose" else "127.0.0.1"

    return StubAudioServer(_utterances(args.say), host=host)


def _speaker(scenario, args):
    """A stub loudspeaker, if anything in the scenario will try to speak.

    Same reasoning as :func:`_microphone`, and a sharper need: with nothing
    listening on ``[cltl.backend.text_output] remote_url`` the agent is mute and
    says so nowhere — ``SynchronizedTextToSpeech.say`` swallows the error. The
    stub logs each utterance at INFO, so a demo shows what the robot would say.
    """
    topologies = _topologies(scenario)
    if not any(needs_speaker(topology, _tier_of(scenario, args))
               for topology in topologies):
        return _Nothing()

    host = "0.0.0.0" if _tier_of(scenario, args) == "compose" else "127.0.0.1"

    return StubTextOutput(host=host)


def _camera(scenario, args):
    """A stub camera, if anything in the scenario will go looking for one.

    Same reasoning as :func:`_microphone`: ``[cltl.backend] server_image_url`` is
    read when the backend's services are constructed, so the socket has to be
    bound first. Serves one deterministic frame per capture — nothing downstream
    of the backend cares what the pixels are, and a demo only needs the pipeline
    to have something to carry.
    """
    topologies = _topologies(scenario)
    if not any(needs_camera(topology, _tier_of(scenario, args))
               for topology in topologies):
        return _Nothing()

    host = "0.0.0.0" if _tier_of(scenario, args) == "compose" else "127.0.0.1"

    return StubImageServer(host=host)


def _topologies(scenario) -> tuple:
    """The topologies a scenario is made of, whichever kind of scenario it is.

    One definition because three callers need it and a missing case here does not
    raise where it is written: a `TenantDeployment` that fell through to
    `(scenario,)` reached `needs_microphone` as though it were a Topology, and
    failed on `.modules` two frames away.
    """
    if isinstance(scenario, (Deployment, TenantDeployment)):
        return tuple(scenario.topologies())

    return (scenario,)


def _tier_of(scenario, args) -> str:
    return ("compose" if isinstance(scenario, (Deployment, TenantDeployment))
            else args.tier)


def _utterances(texts: Sequence[str]) -> List:
    if not texts:
        return [audio.utterance()]
    if not audio.speech_available():
        print("espeak-ng is not installed, so --say cannot synthesise speech; "
              "the microphone will hear a synthetic tone instead "
              "(sudo apt-get install -y espeak-ng)", file=sys.stderr)
        return [audio.utterance() for _ in texts]

    return [audio.spoken(text) for text in texts]


def _build(scenario, args, storage: Path, mic, speaker, camera):
    def _remote(stub, key):
        return {} if stub is None else {key: f"http://host.docker.internal:{stub.port}"}

    environment = {**_remote(mic, "CLTL_AUDIO_URL"), **_remote(speaker, "CLTL_TTS_URL"),
                   **_remote(camera, "CLTL_IMAGE_URL")}

    if isinstance(scenario, Deployment):
        return SplitRunner(scenario, storage_dir=storage,
                           image_tag=args.image_tag,
                           client_environment=environment)
    if isinstance(scenario, TenantDeployment):
        # The stubs, if any, belong to every tenant: they stand in for the
        # devices a tenant deployment is the near end of.
        return TenantRunner(scenario, storage_dir=storage,
                            image_tag=args.image_tag,
                            tenant_environment={tenant: environment
                                                for tenant in scenario.tenants})
    if args.tier == "compose":
        return ComposeRunner(scenario, storage_dir=storage,
                             image_tag=args.image_tag, environment=environment)

    local = {}
    if mic is not None:
        local["CLTL_AUDIO_URL"] = mic.url
    if speaker is not None:
        local["CLTL_TTS_URL"] = speaker.url
    if camera is not None:
        local["CLTL_IMAGE_URL"] = camera.url

    return InProcessRunner(scenario, storage_dir=storage, environment=local)


def _open_scenario(scenario, runner) -> None:
    """Give the modules the scenario they refuse to work without.

    ``ChatUiService`` will not render an utterance with a null scenario id and
    ``BackendService`` will not record without one, so something has to open it.
    With cltl-context in the scenario that something is the ``init`` intention,
    exactly as ``app/py-app/app.py`` publishes at startup; without it there is no
    BDI loop to ask, so the scenario is published directly.
    """
    if isinstance(scenario, TenantDeployment):
        # One scenario per tenant, each opened on that tenant's own bus. Opened
        # from the untenanted side it would be routed to the bare topic key and
        # reach no tenant at all — which is the property the deployment exists
        # to have, seen from the wrong end.
        for tenant in scenario.tenants:
            publish_intention(runner.tenant(tenant).event_bus, INTENTION_TOPIC, "init")
            print(f"Published the 'init' intention for {tenant}; "
                  f"its cltl-context opens the scenario.")
        return

    modules = _modules(scenario)
    if "context" in modules:
        publish_intention(runner.event_bus, INTENTION_TOPIC, "init")
        print("Published the 'init' intention; cltl-context opens the scenario.")
    elif {"chatui", "backend", "emissor"} & modules:
        started = start_scenario(runner.event_bus, SCENARIO_TOPIC)
        print(f"Opened scenario {started.id} (no cltl-context in this scenario).")


def _modules(scenario) -> set:
    return set().union(*(set(t.modules) for t in _topologies(scenario)))


# -- telling the user what to do ---------------------------------------------

def report(scenario, runner, storage: Path, stub=None,
           closing: Optional[str] = "  Ctrl-C to stop.", speaker=None) -> None:
    """Print where everything ended up, in a single write.

    Public because the manual tests print the same thing: a person driving a
    topology from pytest needs exactly what a person driving it from the demo
    launcher needs, and it should not drift into two versions.

    One write, not eighteen prints, because the modules log to stderr from their
    own threads throughout. Printed a line at a time, a log record lands *inside*
    a report line often enough to matter — roughly one run in ten — and what a
    reader is left with is a URL with a timestamp welded into the middle of it.
    """
    lines = ["", f"  {scenario.name} is up.", ""]
    for key in sorted(_modules(scenario)):
        for url, whose in _mounts(scenario, runner, key):
            lines.append(f"    {key:9} {url}{whose}")
            if key == "chatui":
                lines.append(f"    {'':9} {url}{CHAT_PAGE}{whose}   <- open this")
    if stub is not None:
        lines.append(f"    {'mic':9} {stub.url} (stub microphone)")
    if speaker is not None:
        lines.append(f"    {'speaker':9} {speaker.url} (stub loudspeaker; replies "
                     f"are logged as they are spoken)")
    lines += _broker(runner)
    lines += ["", f"    storage  {storage}", ""]
    if closing:
        lines += [closing, ""]
    lines.append("")

    sys.stdout.write("\n".join(lines))
    sys.stdout.flush()


def _mounts(scenario, runner, key: str):
    """The (url, annotation) pairs for one module — several in a multi-tenant run.

    A tenant module is deployed once per tenant and every copy serves, so there is
    no single URL to print and `runner.url` refuses to guess. The annotation says
    which tenant a URL belongs to, so that a run with both a chat UI and an
    EMISSOR endpoint per tenant does not print four unlabelled ports.

    A KeyError is an answer, not a failure: most modules serve no HTTP at all, and
    a module on more than one half of a deployment has to be asked for by half.
    """
    def _url(resolve, annotation=""):
        try:
            return [(resolve(key), annotation)]
        except KeyError:
            return []

    if isinstance(scenario, TenantDeployment) and key in scenario.tenant.modules:
        return [mount for tenant in scenario.tenants
                for mount in _url(runner.tenant(tenant).url, f"   ({tenant})")]

    return _url(runner.url)


def _broker(runner) -> List[str]:
    """The broker's published ports, when this run has a broker at all.

    Nothing else in this report says where they are, and a process outside the
    topology — a notebook, a script, an image built after the deployment came
    up — needs exactly this to join the bus. Ephemeral by design
    (docker-compose.yml publishes no fixed ports, so a run cannot lose to
    whatever else holds 5672), which is exactly why they have to be printed
    rather than assumed.

    Asked for, not computed: tier 1 has no broker at all (`SynchronousEventBus`
    is the bus), and a split or multi-tenant deployment keeps its one broker on
    the server half, so the address belongs to whichever runner actually holds
    a `ComposeRunner`. `getattr` covers all three without importing
    `SplitRunner`/`TenantRunner` here just to `isinstance` them.
    """
    source = runner if hasattr(runner, "amqp_url") else getattr(runner, "server", None)
    if source is None:
        return []

    return ["",
            f"    {'broker':9} {source.amqp_url}",
            f"    {'':9} {source.management_url}   "
            f"(RabbitMQ management, {BROKER_USER}/{BROKER_PASSWORD})"]


def _wait() -> None:
    """Block until Ctrl-C, without a polling loop.

    ``signal.pause()`` would do on Linux, but an Event also lets the handler run
    on the main thread while the runner's own threads keep serving.
    """
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    stop.wait()


if __name__ == "__main__":
    sys.exit(main())
