"""Slice: cltl-backend -> cltl-vad, across `cltl.topic.microphone`.

The seam here is wider than one event. cltl-backend records from its microphone,
writes the samples into audio storage as they arrive, and publishes an
``AudioSignalStarted`` whose ``files[0]`` is a ``cltl-storage:audio/<id>`` URL.
cltl-vad does not receive any audio on the bus at all: it takes that URL and
fetches the samples back over HTTP from the storage service, repeatedly and at
increasing offsets, while the recording is still in progress.

So this test needs the real storage server running — which is why cltl-backend
is in the topology for more than its microphone — and it needs something on the
other end of ``[cltl.backend] server_audio_url`` speaking the streaming audio
protocol. See ``cltl_integration.drivers.audio``.

What that buys is coverage of the failure modes that only appear when the two
modules are wired together: a storage URL the VAD side cannot resolve, an offset
convention that disagrees between writer and reader, or a recording gated on a
scenario that never starts.
"""
import pytest

from cltl_integration.drivers import audio
from cltl_integration.drivers.audio import StubAudioServer
from cltl_integration.drivers.scenario import start_scenario
from cltl_integration.topology import BACKEND_VAD

MIC_TOPIC = "cltl.topic.microphone"
VAD_TOPIC = "cltl.topic.vad"
SCENARIO_TOPIC = "cltl.topic.scenario"

LEAD_SAMPLES = len(audio.silence(audio.LEAD_SILENCE_MS))
SPEECH_SAMPLES = len(audio.speech())


@pytest.fixture
def audio_server():
    servers = []

    def _start(utterances) -> StubAudioServer:
        server = StubAudioServer(utterances).start()
        servers.append(server)
        return server

    yield _start

    for server in reversed(servers):
        server.stop()


@pytest.fixture
def backend_vad(audio_server, inprocess):
    """Start the stub microphone, then a topology pointed at it.

    ``audio_server`` is requested before ``inprocess`` on purpose. The stub's
    port goes into the configuration, so it has to exist first — and because
    pytest tears fixtures down in reverse order of setup, that also means the
    topology stops before the microphone disappears from under its recording
    thread.
    """
    def _start(utterances=(), **kwargs):
        server = audio_server(list(utterances))
        runner = inprocess(BACKEND_VAD,
                           environment={"CLTL_AUDIO_URL": server.url}, **kwargs)
        runner.probe.subscribe(MIC_TOPIC, VAD_TOPIC)

        return runner

    return _start


def _audio_started(event) -> bool:
    return event.payload.type == "AudioSignalStarted"


class TestRecording:
    def test_microphone_records_only_within_a_scenario(self, backend_vad):
        """BackendService's mic thread idles until it sees a scenario.

        Worth pinning down: the thread is running and the stub is serving, so
        "nothing happens" here is a deliberate gate rather than a broken wire.
        """
        runner = backend_vad([audio.utterance()])

        with pytest.raises(AssertionError):
            runner.probe.await_event(MIC_TOPIC, timeout=2.0)

    def test_recording_publishes_a_storage_backed_audio_signal(self, backend_vad):
        runner = backend_vad([audio.utterance()])
        start_scenario(runner.event_bus, SCENARIO_TOPIC)

        started = runner.probe.await_event(MIC_TOPIC, _audio_started)

        signal = started.payload.signal
        assert signal.files, "the audio signal carries no storage URL"
        assert signal.files[0] == f"cltl-storage:audio/{signal.id}", (
            "cltl-vad resolves this URL against [cltl.backend] storage_url; any "
            "other shape is unfetchable")

    def test_recording_is_bracketed_by_started_and_stopped(self, backend_vad):
        """cltl-vad keeps reading until it sees the stop, so both must arrive."""
        runner = backend_vad([audio.utterance()])
        start_scenario(runner.event_bus, SCENARIO_TOPIC)

        started = runner.probe.await_event(MIC_TOPIC, _audio_started)
        stopped = runner.probe.await_event(
            MIC_TOPIC, lambda event: event.payload.type == "AudioSignalStopped")

        assert stopped.payload.signal.id == started.payload.signal.id


class TestVoiceActivity:
    def test_speech_produces_a_vad_event(self, backend_vad):
        runner = backend_vad([audio.utterance()])
        start_scenario(runner.event_bus, SCENARIO_TOPIC)

        vad_event = runner.probe.await_event(VAD_TOPIC, timeout=20.0)

        assert vad_event.payload.mentions, "VAD event without a mention"

    def test_vad_segment_points_back_at_the_recorded_signal(self, backend_vad):
        """The segment's container id is how cltl-asr finds the audio to fetch."""
        runner = backend_vad([audio.utterance()])
        start_scenario(runner.event_bus, SCENARIO_TOPIC)

        started = runner.probe.await_event(MIC_TOPIC, _audio_started)
        vad_event = runner.probe.await_event(VAD_TOPIC, timeout=20.0)

        segment = vad_event.payload.mentions[0].segment[0]
        assert segment.container_id == started.payload.signal.id

    def test_vad_segment_brackets_the_speech(self, backend_vad):
        """Offsets are in samples, and both modules must agree on that.

        A units mismatch (frames vs samples vs bytes) still produces a plausible
        looking event, and only shows up as cltl-asr transcribing the wrong slice
        of audio. Asserting the segment contains the speech, with the padding
        cltl-vad is configured to add, is what catches it.
        """
        runner = backend_vad([audio.utterance()])
        start_scenario(runner.event_bus, SCENARIO_TOPIC)

        vad_event = runner.probe.await_event(VAD_TOPIC, timeout=20.0)

        segment = vad_event.payload.mentions[0].segment[0]
        assert segment.start <= LEAD_SAMPLES, (
            f"segment starts at {segment.start}, after the speech onset at "
            f"{LEAD_SAMPLES}: the configured padding was not applied")
        assert segment.stop >= LEAD_SAMPLES + SPEECH_SAMPLES, (
            f"segment ends at {segment.stop}, before the speech ends at "
            f"{LEAD_SAMPLES + SPEECH_SAMPLES}")

    def test_silence_produces_no_vad_event(self, backend_vad):
        """The stub serves silence once its utterances run out — here, at once."""
        runner = backend_vad([])
        start_scenario(runner.event_bus, SCENARIO_TOPIC)

        runner.probe.await_event(MIC_TOPIC, _audio_started, timeout=20.0)

        with pytest.raises(AssertionError):
            runner.probe.await_event(VAD_TOPIC, timeout=3.0)
