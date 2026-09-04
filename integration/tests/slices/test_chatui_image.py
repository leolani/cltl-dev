"""Slice: a person uploads an image, marks regions on it, and submits.

The chat UI is the only module in the platform that turns a human gesture into
an `ImageSignal`. Everything else that produces one — the camera in
cltl-backend, an object detector — runs unattended, so this is the one path
where the annotation's *content* came from a person and the coordinates came
from a browser.

Three things are checked, and they fail independently:

* the signal and its mentions reach the bus, with the regions the person drew;
* nothing is published on `cltl.topic.text_in`. cltl-eliza consumes that topic,
  so a stray publish here would make the agent answer every picture. cltl-eliza
  is not in this topology — the assertion is on the event, not on the silence
  of a module that is not running;
* the PNG itself lands in the scenario folder. That is the only thing that
  exercises the whole chain — chat-ui PUTs the pixels to cltl-backend's image
  storage, publishes a `cltl-storage:image/<id>` reference, and
  cltl-emissor-data resolves that reference back over HTTP. Asserted separately
  from the mentions, because "pixels lost" and "annotations lost" are different
  bugs with the same symptom on a naive test.
"""
import json
import time

import pytest

from cltl_integration.drivers.chat import ChatClient
from cltl_integration.drivers.image import bounds_of, label_of, png, region
from cltl_integration.drivers.scenario import start_scenario
from cltl_integration.topology import CHATUI_IMAGE

IMAGE_TOPIC = "cltl.topic.image"
TEXT_IN = "cltl.topic.text_in"
SCENARIO_TOPIC = "cltl.topic.scenario"

WIDTH, HEIGHT = 64, 48
PNG = png(WIDTH, HEIGHT, (255, 0, 0))

REGIONS = [region(0, 0, 32, 24, "a chair"),
           region(32, 24, 64, 48, "a lamp")]

TIMEOUT = 10.0


@pytest.fixture
def chat(inprocess):
    """The topology, a chat client, and an open scenario to record into."""
    runner = inprocess(CHATUI_IMAGE)
    runner.probe.subscribe(IMAGE_TOPIC, TEXT_IN)

    scenario = start_scenario(runner.event_bus, SCENARIO_TOPIC)
    client = ChatClient(runner.url("chatui"))
    client.await_scenario()

    return runner, client, scenario


def _submit(client, chat_id, regions=REGIONS):
    upload = client.upload_image(chat_id, PNG, WIDTH, HEIGHT)
    result = client.annotate(chat_id, upload["id"], regions)

    return upload, result


def _await_file(path, timeout: float = TIMEOUT):
    """Persistence runs on cltl-emissor-data's worker, not the caller's thread."""
    end = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= end:
            raise AssertionError(f"{path} was not written within {timeout}s")
        time.sleep(0.02)

    return path


def _await_signals(runner, scenario_id: str, count: int, timeout: float = TIMEOUT):
    path = runner.storage_path / "emissor" / scenario_id / "image.json"
    end = time.monotonic() + timeout
    signals = []
    while time.monotonic() < end:
        if path.exists():
            signals = json.loads(path.read_text())
            if len(signals) >= count:
                return signals
        time.sleep(0.02)

    raise AssertionError(f"expected {count} image signal(s) in {path}, found {signals}")


class TestUpload:
    def test_the_upload_comes_back_unchanged(self, chat):
        _, client, _ = chat
        chat_id = client.start_session()

        upload = client.upload_image(chat_id, PNG, WIDTH, HEIGHT)

        assert client.fetch_image(chat_id, upload["id"]) == PNG

    def test_uploading_publishes_nothing(self, chat):
        """Drawing on an image is not yet a contribution to the conversation."""
        runner, client, _ = chat
        chat_id = client.start_session()

        client.upload_image(chat_id, PNG, WIDTH, HEIGHT)
        time.sleep(0.5)

        assert not runner.probe.events(IMAGE_TOPIC)
        assert not runner.probe.events(TEXT_IN)

    def test_an_uploaded_image_can_be_discarded(self, chat):
        _, client, _ = chat
        chat_id = client.start_session()
        upload = client.upload_image(chat_id, PNG, WIDTH, HEIGHT)

        client.delete_image(chat_id, upload["id"])

        with pytest.raises(Exception):
            client.fetch_image(chat_id, upload["id"])


