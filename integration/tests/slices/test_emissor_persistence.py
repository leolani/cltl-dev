"""Slice: cltl-emissor-data — the event stream written down as a scenario.

Every other module publishes and forgets. This one is the platform's memory: it
subscribes to a configured list of topics and turns whatever arrives into an
EMISSOR scenario on disk, plus an HTTP API to read it back. That makes it the
one module whose contract is not an event it emits but a side effect, and the
only way to test it is to look at what it wrote.

Two things about it are easy to get wrong and quiet when they are:

* it files signals by ``signal.time.container_id`` — the scenario id — and drops
  anything belonging to a scenario it never saw started, with a log warning and
  no error. A pipeline that loses the scenario id somewhere upstream therefore
  persists nothing while looking entirely healthy.
* ``flush_interval`` defaults to -1, which never writes signals to disk at all.
  ``config/topologies/emissor.config`` sets it to 0 for exactly this reason.
"""
import json
import time

import pytest
import requests
from cltl.combot.event.emissor import TextSignalEvent
from cltl.combot.infra.event import Event
from cltl.combot.infra.time_util import timestamp_now
from emissor.representation.scenario import TextSignal

from cltl_integration.drivers.scenario import start_scenario, stop_scenario
from cltl_integration.topology import EMISSOR

SCENARIO_TOPIC = "cltl.topic.scenario"
TEXT_IN = "cltl.topic.text_in"
TEXT_OUT = "cltl.topic.text_out"

TIMEOUT = 5.0


@pytest.fixture
def emissor(inprocess):
    runner = inprocess(EMISSOR)
    runner.probe.subscribe(SCENARIO_TOPIC)
    return runner


def _utterance(scenario_id: str, text: str) -> TextSignal:
    return TextSignal.for_scenario(
        scenario_id, timestamp_now(), timestamp_now(), None, text)


def _say(runner, topic: str, signal: TextSignal) -> TextSignal:
    payload = (TextSignalEvent.for_agent(signal) if topic == TEXT_OUT
               else TextSignalEvent.for_speaker(signal))
    runner.event_bus.publish(topic, Event.for_payload(payload))

    return signal


def _scenario_dir(runner, scenario_id: str):
    return runner.storage_path / "emissor" / scenario_id


def _await_file(path, timeout: float = TIMEOUT):
    """Persistence happens on the service's worker thread, not the publisher's."""
    end = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= end:
            raise AssertionError(f"{path} was not written within {timeout}s")
        time.sleep(0.02)

    return path


def _await_texts(runner, scenario_id: str, count: int, timeout: float = TIMEOUT):
    path = _scenario_dir(runner, scenario_id) / "text.json"
    end = time.monotonic() + timeout
    texts = []
    while time.monotonic() < end:
        if path.exists():
            texts = [signal["text"] for signal in json.loads(path.read_text())]
            if len(texts) >= count:
                return texts
        time.sleep(0.02)

    raise AssertionError(f"expected {count} text signal(s) in {path}, found {texts}")


class TestScenarioFiles:
    def test_started_scenario_is_written_to_disk(self, emissor):
        scenario = start_scenario(emissor.event_bus, SCENARIO_TOPIC)

        _await_file(_scenario_dir(emissor, scenario.id) / f"{scenario.id}.json")

    def test_utterances_are_persisted_in_order(self, emissor):
        scenario = start_scenario(emissor.event_bus, SCENARIO_TOPIC)
        _await_file(_scenario_dir(emissor, scenario.id) / f"{scenario.id}.json")

        _say(emissor, TEXT_IN, _utterance(scenario.id, "I am sad"))
        _say(emissor, TEXT_OUT, _utterance(scenario.id, "Why are you sad?"))

        assert _await_texts(emissor, scenario.id, 2) == ["I am sad", "Why are you sad?"]

    def test_signals_for_an_unknown_scenario_are_dropped(self, emissor):
        """A lost scenario id costs the turn, silently. Pin the behaviour down."""
        scenario = start_scenario(emissor.event_bus, SCENARIO_TOPIC)
        _await_file(_scenario_dir(emissor, scenario.id) / f"{scenario.id}.json")

        _say(emissor, TEXT_IN, _utterance("never-started", "into the void"))
        _say(emissor, TEXT_IN, _utterance(scenario.id, "on the record"))

        assert _await_texts(emissor, scenario.id, 1) == ["on the record"]
        assert not _scenario_dir(emissor, "never-started").exists()


class TestScenarioApi:
    """The HTTP surface, which is how anything outside the process reads this."""

    def test_running_scenario_is_listed(self, emissor):
        scenario = start_scenario(emissor.event_bus, SCENARIO_TOPIC)
        _await_file(_scenario_dir(emissor, scenario.id) / f"{scenario.id}.json")

        listed = requests.get(emissor.url("emissor") + "/scenarios").json()

        assert [entry["id"] for entry in listed["scenarios"]] == [scenario.id]

    def test_a_signal_resolves_to_its_scenario(self, emissor):
        """The index that lets a downstream module ask "which conversation?"."""
        scenario = start_scenario(emissor.event_bus, SCENARIO_TOPIC)
        signal = _say(emissor, TEXT_IN, _utterance(scenario.id, "hello"))
        _await_texts(emissor, scenario.id, 1)

        response = requests.get(f"{emissor.url('emissor')}/{signal.id}/scenario/id")

        assert response.status_code == 200
        assert response.text == scenario.id

    def test_stopped_scenario_records_its_end(self, emissor):
        scenario = start_scenario(emissor.event_bus, SCENARIO_TOPIC)
        _await_file(_scenario_dir(emissor, scenario.id) / f"{scenario.id}.json")

        stop_scenario(emissor.event_bus, SCENARIO_TOPIC, scenario)

        listed = _await_scenario_end(emissor, scenario.id)
        assert listed["end"] >= listed["start"]


def _await_scenario_end(runner, scenario_id: str, timeout: float = TIMEOUT):
    end = time.monotonic() + timeout
    entry = None
    while time.monotonic() < end:
        listed = requests.get(runner.url("emissor") + "/scenarios").json()["scenarios"]
        entry = next((item for item in listed if item["id"] == scenario_id), None)
        if entry and entry.get("end"):
            return entry
        time.sleep(0.05)

    raise AssertionError(f"scenario {scenario_id} has no end time: {entry}")
