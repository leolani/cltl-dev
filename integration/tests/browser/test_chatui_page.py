"""Browser: what a person sees on the chat UI page.

The slices assert the routes the page polls; this asserts the page that polls
them. What only a browser can tell: that the script loads from where the HTML
says it does, that `/config` really switches the bar on and off, that the rows
are drawn as text and not as markup, and that the bar follows the server's
choice of entry.
"""
import re

import pytest
from playwright.sync_api import expect

from cltl_integration.drivers.chat import ChatClient
from cltl_integration.drivers.scenario import start_scenario
from cltl_integration.drivers.status import STATUS_TOPIC, publish_status
from cltl_integration.topology import CHATUI_STATUS, Topology

pytestmark = pytest.mark.browser

SCENARIO_TOPIC = "cltl.topic.scenario"

# The chat UI alone, no status topic. Not CHATUI_IMAGE: that one pulls in the
# backend, which needs libsndfile, and this page has nothing to do with it.
CHATUI = Topology(name="chatui", modules=("chatui",))


def _open(runner, page):
    scenario = start_scenario(runner.event_bus, SCENARIO_TOPIC)
    client = ChatClient(runner.url("chatui"))
    client.await_scenario()
    page.goto(runner.url("chatui") + "/static/chat.html")

    return scenario, client


@pytest.fixture
def status_page(inprocess, page):
    runner = inprocess(CHATUI_STATUS)
    scenario, client = _open(runner, page)
    # The bar is only switched on once /config has answered.
    expect(page.locator("#status-bar")).to_be_visible()

    def say(message, source="test", **kwargs):
        publish_status(runner.event_bus, STATUS_TOPIC, scenario.id, message, source, **kwargs)

    return page, say, client


def test_the_page_loads_without_errors_and_shows_what_a_person_says(inprocess, page):
    runner = inprocess(CHATUI)
    _, client = _open(runner, page)

    client.send(client.start_session(), "hello there")

    expect(page.locator("#chat")).to_contain_text("hello there")
    assert page.errors == []


def test_there_is_no_status_bar_without_a_status_topic(inprocess, page):
    runner = inprocess(CHATUI)
    _open(runner, page)
    expect(page.locator("#chat")).to_be_visible()

    # Let the script's /config request come back before asserting an absence.
    page.wait_for_load_state("networkidle")

    expect(page.locator("#status-bar")).to_be_hidden()


def test_the_bar_is_idle_until_something_is_reported(status_page):
    page, _, _ = status_page

    expect(page.locator("#status-message")).to_have_text("No status")
    expect(page.locator("#status-toggle")).to_have_class("cltl-idle")


def test_the_bar_shows_a_status_with_its_progress_and_drops_it_when_ended(status_page):
    page, say, _ = status_page

    say("Loading the model", "asr", progress=0.5, key="asr.model")

    expect(page.locator("#status-message")).to_have_text("Loading the model")
    expect(page.locator("#status-source")).to_have_text("asr")
    expect(page.locator("#status-progress")).to_be_visible()
    assert page.locator("#status-progress").evaluate("e => e.value") == 0.5

    page.locator("#status-toggle").click()
    expect(page.locator("#status-timeline li")).to_have_text(re.compile(r"Loading the model \(50%\)$"))
    page.locator("#status-toggle").click()

    say("Model loaded", "asr", key="asr.model", resolved=True)

    expect(page.locator("#status-message")).to_have_text("No status")
    expect(page.locator("#status-progress")).to_be_hidden()


def test_an_error_stays_in_the_bar_over_a_newer_info(status_page):
    page, say, _ = status_page

    say("Camera lost", "backend", level="error", key="camera")
    expect(page.locator("#status-message")).to_have_text("Camera lost")
    say("Listening", "vad")

    page.locator("#status-toggle").click()
    expect(page.locator("#status-timeline > li")).to_have_count(2)

    expect(page.locator("#status-message")).to_have_text("Camera lost")
    expect(page.locator("#status-level")).to_have_attribute("data-level", "error")


def test_the_timeline_opens_over_the_chat_without_moving_the_page(status_page):
    page, say, _ = status_page
    say("first", "a")
    say("second", "b", level="warning")
    timeline = page.locator("#status-timeline")
    toggle = page.locator("#status-toggle")
    expect(page.locator("#status-message")).to_have_text("second")
    expect(timeline).to_be_hidden()
    before = page.locator("#chat").bounding_box()

    toggle.click()

    expect(timeline).to_be_visible()
    expect(toggle).to_have_attribute("aria-expanded", "true")
    expect(timeline.locator("li")).to_have_count(2)
    expect(timeline.locator("li").nth(0)).to_contain_text("first")
    expect(timeline.locator("li").nth(1)).to_contain_text("second")
    assert page.locator("#chat").bounding_box() == before

    toggle.click()

    expect(timeline).to_be_hidden()
    expect(toggle).to_have_attribute("aria-expanded", "false")


def test_a_message_is_text_and_never_markup(status_page):
    page, say, _ = status_page
    page.on("dialog", lambda dialog: (page.errors.append("dialog: " + dialog.message), dialog.dismiss()))

    say("<img src=x onerror=alert(1)><b>bold</b>", "<i>src</i>")
    expect(page.locator("#status-message")).to_have_text("<img src=x onerror=alert(1)><b>bold</b>")
    page.locator("#status-toggle").click()

    expect(page.locator("#status-timeline li")).to_contain_text("<b>bold</b>")
    assert page.locator("#status-bar img, #status-bar b, #status-bar i").count() == 0
    assert page.errors == []


def test_a_persistent_status_is_a_line_in_the_chat_and_not_in_the_bar(status_page):
    page, say, client = status_page
    page.on("dialog", lambda dialog: (page.errors.append("dialog: " + dialog.message), dialog.dismiss()))

    say("Switched to <script>alert(1)</script> offline", "asr", level="warning", persistent=True)

    line = page.locator("#chat .cltl-status-line")
    expect(line).to_contain_text("Switched to <script>alert(1)</script> offline")
    expect(line.locator(".cltl-status-source")).to_have_text("asr")
    expect(line).to_have_class(re.compile(r"cltl-status-warning"))
    # No speaker prefix in front of it, and nothing for the bar to show.
    expect(page.locator("#chat")).not_to_contain_text("> Switched")
    expect(page.locator("#status-message")).to_have_text("No status")
    assert page.locator("#chat script").count() == 0

    # It is centred in the chat window, not hung off the left like a reply.
    window = page.locator("#chat").bounding_box()
    bubble = page.locator("#chat .bubble.say:has(.cltl-status-line)")  # `say`: done animating in
    expect(bubble).to_be_visible()
    page.wait_for_timeout(500)
    box = bubble.bounding_box()
    assert abs((box["x"] + box["width"] / 2) - (window["x"] + window["width"] / 2)) < 20

    page.locator("#status-toggle").click()
    expect(page.locator("#status-timeline li.cltl-persistent")).to_have_count(1)

    page.reload()
    expect(page.locator("#chat .cltl-status-line")).to_have_count(1)
    assert page.errors == []
