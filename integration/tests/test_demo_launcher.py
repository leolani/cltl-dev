"""The demo front end, driven the way a person drives it.

``python -m cltl_integration <scenario>`` is the third front end onto the same
``Topology`` objects — after a tier-1 test and a tier-2 test — and the one with
no assertions in it. That makes it the easiest thing in the harness to break
silently: a demo that starts, prints its URLs and then does nothing looks
exactly like a demo that works, because nobody is watching the event bus.

So it is tested as a subprocess: started, talked to over HTTP, interrupted, and
checked for having let go of its port. What that pins down is the part a unit
test cannot see — that the launcher opens a scenario (without one the chat UI
refuses to render anything at all) and that Ctrl-C actually stops the modules.
"""
import os
import re
import signal
import subprocess
import sys
import threading
import time

import pytest
import requests

from cltl_integration.__main__ import (CHAT_PAGE, COMPONENT_ROOT, describe,
                                       parse_args, resolve)
from cltl_integration.drivers.chat import ChatClient
from cltl_integration.topology import DEPLOYMENTS, TOPOLOGIES

STARTUP_TIMEOUT = 120.0
SHUTDOWN_TIMEOUT = 90.0
READY = "is up."


class TestArguments:
    def test_every_scenario_is_listed(self):
        listing = describe()

        for name in list(TOPOLOGIES) + list(DEPLOYMENTS):
            assert name in listing

    def test_a_hyphenated_name_resolves(self):
        """`make demo-text-pipeline` reads better than demo-text_pipeline."""
        assert resolve("text-pipeline") is TOPOLOGIES["text_pipeline"]

    def test_an_unknown_scenario_says_what_there_is(self):
        with pytest.raises(SystemExit) as error:
            resolve("not-a-scenario")

        assert "text_pipeline" in str(error.value)

    def test_listing_exits_without_a_scenario(self, capsys):
        with pytest.raises(SystemExit) as error:
            parse_args(["--list"])

        assert error.value.code == 0
        assert "csplit" in capsys.readouterr().out


class TestRunning:
    """Started, talked to, and interrupted — one scenario per branch.

    ``text_pipeline`` has cltl-context, so the launcher opens the scenario by
    publishing the ``init`` intention and letting the BDI loop do it.
    ``eliza_chatui`` has no BDI loop, so the launcher publishes
    ``ScenarioStarted`` itself. Those are the two paths through
    ``_open_scenario``, and either one failing leaves a demo that comes up
    healthy and answers nothing.
    """

    @pytest.mark.parametrize("scenario", ["text_pipeline", "eliza_chatui"])
    def test_the_demo_answers_and_stops(self, scenario, tmp_path):
        with _demo(scenario, tmp_path) as demo:
            page = requests.get(demo.url("chatui") + CHAT_PAGE, timeout=10)
            assert page.status_code == 200, (
                f"the chat page is not being served: {page.status_code} for "
                f"{page.url}\n{demo.output}")

            client = ChatClient(demo.url("chatui"))
            client.await_scenario(timeout=30)
            chat_id = client.start_session()
            client.send(chat_id, "I feel very anxious today")

            assert client.receive(chat_id, timeout=30), (
                f"the {scenario} demo never answered:\n{demo.output}")

        assert demo.returncode == 0, f"unclean exit:\n{demo.output}"
        assert _refused(demo.url("chatui")), "the demo did not release its port"


# -- driving the subprocess --------------------------------------------------

class _Demo:
    """A launcher subprocess, read asynchronously so it cannot fill its pipes.

    The two streams are kept apart. The launcher's report goes to stdout and the
    modules log to stderr from their own threads throughout, so merging them
    lets a log record land inside a report line — which is how this test found
    that the report needed to be written in one call rather than eighteen.
    Parsing only stdout means the test cannot be broken again by log volume,
    while ``output`` still carries both for diagnosing a failure.
    """

    def __init__(self, args):
        self._args = list(args)
        self._process = None
        self._out = []
        self._err = []
        self._readers = []
        self.returncode = None

    @property
    def output(self) -> str:
        return "".join(self._out) + "".join(self._err)

    def url(self, module_key: str) -> str:
        """Read a URL back out of the launcher's own report.

        Deliberately not recomputed from DEFAULT_PORT: what is being tested is
        that the launcher tells a person somewhere they can actually go.
        """
        pattern = re.compile(rf"^ +{re.escape(module_key)} +(http\S+)$")
        for line in "".join(self._out).splitlines():
            found = pattern.match(line)
            if found:
                return found.group(1)

        raise AssertionError(f"{module_key} was never reported:\n{self.output}")

    def __enter__(self) -> "_Demo":
        environment = dict(os.environ, PYTHONPATH="src", PYTHONUNBUFFERED="1")
        self._process = subprocess.Popen(
            [sys.executable, "-m", "cltl_integration", *self._args],
            cwd=str(COMPONENT_ROOT), env=environment, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self._readers = [self._reader(self._process.stdout, self._out),
                         self._reader(self._process.stderr, self._err)]
        self._await_ready()

        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.interrupt()

    def _reader(self, stream, sink) -> threading.Thread:
        """Line at a time. ``read(n)`` on a text stream blocks for n characters,
        which for output this short means blocking until the process exits."""
        def drain():
            for line in stream:
                sink.append(line)

        thread = threading.Thread(target=drain, daemon=True)
        thread.start()

        return thread

    def _await_ready(self) -> None:
        """Wait for the report, which arrives whole or not at all."""
        end = time.monotonic() + STARTUP_TIMEOUT
        while time.monotonic() < end:
            if READY in "".join(self._out):
                return
            if self._process.poll() is not None:
                raise AssertionError(f"the demo exited early:\n{self.output}")
            time.sleep(0.2)

        self.interrupt()
        raise AssertionError(
            f"the demo did not come up within {STARTUP_TIMEOUT}s:\n{self.output}")

    def interrupt(self) -> None:
        """Ctrl-C, which is the only way a demo is meant to end."""
        if self._process is None or self.returncode is not None:
            return
        self._process.send_signal(signal.SIGINT)
        try:
            self.returncode = self._process.wait(timeout=SHUTDOWN_TIMEOUT)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self.returncode = self._process.wait(timeout=30)
            raise AssertionError(
                f"the demo ignored SIGINT for {SHUTDOWN_TIMEOUT}s:\n{self.output}")
        finally:
            for reader in self._readers:
                reader.join(timeout=5)


def _demo(scenario: str, storage) -> _Demo:
    return _Demo([scenario, "--storage", str(storage)])


def _refused(url: str) -> bool:
    try:
        requests.get(url, timeout=2)
    except requests.ConnectionError:
        return True

    return False
