"""Slice: an image on the bus comes back as objects, a scene and a description.

cltl-vlm-object is the platform's one consumer of an `ImageSignalEvent` that
needs a model. Its llama-server is a `StubLlamaServer`, so what this covers is
everything *around* the model, which is where the module can break without
anyone noticing:

* the signal's `cltl-storage:image/<id>` reference is resolved against
  cltl-backend's storage and the pixels reach the model, as a PNG data URL;
* the model's answer — boxes normalised to 0-1000 — becomes mentions in image
  pixels, with the scene and its description as whole-image mentions;
* the event is published under the *scenario of the image*, not a fresh one;
* a model that answers with something that is not JSON does not take the module
  down: the image is annotated as empty and the next one is still handled.

The real model is not exercised here. That needs a GPU and a ~5 GB download, and
nothing in this module changes what it would answer.
"""
import pytest

from cltl_integration.drivers.chat import ChatClient
from cltl_integration.drivers.image import bounds_of, label_of, png
from cltl_integration.drivers.scenario import start_scenario
from cltl_integration.drivers.vlm import StubLlamaServer
from cltl_integration.topology import CHATUI_VLM

IMAGE_TOPIC = "cltl.topic.image"
OBJECT_TOPIC = "cltl.topic.object_recognition"
SCENARIO_TOPIC = "cltl.topic.scenario"

WIDTH, HEIGHT = 100, 80
PNG = png(WIDTH, HEIGHT, (0, 128, 255))

TIMEOUT = 10.0


@pytest.fixture
def llama():
    with StubLlamaServer() as server:
        yield server


@pytest.fixture
def chat(llama, inprocess):
    """Stub model first, then the topology pointed at it, then an open scenario.

    The stub is requested before ``inprocess`` because its port goes into the
    configuration, and reverse-order teardown then stops the topology before the
    model disappears from under a request in flight.
    """
    runner = inprocess(CHATUI_VLM, environment={"CLTL_VLM_URL": llama.url})
    runner.probe.subscribe(OBJECT_TOPIC)

    scenario = start_scenario(runner.event_bus, SCENARIO_TOPIC)
    client = ChatClient(runner.url("chatui"))
    client.await_scenario()

    return runner, client, scenario


def _submit(client):
    chat_id = client.start_session()
    upload = client.upload_image(chat_id, PNG, WIDTH, HEIGHT)
    client.annotate(chat_id, upload["id"], [])


def _labelled(event):
    return {label_of(mention): mention for mention in event.payload.mentions}


class TestRecognition:
    def test_the_model_is_shown_the_uploaded_image(self, chat, llama):
        _, client, _ = chat

        _submit(client)

        content = llama.await_request()[0]["messages"][0]["content"]
        image = next(part for part in content if part["type"] == "image_url")
        assert image["image_url"]["url"].startswith("data:image/png;base64,")

    def test_objects_and_scene_are_published(self, chat):
        runner, client, _ = chat

        _submit(client)

        event = runner.probe.await_event(OBJECT_TOPIC, timeout=TIMEOUT)
        mentions = _labelled(event)
        assert {"office", "A chair and a lamp in a small office.", "chair", "lamp"} <= set(mentions)

    def test_boxes_are_in_image_pixels(self, chat):
        """The model reports 0-1000; the mention must be in the image's own pixels."""
        runner, client, _ = chat

        _submit(client)

        mentions = _labelled(runner.probe.await_event(OBJECT_TOPIC, timeout=TIMEOUT))
        assert bounds_of(mentions["chair"]) == (0, 0, WIDTH // 2, HEIGHT // 2)
        assert bounds_of(mentions["lamp"]) == (WIDTH // 2, HEIGHT // 2, WIDTH, HEIGHT)

    def test_the_event_belongs_to_the_scenario_of_the_image(self, chat):
        runner, client, scenario = chat

        _submit(client)

        event = runner.probe.await_event(OBJECT_TOPIC, timeout=TIMEOUT)
        assert event.metadata.scenario_id == scenario.id


class TestUnusableModelAnswer:
    def test_an_answer_that_is_not_json_yields_an_empty_annotation(self, chat, llama):
        runner, client, _ = chat
        llama.reply = "I see a room, but I will not tell you about it."

        _submit(client)

        event = runner.probe.await_event(OBJECT_TOPIC, timeout=TIMEOUT)
        assert len(event.payload.mentions) == 1

    def test_the_next_image_is_still_recognised(self, chat, llama):
        runner, client, _ = chat
        llama.reply = "not json"
        _submit(client)
        runner.probe.await_event(OBJECT_TOPIC, timeout=TIMEOUT)

        llama.reply = {"scene": "kitchen", "scene_description": "A kitchen.", "objects": []}
        _submit(client)

        event = runner.probe.await_event(
            OBJECT_TOPIC, timeout=TIMEOUT,
            predicate=lambda e: "kitchen" in _labelled(e))
        assert event is not None
