"""Slice: cltl-eliza -> cltl-backend, out through the loudspeaker.

The last leg of the platform, and until now the only one with no coverage at all:
what the agent says has to leave the process. ``BackendService`` subscribes a
worker to ``[cltl.backend.tts] topic`` and hands each payload to
``SynchronizedTextToSpeech.say``, which takes a write lock on the shared audio
resource and then calls the configured ``TextOutput``.

Two reasons it was worth building rather than leaving to a person with speakers:

* ``SynchronizedTextToSpeech.say`` catches everything (``sync_tts.py``, bare
  ``except:`` around the whole body) and only logs. A backend that cannot speak
  keeps publishing, keeps recording and keeps answering in the chat UI. There is
  no failure to see anywhere except in the log nobody is reading.
* It needs no sound device. With ``remote_type`` neither ``console`` nor
  ``sound``, ``BackendContainer.text_output`` builds an
  ``AnimatedRemoteTextOutput`` that just POSTs to ``$CLTL_TTS_URL/text``.

Tier 1 only, deliberately. This is where ``ThreadedResourceManager`` is
process-wide and the microphone and the speaker genuinely contend for one
resource; in tier 2 each container has its own. Container-to-host reachability is
already covered by the stub microphone, which dials the same way.
"""
import pytest
from cltl.combot.event.emissor import TextSignalEvent
from cltl.combot.infra.event import Event
from cltl.combot.infra.time_util import timestamp_now
from emissor.representation.scenario import TextSignal

from cltl_integration.drivers import audio
from cltl_integration.drivers.audio import StubAudioServer
from cltl_integration.drivers.scenario import start_scenario
from cltl_integration.drivers.tts import StubTextOutput, gesture_of
from cltl_integration.topology import BACKEND_TTS, BACKEND_TTS_MIC

TEXT_IN = "cltl.topic.text_in"
TEXT_OUT = "cltl.topic.text_out"
SCENARIO_TOPIC = "cltl.topic.scenario"

REPLY = "I am a reply that should be spoken"


def _signal(text: str) -> TextSignal:
    return TextSignal.for_scenario(None, timestamp_now(), timestamp_now(), None, text)


def _agent_says(text: str) -> TextSignalEvent:
    """The payload ElizaService publishes on text_out."""
    return TextSignalEvent.for_agent(_signal(text))


def _person_says(text: str) -> TextSignalEvent:
    """The payload a chat UI or a recogniser publishes on text_in."""
    return TextSignalEvent.for_speaker(_signal(text))


@pytest.fixture
def speaker(inprocess):
    """A backend wired to a stub loudspeaker, with the microphone off."""
    sink = StubTextOutput().start()
    try:
        runner = inprocess(BACKEND_TTS, environment={"CLTL_TTS_URL": sink.url})
        runner.probe.subscribe(TEXT_OUT)
        yield runner, sink
    finally:
        sink.stop()


class TestSpeaking:
    def test_a_reply_reaches_the_loudspeaker(self, speaker):
        runner, sink = speaker

        runner.event_bus.publish(TEXT_OUT, Event.for_payload(_agent_says(REPLY)))

        assert sink.await_text() == REPLY

    def test_the_utterance_is_animated(self, speaker):
        """`gestures` is unset, so AnimatedRemoteTextOutput picks one per utterance.

        Asserted rather than stripped and ignored: a remote speaker is handed
        ``^startTag(nod) ... ^stopTag(nod)``, not the bare sentence, and a
        receiver written against the wrong shape says nothing at all.
        """
        runner, sink = speaker

        runner.event_bus.publish(TEXT_OUT, Event.for_payload(_agent_says(REPLY)))
        sink.await_text()

        assert gesture_of(sink.texts[0]), (
            f"expected the reply to be wrapped in gesture tags: {sink.texts[0]!r}")

    def test_the_agent_keeps_speaking(self, speaker):
        """A second utterance, because the first only proves the lock was taken.

        ``say`` holds a write lock on the audio resource for the duration of the
        call. If it is not released — an early return, a swallowed exception on
        the way out — the backend speaks once and is mute from then on, with the
        worker blocked and nothing logged above DEBUG.
        """
        runner, sink = speaker

        runner.event_bus.publish(TEXT_OUT, Event.for_payload(_agent_says("first")))
        sink.await_text(lambda text: text == "first")
        runner.event_bus.publish(TEXT_OUT, Event.for_payload(_agent_says("second")))

        assert sink.await_text(lambda text: text == "second")

    def test_eliza_is_heard(self, speaker):
        """End of the slice: what the agent decided to say is what comes out.

        Everything above publishes on text_out directly. This drives cltl-eliza,
        so the text on the wire is one the agent composed rather than one the
        test wrote, and the two have to match exactly.
        """
        runner, sink = speaker

        runner.event_bus.publish(
            TEXT_IN, Event.for_payload(_person_says("I feel very anxious today")))
        reply = runner.probe.await_event(TEXT_OUT)

        assert sink.await_text() == reply.payload.signal.text


class TestSpeakingWhileListening:
    """The case the two Synchronized* classes exist for.

    ``SynchronizedMicrophone.listen`` holds a *read* lock on the audio resource
    for as long as it is recording; ``say`` wants the *write* lock. So the
    microphone has to notice a speaker waiting, mute itself, release, and take
    the lock back afterwards.

    This is the configuration the harness used to refuse to start. A guard in
    ``topology.py`` rejected TTS whenever the microphone was off, on the grounds
    that ``SynchronizedMicrophone.start`` is the only thing that ever provides
    the audio resource. That is true, and it is not the whole story:
    ``BackendService.start`` calls ``Backend.start`` unconditionally, which
    starts the microphone *object* whether or not ``[cltl.backend.mic] topic``
    is set. The topic gates the recording thread, not the resource. Both
    configurations work; the guard, its two validators and their tests are gone,
    and this class is what stops that finding from being lost again.
    """

    @pytest.fixture
    def listening_speaker(self, inprocess):
        mic = StubAudioServer([audio.utterance()] * 3, idle_ms=2000).start()
        sink = StubTextOutput().start()
        try:
            runner = inprocess(BACKEND_TTS_MIC,
                               environment={"CLTL_TTS_URL": sink.url,
                                            "CLTL_AUDIO_URL": mic.url})
            yield runner, sink, mic
        finally:
            sink.stop()
            mic.stop()

    def test_the_agent_speaks_over_an_open_microphone(self, listening_speaker):
        runner, sink, mic = listening_speaker
        start_scenario(runner.event_bus, SCENARIO_TOPIC)
        # Let the recording thread reach listen() and take the read lock, or the
        # test passes for the uninteresting reason that there was no contention.
        mic.await_connection()

        runner.event_bus.publish(TEXT_OUT, Event.for_payload(_agent_says(REPLY)))

        assert sink.await_text(timeout=30.0) == REPLY
