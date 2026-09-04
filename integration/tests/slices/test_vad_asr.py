"""Slice: cltl-vad -> cltl-asr, across `cltl.topic.vad`.

What travels between these two modules is not audio. cltl-vad publishes a
``VadMentionEvent`` holding an ``Index`` — a container id and a sample range —
and cltl-asr turns that into ``cltl-storage:audio/<container_id>`` and fetches
exactly that range back from the storage service. cltl-backend is in the
topology purely to serve that endpoint.

So the contract has three parts, and only the first is visible in the event:

1. the container id names an audio signal the storage service knows about,
2. the offsets are **samples**, understood identically on both sides,
3. the answer comes back as an ``AsrTextSignalEvent`` on the ASR topic.

The test drives the VAD event directly rather than running cltl-vad, so a
failure here is about the boundary and not about whether WebRtcVAD found the
speech — that is ``test_backend_vad.py``'s job.

The recogniser is a stub. Every implementation ``ASRContainer`` can build needs
torch or a cloud account, and transcription accuracy is not what a slice test
measures. The real ``AsrService`` is left in place, so the audio fetch, the
segment bookkeeping and the published payload are all shipped code; only
``speech_to_text`` is replaced, and the stub records what it was given so that
(2) can actually be asserted instead of assumed.
"""
import numpy as np
import pytest
from cltl.combot.event.emissor import ConversationalAgent
from cltl.combot.infra.event import Event
from cltl_service.asr.schema import AsrTextSignalEvent
from cltl_service.vad.schema import VadAnnotation, VadMentionEvent
from emissor.representation.container import Index

from cltl_integration.drivers import audio
from cltl_integration.drivers.asr import RecordingASR, asr_override
from cltl_integration.topology import VAD_ASR

VAD_TOPIC = "cltl.topic.vad"
TEXT_IN = "cltl.topic.text_in"

SCENARIO = "test-scenario"
TRANSCRIPT = "the stub heard something"

SPEECH_START = len(audio.silence(audio.LEAD_SILENCE_MS))
SPEECH_STOP = SPEECH_START + len(audio.speech())


def _vad_event(audio_id: str, start: int, stop: int) -> VadMentionEvent:
    """Build the event exactly as VadService._create_payload does."""
    segment = Index.from_range(audio_id, start, stop)
    annotation = VadAnnotation.for_activation(1.0, "WebRtcVAD")

    return VadMentionEvent.create(segment, annotation)


@pytest.fixture
def vad_asr(inprocess):
    """A topology whose recogniser is a stub, returned alongside the runner."""
    def _start():
        asr = RecordingASR(TRANSCRIPT)
        runner = inprocess(VAD_ASR, container_overrides=asr_override(asr))
        runner.probe.subscribe(TEXT_IN)

        return runner, asr

    return _start


def _store(runner, samples: np.ndarray) -> str:
    """Put audio into the backend's storage and return its id."""
    audio_id = "test-audio-signal"
    runner.container.audio_storage.store(audio_id, audio.frames(samples), audio.RATE)

    return audio_id


def _publish(runner, payload) -> None:
    runner.event_bus.publish(VAD_TOPIC, Event.for_scenario_payload(SCENARIO, payload))


