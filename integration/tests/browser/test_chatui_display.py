"""Browser: what a person sees in the Display tab.

The slice asserts the route the page polls; this asserts the page that polls it.
What only a browser can tell: that the tab appears when `/config` says so, that
the frame follows the newest page, that choosing an older page switches the
frame, that a name is drawn as text and not as markup, and that a new page does
not take the person away from the tab they are on.
"""
import pytest
from playwright.sync_api import expect

from cltl_integration.drivers.chat import ChatClient
from cltl_integration.drivers.display import DISPLAY_TOPIC, publish_display
from cltl_integration.drivers.scenario import start_scenario, stop_scenario
from cltl_integration.topology import CHATUI_DISPLAY

pytestmark = pytest.mark.browser

SCENARIO_TOPIC = "cltl.topic.scenario"


@pytest.fixture
def display_page(inprocess, page):
    runner = inprocess(CHATUI_DISPLAY)
    scenario = start_scenario(runner.event_bus, SCENARIO_TOPIC)
    ChatClient(runner.url("chatui")).await_scenario()
    page.goto(runner.url("chatui") + "/static/chat.html")
    # The tab is only offered once /config has answered.
    expect(page.locator("#tab-display")).to_be_visible()

    def show(name, path="/chat/scenario"):
        publish_display(runner.event_bus, DISPLAY_TOPIC, scenario.id, runner.url("chatui") + path, name)

    return page, show, runner, scenario


def test_the_tab_is_there_before_anything_is_shown(display_page):
    page, _, _, _ = display_page

    expect(page.locator("#display-tab-label")).to_have_text("Display")
    page.locator("#tab-display").click()
    expect(page.locator("#display-status")).to_have_text("Nothing to show yet.")
    expect(page.locator("#display-open")).to_be_hidden()
    assert page.errors == []


def test_the_newest_page_is_on_view_and_names_the_tab(display_page):
    page, show, runner, _ = display_page
    page.locator("#tab-display").click()

    show("Routes", "/urlmap")

    expect(page.locator("#display-tab-label")).to_have_text("Routes")
    expect(page.locator("#display-frame")).to_have_attribute("src", runner.url("chatui") + "/urlmap")
    expect(page.locator("#display-open")).to_have_attribute("href", runner.url("chatui") + "/urlmap")


def test_choosing_an_older_page_switches_the_frame(display_page):
    page, show, runner, _ = display_page
    page.locator("#tab-display").click()
    show("First", "/urlmap")
    expect(page.locator("#display-history option")).to_have_count(1)
    show("Second", "/chat/scenario")
    expect(page.locator("#display-history option")).to_have_count(2)
    expect(page.locator("#display-history option").first).to_have_text("Second")

    page.locator("#display-history").select_option(label="First")

    expect(page.locator("#display-frame")).to_have_attribute("src", runner.url("chatui") + "/urlmap")


def test_a_new_page_does_not_take_the_person_off_their_tab(display_page):
    page, show, runner, _ = display_page
    expect(page.locator("#display")).to_be_hidden()

    show("Routes", "/urlmap")

    expect(page.locator("#display-badge")).to_be_visible()
    expect(page.locator("#display")).to_be_hidden()
    page.locator("#tab-display").click()
    expect(page.locator("#display-badge")).to_be_hidden()


def test_a_name_is_text_and_never_markup(display_page):
    page, show, _, _ = display_page
    page.locator("#tab-display").click()

    show("<img src=x onerror=window.pwned=1>")

    expect(page.locator("#display-tab-label")).to_have_text("<img src=x onerror=window.pwned=1>")
    assert page.evaluate("window.pwned") is None
    assert page.errors == []


def test_the_page_goes_when_the_scenario_ends(display_page):
    page, show, runner, scenario = display_page
    page.locator("#tab-display").click()
    show("Routes", "/urlmap")
    expect(page.locator("#display-tab-label")).to_have_text("Routes")

    stop_scenario(runner.event_bus, SCENARIO_TOPIC, scenario)

    expect(page.locator("#display-tab-label")).to_have_text("Display")
    expect(page.locator("#display-status")).to_have_text("Nothing to show yet.")