class TestAnnotationEvent:
    def test_submit_publishes_the_signal_with_its_mentions(self, chat):
        runner, client, scenario = chat
        chat_id = client.start_session()

        upload, result = _submit(client, chat_id)

        event = runner.probe.await_event(
            IMAGE_TOPIC, lambda e: e.payload.signal.id == upload["id"], timeout=TIMEOUT)
        signal = event.payload.signal
        assert signal.time.container_id == scenario.id
        assert list(signal.ruler.bounds) == [0, 0, WIDTH, HEIGHT]
        assert signal.files == [f"cltl-storage:image/{upload['id']}"]
        assert result["mentions"] == 2
        assert [bounds_of(mention) for mention in signal.mentions] == [(0, 0, 32, 24),
                                                                      (32, 24, 64, 48)]
        assert [label_of(mention) for mention in signal.mentions] == ["a chair", "a lamp"]

    def test_submit_says_nothing_on_the_utterance_topic(self, chat):
        """The regression that would make the agent answer every picture."""
        runner, client, _ = chat
        chat_id = client.start_session()

        upload, _ = _submit(client, chat_id)

        runner.probe.await_event(
            IMAGE_TOPIC, lambda e: e.payload.signal.id == upload["id"], timeout=TIMEOUT)
        assert not runner.probe.events(TEXT_IN)

    def test_the_image_is_echoed_into_the_transcript(self, chat):
        _, client, _ = chat
        chat_id = client.start_session()

        upload, _ = _submit(client, chat_id)

        utterances = client.fetch_all(chat_id)
        echoes = [u for u in utterances if u["content_type"] == "text/html"]
        assert len(echoes) == 1
        assert "<img src=" in echoes[0]["text"]
        assert upload["id"] in echoes[0]["text"]
        assert "a chair" in echoes[0]["text"]


class TestPersistence:
    def test_the_signal_is_written_to_the_scenario(self, chat):
        runner, client, scenario = chat
        chat_id = client.start_session()
        _await_file(runner.storage_path / "emissor" / scenario.id / f"{scenario.id}.json")

        upload, _ = _submit(client, chat_id)

        signals = _await_signals(runner, scenario.id, 1)
        assert [signal["id"] for signal in signals] == [upload["id"]]
        assert [label_of(mention) for mention in signals[0]["mentions"]] == ["a chair", "a lamp"]

    def test_the_pixels_reach_the_scenario_folder(self, chat):
        """chat-ui PUT -> backend storage -> cltl-storage: -> emissor-data GET.

        Separate from the mentions on purpose: losing the PNG and losing the
        annotations are different faults, and this is the only assertion that
        covers the storage round trip at all.
        """
        runner, client, scenario = chat
        chat_id = client.start_session()
        _await_file(runner.storage_path / "emissor" / scenario.id / f"{scenario.id}.json")

        upload, _ = _submit(client, chat_id)

        _await_signals(runner, scenario.id, 1)
        stored = _await_file(
            runner.storage_path / "emissor" / scenario.id / "image" / f"{upload['id']}.png")
        assert stored.stat().st_size > 0

    def test_a_submission_without_regions_still_records_the_image(self, chat):
        runner, client, scenario = chat
        chat_id = client.start_session()

        upload, result = _submit(client, chat_id, regions=[])

        assert result["mentions"] == 0
        signals = _await_signals(runner, scenario.id, 1)
        assert signals[0]["id"] == upload["id"]
        assert signals[0]["mentions"] == []
