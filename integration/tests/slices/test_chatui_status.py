"""Slice: the status bar's server half, behind the real HTTP app.

The chat UI is the consumer and this test is the emitter, because nothing in the
platform publishes status yet. What it pins down is what only the composed
process can: that a `StatusEvent` survives the event bus's own transport, and
that the routes the page polls are mounted where the page looks for them.
"""
import time

import pytest
import requests

from cltl_integration.drivers.chat import ChatClient
from cltl_integration.drivers.scenario import start_scenario, stop_scenario
from cltl_integration.drivers.status import STATUS_TOPIC, publish_status
from cltl_integration.topology import CHATUI_STATUS

SCENARIO_TOPIC = "cltl.topic.scenario"
TIMEOUT = 10.0


@pytest.fixture
def chat(inprocess):
    runner = inprocess(CHATUI_STATUS)
    scenario = start_scenario(runner.event_bus, SCENARIO_TOPIC)
    ChatClient(runner.url("chatui")).await_scenario()

    return runner, scenario


def _status(runner, from_sequence=0) -> dict:
    return requests.get(f"{runner.url('chatui')}/chat/status", params={"from": from_sequence},
                        timeout=5).json()


def _await_rows(runner, count: int) -> dict:
    end = time.monotonic() + TIMEOUT
    while True:
        status = _status(runner)
        if len(status["events"]) >= count:
            return status
        if time.monotonic() >= end:
            raise AssertionError(f"fewer than {count} status row(s) within {TIMEOUT}s: {status}")
        time.sleep(0.05)


def test_the_page_is_told_that_status_is_on(chat):
    runner, _ = chat

    config = requests.get(f"{runner.url('chatui')}/config", timeout=5).json()

    assert config["status_enabled"] is True


def test_a_status_shows_and_is_ended_by_its_key(chat):
    runner, scenario = chat

    publish_status(runner.event_bus, STATUS_TOPIC, scenario.id, "loading", "asr",
                   level="warning", progress=0.5, key="asr.model")
    status = _await_rows(runner, 1)
    assert status["current"]["message"] == "loading"
    assert status["current"]["progress"] == 0.5

    publish_status(runner.event_bus, STATUS_TOPIC, scenario.id, "loaded", "asr",
                   key="asr.model", resolved=True)
    status = _await_rows(runner, 2)

    assert status["current"] is None
    assert [e["message"] for e in status["events"]] == ["loading", "loaded"]


def test_a_persistent_status_goes_to_the_chat_and_not_the_bar(chat):
    runner, scenario = chat
    client = ChatClient(runner.url("chatui"))
    chat_id = client.start_session()

    publish_status(runner.event_bus, STATUS_TOPIC, scenario.id, "Went offline", "asr",
                   level="warning", persistent=True)

    status = _await_rows(runner, 1)
    assert status["current"] is None
    assert status["events"][0]["persistent"] is True
    end = time.monotonic() + TIMEOUT
    while not any("Went offline" in u["text"] for u in client.fetch_all(chat_id)):
        assert time.monotonic() < end, "the status never reached the transcript"
        time.sleep(0.05)


def test_the_timeline_goes_with_the_scenario(chat):
    runner, scenario = chat
    publish_status(runner.event_bus, STATUS_TOPIC, scenario.id, "hello", "test")
    _await_rows(runner, 1)

    stop_scenario(runner.event_bus, SCENARIO_TOPIC, scenario)

    end = time.monotonic() + TIMEOUT
    while _status(runner)["events"]:
        assert time.monotonic() < end, "the timeline outlived its scenario"
        time.sleep(0.05)
