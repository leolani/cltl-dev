"""Tier 2: audio crossing a container boundary.

The most deployment-shaped path in the platform, and the one with the most ways
to be wrong that only appear once the modules are in separate containers:

    stub microphone (this process, on the docker host gateway)
      -> cltl-backend records and writes to its own audio storage
      -> AudioSignalStarted names it as `cltl-storage:audio/<id>`
      -> cltl-vad resolves that against http://backend:8000/storage/ and
         fetches the samples back over HTTP, while recording is still running
      -> VadMentionEvent

In tier 1 the same slice runs against a storage service in the same process on
127.0.0.1. Here the URL has to be right on the compose network, the backend has
to be reachable from another container, and the microphone has to be reachable
from inside the network at all — which is what `host.docker.internal` and the
`extra_hosts` entry in the compose file are for.
"""
import pytest

from cltl_integration.drivers import audio
from cltl_integration.drivers.audio import StubAudioServer
from cltl_integration.drivers.scenario import start_scenario
from cltl_integration.topology import BACKEND_VAD

pytestmark = pytest.mark.compose

MIC_TOPIC = "cltl.topic.microphone"
VAD_TOPIC = "cltl.topic.vad"
SCENARIO_TOPIC = "cltl.topic.scenario"

LEAD_SAMPLES = len(audio.silence(audio.LEAD_SILENCE_MS))
SPEECH_SAMPLES = len(audio.speech())

# Containers, a broker and a real recording, so give it room.
AUDIO_TIMEOUT = 90.0


@pytest.fixture
def audio_server():
    servers = []

    def _start(utterances) -> StubAudioServer:
        # 0.0.0.0, not localhost: the containers dial in from the compose
        # network via the host gateway.
        server = StubAudioServer(utterances, host="0.0.0.0").start()
        servers.append(server)
        return server

    yield _start

    for server in reversed(servers):
        server.stop()


@pytest.fixture
def backend_vad(audio_server, compose):
    """Stub microphone first, then a stack pointed at it.

    ``audio_server`` is requested before ``compose`` deliberately: its port has
    to exist before the stack's environment is built, and because pytest tears
    fixtures down in reverse, the containers also stop before the microphone
    disappears from under them.
    """
    def _start(utterances=()):
        server = audio_server(list(utterances))
        runner = compose(
            BACKEND_VAD,
            environment={"CLTL_AUDIO_URL": f"http://host.docker.internal:{server.port}"})
        runner.probe.subscribe(MIC_TOPIC, VAD_TOPIC)

        return runner

    return _start


def _audio_started(event) -> bool:
    return event.payload.type == "AudioSignalStarted"


class TestAudioAcrossContainers:
    def test_the_backend_records_from_a_microphone_outside_the_network(self, backend_vad):
        runner = backend_vad([audio.utterance()])
        start_scenario(runner.event_bus, SCENARIO_TOPIC)

        started = runner.probe.await_event(MIC_TOPIC, _audio_started, timeout=AUDIO_TIMEOUT)

        signal = started.payload.signal
        assert signal.files[0] == f"cltl-storage:audio/{signal.id}"

    def test_vad_fetches_the_audio_from_the_backend_container(self, backend_vad):
        """cltl-vad publishing at all means the cross-container fetch worked.

        It receives no audio on the bus — only a `cltl-storage:` URL it has to
        resolve against `[cltl.backend] storage_url` and GET from a different
        container. A wrong URL there produces no error on this side, just
        silence.
        """
        runner = backend_vad([audio.utterance()])
        start_scenario(runner.event_bus, SCENARIO_TOPIC)

        started = runner.probe.await_event(MIC_TOPIC, _audio_started, timeout=AUDIO_TIMEOUT)
        vad_event = runner.probe.await_event(VAD_TOPIC, timeout=AUDIO_TIMEOUT)

        segment = vad_event.payload.mentions[0].segment[0]
        assert segment.container_id == started.payload.signal.id

    def test_the_segment_brackets_the_speech(self, backend_vad):
        """Offsets are samples, and both containers must agree on that.

        The same assertion as tier 1, and not a duplicate of it: here the
        numbers have been through marshal/unmarshal and the audio through an
        HTTP range request between two containers.
        """
        runner = backend_vad([audio.utterance()])
        start_scenario(runner.event_bus, SCENARIO_TOPIC)

        vad_event = runner.probe.await_event(VAD_TOPIC, timeout=AUDIO_TIMEOUT)

        segment = vad_event.payload.mentions[0].segment[0]
        assert segment.start <= LEAD_SAMPLES
        assert segment.stop >= LEAD_SAMPLES + SPEECH_SAMPLES

    def test_silence_produces_no_voice_activity(self, backend_vad):
        runner = backend_vad([])
        start_scenario(runner.event_bus, SCENARIO_TOPIC)

        runner.probe.await_event(MIC_TOPIC, _audio_started, timeout=AUDIO_TIMEOUT)

        with pytest.raises(AssertionError):
            runner.probe.await_event(VAD_TOPIC, timeout=10.0)
