"""Slice: what the Monitoring tab shows while a conversation is running.

The tab is an iframe onto a page cltl-monitoring serves, so what this asserts is
the thing behind the iframe: that an image submitted in one conversation is
served back, annotated, under *that conversation's* id and no other.

The second half is the whole point of the change. cltl-monitoring used to hold
one image, one caption and one activity flag for the entire process, so a second
conversation silently overwrote the first and every URL returned whatever had
arrived last. Nothing a single-conversation test could ask would have failed.

Two modules besides the subject, each carrying something the other cannot:
cltl-chat-ui is the only module that turns a human gesture into an `ImageSignal`,
and it embeds the regions *inside* that signal rather than publishing them on an
annotation topic — so it is also the only thing that exercises monitoring's
reading of `signal.mentions`. cltl-backend holds the pixels the signal only
refers to; without it the reference resolves to nothing, which is a different
failure from "the annotations were lost" and has to be told apart from it.
"""
import time

import pytest
import requests

from cltl_integration.drivers.chat import ChatClient
from cltl_integration.drivers.image import png, region
from cltl_integration.drivers.scenario import start_scenario
from cltl_integration.topology import CHATUI_MONITORING

SCENARIO_TOPIC = "cltl.topic.scenario"

WIDTH, HEIGHT = 64, 48
PNG = png(WIDTH, HEIGHT, (255, 0, 0))

REGIONS = [region(0, 0, 32, 24, "a chair"),
           region(32, 24, 64, 48, "a lamp")]

TIMEOUT = 10.0


@pytest.fixture
def chat(inprocess):
    """The topology, a chat client, an open scenario, and monitoring's base URL."""
    runner = inprocess(CHATUI_MONITORING)

    scenario = start_scenario(runner.event_bus, SCENARIO_TOPIC)
    client = ChatClient(runner.url("chatui"))
    client.await_scenario()

    return runner, client, scenario, runner.url("monitoring")


def _submit(client, chat_id, regions=REGIONS):
    upload = client.upload_image(chat_id, PNG, WIDTH, HEIGHT)
    client.annotate(chat_id, upload["id"], regions)

    return upload


def _await_image(base_url: str, scenario_id: str, timeout: float = TIMEOUT) -> bytes:
    """Monitoring renders on its own worker, not on the submitting thread."""
    end = time.monotonic() + timeout
    while True:
        response = requests.get(f"{base_url}/scenarios/{scenario_id}/image.jpg", timeout=5)
        if response.status_code == 200:
            return response.content
        if time.monotonic() >= end:
            raise AssertionError(
                f"no image for scenario {scenario_id} within {timeout}s "
                f"(last status {response.status_code})")
        time.sleep(0.05)


class TestSubmittedImage:
    def test_a_submitted_image_reaches_monitoring(self, chat):
        _, client, scenario, monitoring = chat
        chat_id = client.start_session()

        _submit(client, chat_id)

        assert _await_image(monitoring, scenario.id).startswith(b"\xff\xd8")

    def test_the_regions_are_drawn_on_it(self, chat):
        """Against the same image with no regions, so only the drawing differs."""
        _, client, scenario, monitoring = chat
        chat_id = client.start_session()

        _submit(client, chat_id, regions=[])
        plain = _await_image(monitoring, scenario.id)

        _submit(client, chat_id)

        end = time.monotonic() + TIMEOUT
        while _await_image(monitoring, scenario.id) == plain:
            if time.monotonic() >= end:
                raise AssertionError("the submitted regions were never drawn")
            time.sleep(0.05)

    def test_the_scenario_is_listed(self, chat):
        _, client, scenario, monitoring = chat
        chat_id = client.start_session()
        _submit(client, chat_id)
        _await_image(monitoring, scenario.id)

        listed = requests.get(f"{monitoring}/scenarios", timeout=5).json()["scenarios"]

        assert scenario.id in listed


class TestScenarioIsolation:
    def test_another_scenario_does_not_see_the_image(self, chat):
        """The regression this whole change is about."""
        runner, client, scenario, monitoring = chat
        chat_id = client.start_session()
        _submit(client, chat_id)
        _await_image(monitoring, scenario.id)

        other = start_scenario(runner.event_bus, SCENARIO_TOPIC)

        response = requests.get(f"{monitoring}/scenarios/{other.id}/image.jpg", timeout=5)
        assert response.status_code == 404

    def test_an_unknown_scenario_is_not_served_someone_elses_image(self, chat):
        _, client, scenario, monitoring = chat
        chat_id = client.start_session()
        _submit(client, chat_id)
        _await_image(monitoring, scenario.id)

        response = requests.get(f"{monitoring}/scenarios/nobody/image.jpg", timeout=5)

        assert response.status_code == 404

    def test_a_malformed_scenario_id_is_rejected(self, chat):
        _, _, _, monitoring = chat

        response = requests.get(f"{monitoring}/scenarios/a%20b/image.jpg", timeout=5)

        assert response.status_code == 400
