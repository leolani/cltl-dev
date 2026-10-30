"""Slice: the Display tab's server half, behind the real HTTP app.

The chat UI is the consumer and this test is the publisher, because nothing in
the platform asks for a page to be shown yet. What it pins down is what only the
composed process can: that a `DisplayEvent` survives the event bus's own
transport, and that the routes the page polls are mounted where the page looks
for them.
"""
import time

import pytest
import requests

from cltl_integration.drivers.chat import ChatClient
from cltl_integration.drivers.display import DISPLAY_TOPIC, publish_display
from cltl_integration.drivers.scenario import start_scenario, stop_scenario
from cltl_integration.topology import CHATUI_DISPLAY

SCENARIO_TOPIC = "cltl.topic.scenario"
TIMEOUT = 10.0


@pytest.fixture
def chat(inprocess):
    runner = inprocess(CHATUI_DISPLAY)
    scenario = start_scenario(runner.event_bus, SCENARIO_TOPIC)
    ChatClient(runner.url("chatui")).await_scenario()

    return runner, scenario


def _display(runner) -> dict:
    return requests.get(f"{runner.url('chatui')}/chat/display", timeout=5).json()


def _await_pages(runner, count: int) -> dict:
    end = time.monotonic() + TIMEOUT
    while True:
        display = _display(runner)
        if len(display["entries"]) >= count:
            return display
        if time.monotonic() >= end:
            raise AssertionError(f"fewer than {count} page(s) within {TIMEOUT}s: {display}")
        time.sleep(0.05)


def test_the_page_is_told_that_display_is_on(chat):
    runner, _ = chat

    config = requests.get(f"{runner.url('chatui')}/config", timeout=5).json()

    assert config["display_enabled"] is True


def test_a_page_is_listed_with_the_name_it_was_given(chat):
    runner, scenario = chat

    publish_display(runner.event_bus, DISPLAY_TOPIC, scenario.id, "http://localhost:8123/a", "Chart")
    display = _await_pages(runner, 1)

    assert [(e["name"], e["url"]) for e in display["entries"]] == [("Chart", "http://localhost:8123/a")]
    assert display["scenario_id"] == scenario.id


def test_a_page_on_a_host_nobody_allowed_is_not_listed(chat):
    runner, scenario = chat

    publish_display(runner.event_bus, DISPLAY_TOPIC, scenario.id, "https://evil.example/", "Refused")
    publish_display(runner.event_bus, DISPLAY_TOPIC, scenario.id, "http://localhost/ok", "Fine")

    assert [e["name"] for e in _await_pages(runner, 1)["entries"]] == ["Fine"]


def test_the_history_goes_with_the_scenario(chat):
    runner, scenario = chat
    publish_display(runner.event_bus, DISPLAY_TOPIC, scenario.id, "http://localhost/a", "A")
    _await_pages(runner, 1)

    stop_scenario(runner.event_bus, SCENARIO_TOPIC, scenario)
    end = time.monotonic() + TIMEOUT
    while _display(runner)["entries"]:
        assert time.monotonic() < end, "the history was not cleared"
        time.sleep(0.05)
