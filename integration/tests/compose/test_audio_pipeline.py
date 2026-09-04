"""Tier 2, end to end: someone speaks, the agent answers in the chat UI.

    espeak-ng (this process)
      -> cltl-backend records and stores the audio
      -> cltl-vad fetches it back over HTTP and finds the speech
      -> cltl-asr fetches the segment and Whisper transcribes it
      -> cltl-eliza answers on cltl.topic.text_out
      -> cltl-chat-ui shows the answer over HTTP

Six processes, a broker, an HTTP storage service and a real speech model. It is
the slowest thing in the suite by a wide margin and it duplicates coverage every
other test already provides — which is the point of having exactly one of them.
What it adds is the composition itself: every slice can pass while the chain
still fails, because a slice test supplies its own stimulus and this does not.

Offline speech, deliberately. The prior art in ``app/docker-app`` synthesises
utterances with gTTS, so its audio tests need the network, take seconds per
utterance and are never quite the same twice. espeak-ng is installed from apt,
produces identical samples every run, and Whisper transcribes it verbatim.
"""
import pytest

from cltl_integration.drivers import audio
from cltl_integration.drivers.audio import StubAudioServer
from cltl_integration.drivers.chat import ChatClient
from cltl_integration.drivers.scenario import start_scenario
from cltl_integration.topology import AUDIO_PIPELINE

pytestmark = [pytest.mark.compose, pytest.mark.slow]

MIC_TOPIC = "cltl.topic.microphone"
VAD_TOPIC = "cltl.topic.vad"
TEXT_IN = "cltl.topic.text_in"
TEXT_OUT = "cltl.topic.text_out"
SCENARIO_TOPIC = "cltl.topic.scenario"

UTTERANCE = "Hello, I feel very anxious today"

# Recording is paced at real time, Whisper loads a 139 MB model on first use and
# transcribes on CPU. None of that is a hang.
PIPELINE_TIMEOUT = 240.0


@pytest.fixture
def spoken_pipeline(compose):
    if not audio.speech_available():
        pytest.skip("espeak-ng is not installed (sudo apt-get install -y espeak-ng)")

    servers = []

    def _start(text: str = UTTERANCE):
        server = StubAudioServer([audio.spoken(text)], host="0.0.0.0").start()
        servers.append(server)
        runner = compose(
            AUDIO_PIPELINE,
            environment={"CLTL_AUDIO_URL": f"http://host.docker.internal:{server.port}"})
        runner.probe.subscribe(MIC_TOPIC, VAD_TOPIC, TEXT_IN, TEXT_OUT)

        return runner

    yield _start

    for server in reversed(servers):
        server.stop()


class TestSpokenConversation:
    def test_speech_is_transcribed(self, spoken_pipeline):
        """Whisper, in its own container, on audio it fetched from another."""
        runner = spoken_pipeline()
        start_scenario(runner.event_bus, SCENARIO_TOPIC)

        transcribed = runner.probe.await_event(TEXT_IN, timeout=PIPELINE_TIMEOUT)

        assert "anxious" in transcribed.payload.signal.text.lower(), (
            f"expected the spoken utterance, got "
            f"{transcribed.payload.signal.text!r}")

    def test_the_agent_answers_what_was_said(self, spoken_pipeline):
        """The whole chain, ending where a user would see it."""
        runner = spoken_pipeline()
        scenario = start_scenario(runner.event_bus, SCENARIO_TOPIC)

        transcribed = runner.probe.await_event(TEXT_IN, timeout=PIPELINE_TIMEOUT)
        reply = runner.probe.await_event(
            TEXT_OUT, lambda event: bool(event.payload.signal.text),
            timeout=PIPELINE_TIMEOUT)

        assert reply.payload.signal.text != transcribed.payload.signal.text

        client = ChatClient(runner.url("chatui"))
        client.await_scenario(timeout=30)
        history = client.fetch_all(client.current()["id"])
        assert any(reply.payload.signal.text == utterance["text"]
                   for utterance in history), (
            f"the reply never reached the chat UI: {history}")
        assert scenario.id
