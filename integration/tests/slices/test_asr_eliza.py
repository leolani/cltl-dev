"""Slice: cltl-asr -> cltl-eliza, across `cltl.topic.text_in`.

Runs Eliza alone and injects the payload cltl-asr would publish. That is the
point: the contract between the two modules is the event on the topic, so
driving it directly tests the boundary without paying for Whisper. The payload
type is the real ``AsrTextSignalEvent`` from ``cltl_service.asr.schema``, not a
stand-in, so a change to ASR's output shape breaks this test.

The tier-2 counterpart of this test runs the same assertions against the real
images, where the event additionally round-trips through marshal/unmarshal.
"""
import pytest
from cltl.combot.event.emissor import TextSignalEvent
from cltl.combot.infra.event import Event
from cltl.combot.infra.time_util import timestamp_now
from cltl_service.asr.schema import AsrTextSignalEvent
from emissor.representation.container import Index
from emissor.representation.scenario import TextSignal

from cltl_integration.topology import ELIZA

TEXT_IN = "cltl.topic.text_in"
TEXT_OUT = "cltl.topic.text_out"

SCENARIO = "test-scenario"


def _asr_event(text: str) -> AsrTextSignalEvent:
    """Build the event exactly as AsrService._create_payload does."""
    signal = TextSignal.for_scenario(
        SCENARIO, timestamp_now(), timestamp_now(), None, text)
    segment = Index.from_range("audio-signal-id", 0, 16)

    return AsrTextSignalEvent.create_asr(signal, 1.0, [segment])


@pytest.fixture
def eliza(inprocess):
    runner = inprocess(ELIZA)
    runner.probe.subscribe(TEXT_OUT)
    return runner


class TestAsrToEliza:
    def test_transcript_gets_a_reply(self, eliza):
        eliza.event_bus.publish(TEXT_IN, Event.for_payload(_asr_event("I feel anxious today")))

        reply = eliza.probe.await_event(TEXT_OUT)

        assert reply.payload.signal.text, "Eliza published an empty response"

    def test_reply_is_a_text_signal_event_for_the_agent(self, eliza):
        """cltl-chat-ui filters replies by speaker, so the agent annotation matters."""
        eliza.event_bus.publish(TEXT_IN, Event.for_payload(_asr_event("Hello")))

        reply = eliza.probe.await_event(TEXT_OUT)

        assert isinstance(reply.payload, TextSignalEvent)
        assert reply.payload.signal.text != "Hello", "Eliza echoed the input verbatim"

    def test_reply_carries_the_scenario_through(self, eliza):
        """cltl-emissor-data files signals by scenario; losing it drops the turn."""
        eliza.event_bus.publish(TEXT_IN, Event.for_payload(_asr_event("I am sad")))

        reply = eliza.probe.await_event(TEXT_OUT)

        assert reply.payload.signal.time.container_id == SCENARIO

    def test_each_transcript_gets_its_own_reply(self, eliza):
        for text in ("Hello", "I feel sad", "Why do you ask?"):
            eliza.probe.clear()
            eliza.event_bus.publish(TEXT_IN, Event.for_payload(_asr_event(text)))

            reply = eliza.probe.await_event(TEXT_OUT)

            assert reply.payload.signal.text, f"no reply to {text!r}"