class TestTranscription:
    def test_voice_activity_becomes_a_text_signal(self, vad_asr):
        runner, _ = vad_asr()
        audio_id = _store(runner, audio.utterance())

        _publish(runner, _vad_event(audio_id, SPEECH_START, SPEECH_STOP))

        transcribed = runner.probe.await_event(TEXT_IN)

        assert isinstance(transcribed.payload, AsrTextSignalEvent)
        assert transcribed.payload.signal.text == TRANSCRIPT

    def test_asr_receives_exactly_the_segment_it_was_pointed_at(self, vad_asr):
        """The assertion the storage round-trip exists for.

        A wrong offset unit, an off-by-one in the range parameters, or a storage
        service that ignores them all still produce a perfectly well-formed text
        event — with the wrong audio behind it. The sample count is the only
        thing that notices.
        """
        runner, asr = vad_asr()
        audio_id = _store(runner, audio.utterance())

        _publish(runner, _vad_event(audio_id, SPEECH_START, SPEECH_STOP))
        runner.probe.await_event(TEXT_IN)

        assert len(asr.calls) == 1
        assert asr.calls[0].samples == SPEECH_STOP - SPEECH_START
        assert asr.calls[0].sampling_rate == audio.RATE

    def test_text_signal_keeps_the_scenario(self, vad_asr):
        """cltl-emissor-data files signals by scenario; losing it drops the turn."""
        runner, _ = vad_asr()
        audio_id = _store(runner, audio.utterance())

        _publish(runner, _vad_event(audio_id, SPEECH_START, SPEECH_STOP))

        transcribed = runner.probe.await_event(TEXT_IN)

        assert transcribed.metadata.scenario_id == SCENARIO

    def test_text_signal_references_the_audio_segment(self, vad_asr):
        """The transcript stays traceable to the audio it came from.

        ``AsrTextSignalEvent`` carries the ranges in ``audio_segment``, gathered
        from every VAD mention that contributed to the utterance.
        """
        runner, _ = vad_asr()
        audio_id = _store(runner, audio.utterance())

        _publish(runner, _vad_event(audio_id, SPEECH_START, SPEECH_STOP))

        transcribed = runner.probe.await_event(TEXT_IN)

        segments = transcribed.payload.audio_segment
        assert [(segment.container_id, segment.start, segment.stop)
                for segment in segments] == [(audio_id, SPEECH_START, SPEECH_STOP)]


class TestAgentAnnotation:
    """Who said it — the one field three producers disagree about."""

    @pytest.mark.xfail(
        strict=True,
        reason="cltl-asr/src/cltl_service/asr/schema.py:16 calls "
               "add_agent_annotation(signal, ConversationalAgent.SPEAKER), passing "
               "the enum member where TextSignalEvent.for_speaker/for_agent both "
               "pass ConversationalAgent.SPEAKER.name. So the same fact is "
               "represented three ways: 'SPEAKER' from every other producer, the "
               "enum object from cltl-asr in tier 1, and 'speaker' from cltl-asr "
               "in tier 2, where marshal/unmarshal turns the enum into its "
               "lowercased name. Fix: append .name, as in cltl-combot.")
    def test_asr_annotates_the_speaker_like_every_other_producer(self, vad_asr):
        runner, _ = vad_asr()
        audio_id = _store(runner, audio.utterance())

        _publish(runner, _vad_event(audio_id, SPEECH_START, SPEECH_STOP))

        transcribed = runner.probe.await_event(TEXT_IN)

        annotations = [annotation
                       for mention in transcribed.payload.signal.mentions
                       for annotation in mention.annotations
                       if annotation.type == ConversationalAgent.__name__]
        assert [annotation.value for annotation in annotations] == \
               [ConversationalAgent.SPEAKER.name]


class TestEmptyDetections:
    """cltl-vad publishes an event per detection round, including empty ones."""

    def test_empty_segment_is_not_transcribed(self, vad_asr):
        runner, asr = vad_asr()
        audio_id = _store(runner, audio.utterance())

        _publish(runner, _vad_event(audio_id, SPEECH_START, SPEECH_START))

        with pytest.raises(AssertionError):
            runner.probe.await_event(TEXT_IN, timeout=2.0)
        assert not asr.calls, "the recogniser was called for an empty segment"

    def test_mention_without_a_segment_is_not_transcribed(self, vad_asr):
        runner, asr = vad_asr()
        _store(runner, audio.utterance())

        payload = VadMentionEvent.create(None, VadAnnotation.for_activation(0.0, "WebRtcVAD"))
        payload.mentions[0].segment = []
        _publish(runner, payload)

        with pytest.raises(AssertionError):
            runner.probe.await_event(TEXT_IN, timeout=2.0)
        assert not asr.calls
