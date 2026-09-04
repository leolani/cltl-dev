"""Tier 2, two stacks: the platform deployed as a client and a server.

    client project                          server project
    ----------------------------            ----------------------------
    cltl-backend  (devices, remote          cltl-backend  (storage_main.py)
                   storage)          --->   cltl-emissor-data
    cltl-context  (BDI handshake)    --->   cltl-eliza
    cltl-chat-ui                            rabbitmq

Two compose projects on two bridge networks. Nothing resolves across them: the
client reaches the broker and the storage service through the host, at the ports
the server project happened to publish, exactly as it would across a room. See
``cltl_integration.runner.split``.

What this covers that no single stack does:

* the BDI handshake completes with cltl-context and cltl-eliza in different
  deployments, so every intention and desire crosses a broker the client does
  not own;
* the client stores no audio. It PUTs to the server and the server's cltl-vad
  and cltl-asr read it back — a URL a monolith gets right by accident, because
  its local fallback happens to point at the same disk;
* the server runs ``src/storage_main.py``, which is ``StorageContainer`` rather
  than ``BackendContainer``. Nothing else in the suite starts that entry point.
"""
import json
import time

import numpy as np
import pytest
import requests

from cltl_integration.drivers import audio
from cltl_integration.drivers.audio import StubAudioServer
from cltl_integration.drivers.conversation import (TEXT_IN, TEXT_OUT, TOPICS,
                                                   Conversation)
from cltl_integration.runner.split import CLIENT_ENTRY_POINT, SERVER_ENTRY_POINT
from cltl_integration.topology import CSPLIT, CSPLIT_AUDIO

pytestmark = pytest.mark.compose

AUDIO_ID = "csplit-round-trip"
CONTENT_TYPE = (f"audio/L16; rate={audio.RATE}; channels={audio.CHANNELS}; "
                f"frame_size={audio.FRAME_SIZE}")

PERSIST_TIMEOUT = 15.0
# The client records in real time, uploads over HTTP to another stack, and
# Whisper then loads a 139 MB model and transcribes on CPU. None of that is a
# hang.
SPOKEN_TIMEOUT = 300.0

SPOKEN_UTTERANCE = "Hello, I feel very anxious today"


@pytest.fixture
def conversation(split):
    runner = split(CSPLIT)
    runner.probe.subscribe(*TOPICS)

    return Conversation(runner)


class TestSplitConversation:
    """The handshake and the conversation, with the halves on separate networks."""

    def test_the_handshake_crosses_the_split(self, conversation):
        """cltl-context is on the client; the agent it hands over to is not.

        ``open()`` publishes the ``init`` intention and waits for the scenario
        and the greeting — both produced on the client. ``confirm()`` waits for
        the ``initialized`` desire and the ``eliza`` intention, which is where
        the server's ElizaService is switched on. Every one of those events was
        routed by a broker in the other project.
        """
        conversation.open().confirm()

        assert conversation.scenario_id
        assert conversation.client.current()["scenario_id"] == conversation.scenario_id

    def test_the_agent_on_the_server_answers_the_client(self, conversation):
        conversation.open().confirm()

        conversation.say("I feel very anxious today")

        assert conversation.replies(), "Eliza never answered across the split"

    def test_the_conversation_is_recorded_on_the_server(self, conversation):
        """cltl-emissor-data is deployed server-side, and the client has no copy.

        A split where the client quietly kept its own scenario would look
        identical from the chat UI and be worthless as a deployment.
        """
        conversation.open().confirm()
        conversation.say("My mother never listens to me")
        reply = conversation.runner.probe.await_event(
            TEXT_OUT, lambda event: bool(event.payload.signal.text), timeout=30)

        texts = _await_texts(conversation.runner.storage_path,
                             conversation.scenario_id, count=2)

        assert reply.payload.signal.text in texts
        assert not list((conversation.runner.client.storage_path / "emissor").glob("*"))


class TestEntryPoints:
    """The two backends are the same image running two different programs.

    Asserted structurally because behaviourally it is nearly invisible: with the
    microphone off, ``main.py`` and ``storage_main.py`` serve the same
    ``/storage`` and the same ``/health``, so every other test in this file
    passes either way. It only shows up in the spoken split, as three hundred
    seconds of silence.

    It has already gone wrong once. ``ComposeStack._compose_env`` starts from
    ``os.environ``, and the server runner puts its own ``CLTL_BACKEND_MAIN``
    there, so the client inherited the server's entry point and came up with no
    microphone at all.
    """

    def test_the_two_halves_run_different_programs(self, split):
        stack = split(CSPLIT)

        assert stack.server.service_command("backend") == ("python", SERVER_ENTRY_POINT)
        assert stack.client.service_command("backend") == ("python", CLIENT_ENTRY_POINT)


