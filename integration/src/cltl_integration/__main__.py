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
from cltl_integration.drivers.tts import StubTextOutput
from cltl_integration.drivers.bdi import publish_intention
from cltl_integration.drivers.scenario import start_scenario
from cltl_integration.runner.compose import ComposeError, ComposeRunner
from cltl_integration.runner.inprocess import InProcessRunner
from cltl_integration.runner.split import SplitRunner
from cltl_integration.topology import (DEPLOYMENTS, TOPOLOGIES, Deployment,
                                       Topology, needs_microphone,
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
    return {**TOPOLOGIES, **DEPLOYMENTS}


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

    storage = args.storage or (DEMO_ROOT / scenario.name)
    if args.storage is None and storage.exists():
        shutil.rmtree(storage)

    with _microphone(scenario, args) as mic, _speaker(scenario, args) as speaker:
        runner = _build(scenario, args, storage, mic, speaker)
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
    topologies = (scenario.topologies() if isinstance(scenario, Deployment)
                  else (scenario,))
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
    topologies = (scenario.topologies() if isinstance(scenario, Deployment)
                  else (scenario,))
    if not any(needs_speaker(topology, _tier_of(scenario, args))
               for topology in topologies):
        return _Nothing()

    host = "0.0.0.0" if _tier_of(scenario, args) == "compose" else "127.0.0.1"

    return StubTextOutput(host=host)


def _tier_of(scenario, args) -> str:
    return "compose" if isinstance(scenario, Deployment) else args.tier


def _utterances(texts: Sequence[str]) -> List:
    if not texts:
        return [audio.utterance()]
    if not audio.speech_available():
        print("espeak-ng is not installed, so --say cannot synthesise speech; "
              "the microphone will hear a synthetic tone instead "
              "(sudo apt-get install -y espeak-ng)", file=sys.stderr)
        return [audio.utterance() for _ in texts]

    return [audio.spoken(text) for text in texts]


def _build(scenario, args, storage: Path, mic, speaker):
    def _remote(stub, key):
        return {} if stub is None else {key: f"http://host.docker.internal:{stub.port}"}

    environment = {**_remote(mic, "CLTL_AUDIO_URL"), **_remote(speaker, "CLTL_TTS_URL")}

    if isinstance(scenario, Deployment):
        return SplitRunner(scenario, storage_dir=storage,
                           image_tag=args.image_tag,
                           client_environment=environment)
    if args.tier == "compose":
        return ComposeRunner(scenario, storage_dir=storage,
                             image_tag=args.image_tag, environment=environment)

    local = {}
    if mic is not None:
        local["CLTL_AUDIO_URL"] = mic.url
    if speaker is not None:
        local["CLTL_TTS_URL"] = speaker.url

    return InProcessRunner(scenario, storage_dir=storage, environment=local)


def _open_scenario(scenario, runner) -> None:
    """Give the modules the scenario they refuse to work without.

    ``ChatUiService`` will not render an utterance with a null scenario id and
    ``BackendService`` will not record without one, so something has to open it.
    With cltl-context in the scenario that something is the ``init`` intention,
    exactly as ``app/py-app/app.py`` publishes at startup; without it there is no
    BDI loop to ask, so the scenario is published directly.
    """
    modules = _modules(scenario)
    if "context" in modules:
        publish_intention(runner.event_bus, INTENTION_TOPIC, "init")
        print("Published the 'init' intention; cltl-context opens the scenario.")
    elif {"chatui", "backend", "emissor"} & modules:
        started = start_scenario(runner.event_bus, SCENARIO_TOPIC)
        print(f"Opened scenario {started.id} (no cltl-context in this scenario).")


def _modules(scenario) -> set:
    if isinstance(scenario, Deployment):
        return set().union(*(set(t.modules) for t in scenario.topologies()))

    return set(scenario.modules)


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
        try:
            url = runner.url(key)
        except KeyError:
            continue                      # no HTTP mount, or on both halves
        lines.append(f"    {key:9} {url}")
        if key == "chatui":
            lines.append(f"    {'':9} {url}{CHAT_PAGE}   <- open this")
    if stub is not None:
        lines.append(f"    {'mic':9} {stub.url} (stub microphone)")
    if speaker is not None:
        lines.append(f"    {'speaker':9} {speaker.url} (stub loudspeaker; replies "
                     f"are logged as they are spoken)")
    lines += ["", f"    storage  {storage}", ""]
    if closing:
        lines += [closing, ""]
    lines.append("")

    sys.stdout.write("\n".join(lines))
    sys.stdout.flush()


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
