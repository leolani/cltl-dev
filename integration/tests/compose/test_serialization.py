"""Tier 2's reason for existing: the events have to survive the wire.

In tier 1 a subscriber is handed the very object the publisher created, so a
payload that cannot be serialised passes every in-process test there is. In a
deployment each event is marshalled to JSON by ``cltl_integration.serialization``
(the same functions every module's ``main.py`` installs), routed by RabbitMQ,
and reconstructed on the far side. A field that does not make that trip is a
field the receiving module never sees.

These publish the real payload types the modules exchange and read them back
off the broker, so the assertion is about the round trip itself rather than
about any one module's behaviour.
"""
import pytest
from cltl.combot.event.emissor import ConversationalAgent, TextSignalEvent
from cltl.combot.infra.event import Event
from cltl.combot.infra.time_util import timestamp_now
from cltl_service.asr.schema import AsrTextSignalEvent
from cltl_service.vad.schema import VadAnnotation, VadMentionEvent
from emissor.representation.container import Index
from emissor.representation.scenario import TextSignal

from cltl_integration.topology import ELIZA

pytestmark = pytest.mark.compose

TEXT_IN = "cltl.topic.text_in"
TEXT_OUT = "cltl.topic.text_out"
VAD_TOPIC = "cltl.topic.vad"

SCENARIO = "test-scenario"
AUDIO_ID = "test-audio-signal"


def _asr_event(text: str) -> AsrTextSignalEvent:
    """Built exactly as AsrService._create_payload does."""
    signal = TextSignal.for_scenario(SCENARIO, timestamp_now(), timestamp_now(), None, text)

    return AsrTextSignalEvent.create_asr(signal, 1.0, [Index.from_range(AUDIO_ID, 0, 16)])


def _vad_event() -> VadMentionEvent:
    """Built exactly as VadService._create_payload does."""
    return VadMentionEvent.create(Index.from_range(AUDIO_ID, 480, 9600),
                                  VadAnnotation.for_activation(1.0, "WebRtcVAD"))


@pytest.fixture
def bus(compose):
    runner = compose(ELIZA)
    runner.probe.subscribe(TEXT_IN, TEXT_OUT, VAD_TOPIC)

    return runner


class TestRoundTrip:
    def test_asr_payload_survives_the_broker(self, bus):
        bus.event_bus.publish(TEXT_IN, Event.for_scenario_payload(SCENARIO, _asr_event("hello")))

        received = bus.probe.await_event(
            TEXT_IN, lambda event: isinstance(event.payload, AsrTextSignalEvent), timeout=20)

        assert received.payload.signal.text == "hello"
        assert received.payload.confidence == 1.0
        assert received.metadata.scenario_id == SCENARIO

    def test_asr_audio_segment_survives_the_broker(self, bus):
        """The segment is how cltl-asr's output stays traceable to its audio."""
        bus.event_bus.publish(TEXT_IN, Event.for_scenario_payload(SCENARIO, _asr_event("hello")))

        received = bus.probe.await_event(
            TEXT_IN, lambda event: isinstance(event.payload, AsrTextSignalEvent), timeout=20)

        segments = received.payload.audio_segment
        assert [(segment.container_id, segment.start, segment.stop) for segment in segments] \
               == [(AUDIO_ID, 0, 16)]

    def test_vad_payload_survives_the_broker(self, bus):
        """A mention with an Index inside it — the shape cltl-asr consumes."""
        bus.event_bus.publish(VAD_TOPIC, Event.for_scenario_payload(SCENARIO, _vad_event()))

        received = bus.probe.await_event(
            VAD_TOPIC, lambda event: isinstance(event.payload, VadMentionEvent), timeout=20)

        segment = received.payload.mentions[0].segment[0]
        assert (segment.container_id, segment.start, segment.stop) == (AUDIO_ID, 480, 9600)
        assert received.payload.mentions[0].annotations[0].value == 1.0


class TestModuleReadsWhatWeSend:
    def test_a_transcript_published_here_is_answered_there(self, bus):
        """End of the round trip: another process unmarshals it and acts on it.

        The strongest statement available about serialization — cltl-eliza had
        to reconstruct the payload well enough to read `signal.text` off it.
        """
        bus.event_bus.publish(TEXT_IN, Event.for_scenario_payload(
            SCENARIO, _asr_event("I feel very anxious today")))

        reply = bus.probe.await_event(
            TEXT_OUT, lambda event: bool(event.payload.signal.text), timeout=30)

        assert isinstance(reply.payload, TextSignalEvent)
        assert reply.payload.signal.text != "I feel very anxious today"


class TestAgentAnnotation:
    @pytest.mark.xfail(
        strict=True,
        reason="The deployment half of the defect pinned in tier 1 by "
               "tests/slices/test_vad_asr.py::TestAgentAnnotation. "
               "cltl-asr/src/cltl_service/asr/schema.py:16 annotates the speaker "
               "with the ConversationalAgent.SPEAKER *enum member*, where "
               "TextSignalEvent.for_speaker/for_agent both pass "
               "ConversationalAgent.SPEAKER.name. In process the annotation "
               "value is the enum object; through marshal/unmarshal it becomes "
               "the lowercased 'speaker'; every other producer writes 'SPEAKER'. "
               "Three representations of one fact, and only this tier shows the "
               "third. Fix: append .name, as cltl-combot does.")
    def test_the_speaker_annotation_matches_every_other_producer(self, bus):
        bus.event_bus.publish(TEXT_IN, Event.for_scenario_payload(SCENARIO, _asr_event("hello")))

        received = bus.probe.await_event(
            TEXT_IN, lambda event: isinstance(event.payload, AsrTextSignalEvent), timeout=20)

        values = [annotation.value
                  for mention in received.payload.signal.mentions
                  for annotation in mention.annotations
                  if annotation.type == ConversationalAgent.__name__]
        assert values == [ConversationalAgent.SPEAKER.name]