class TestRemoteStorage:
    """``audio_storage: remote`` — the client's storage endpoint is a proxy.

    Driven over HTTP rather than through the microphone, so that the assertion
    is about the storage boundary alone and does not have to wait for a
    recording. The spoken test below covers the same path with a real signal.
    """

    @pytest.fixture
    def stack(self, split):
        return split(CSPLIT)

    def test_audio_put_to_the_client_is_stored_on_the_server(self, stack):
        samples = audio.utterance()

        response = requests.put(f"{stack.client.url('backend')}/audio/{AUDIO_ID}",
                                data=samples.tobytes(),
                                headers={"Content-Type": CONTENT_TYPE}, timeout=30)
        assert response.status_code == 204, response.text

        served = requests.get(f"{stack.server.url('backend')}/audio/{AUDIO_ID}",
                              timeout=30)
        assert served.status_code == 200, served.text
        assert np.array_equal(np.frombuffer(served.content, dtype=np.int16), samples)

    def test_the_samples_are_on_the_server_disk_and_not_the_client_disk(self, stack):
        """Where the bytes actually end up, rather than where an API says they are.

        Both halves mount a storage directory, and both are empty at the start.
        Only the server's may have anything in it afterwards — the client's
        ``RemoteAudioStorage`` has no local path at all, and a fallback to
        ``CachedAudioStorage`` would show up here as a file on the near side.
        """
        requests.put(f"{stack.client.url('backend')}/audio/{AUDIO_ID}",
                     data=audio.utterance().tobytes(),
                     headers={"Content-Type": CONTENT_TYPE}, timeout=30).raise_for_status()

        _await_file(stack.server.storage_path / "audio" / f"{AUDIO_ID}.wav")
        assert not list((stack.client.storage_path / "audio").glob("*"))


class TestSplitAudio:
    """Speech captured on the client, recognised on the server.

    The longest path in the suite: the samples enter the client's backend from a
    stub microphone on the host, are uploaded to the server's storage over HTTP,
    are fetched back from there by cltl-vad and again by cltl-asr, and come out
    as text on a topic cltl-context is listening to from the other stack.
    """

    pytestmark = pytest.mark.slow

    @pytest.fixture
    def spoken(self, split):
        if not audio.speech_available():
            pytest.skip("espeak-ng is not installed (sudo apt-get install -y espeak-ng)")

        server = StubAudioServer([audio.spoken(SPOKEN_UTTERANCE)], host="0.0.0.0").start()
        try:
            runner = split(
                CSPLIT_AUDIO,
                client_environment={
                    "CLTL_AUDIO_URL": f"http://host.docker.internal:{server.port}"})
            runner.probe.subscribe(*TOPICS)
            yield Conversation(runner)
        finally:
            server.stop()

    def test_speech_recorded_on_the_client_is_transcribed_on_the_server(self, spoken):
        """Whisper, in a container on the far side, on audio it fetched back.

        ``open()`` starts the scenario, which is what releases the client's
        recording thread; the transcript is the first thing to arrive on
        ``text_in`` afterwards.
        """
        spoken.open()

        transcribed = spoken.runner.probe.await_event(TEXT_IN, timeout=SPOKEN_TIMEOUT)

        assert "anxious" in transcribed.payload.signal.text.lower(), (
            f"expected the spoken utterance, got "
            f"{transcribed.payload.signal.text!r}")

    def test_the_recording_is_stored_on_the_server(self, spoken):
        """The audio the server transcribed is on the server's disk, not the client's."""
        spoken.open()
        spoken.runner.probe.await_event(TEXT_IN, timeout=SPOKEN_TIMEOUT)

        stored = list((spoken.runner.server.storage_path / "audio").glob("*.wav"))

        assert stored, "the client's recording never reached the server's storage"
        assert not list((spoken.runner.client.storage_path / "audio").glob("*"))


# -- helpers ----------------------------------------------------------------

def _await_file(path, timeout: float = PERSIST_TIMEOUT):
    end = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= end:
            raise AssertionError(f"{path} was not written within {timeout}s")
        time.sleep(0.05)

    return path


def _await_texts(storage_path, scenario_id: str, count: int,
                 timeout: float = PERSIST_TIMEOUT):
    path = storage_path / "emissor" / scenario_id / "text.json"
    end = time.monotonic() + timeout
    texts = []
    while time.monotonic() < end:
        if path.exists():
            texts = [signal["text"] for signal in json.loads(path.read_text())]
            if len(texts) >= count:
                return texts
        time.sleep(0.05)

    raise AssertionError(f"expected {count} text signal(s) in {path}, found {texts}")
