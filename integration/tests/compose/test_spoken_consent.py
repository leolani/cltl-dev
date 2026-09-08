"""Tier 2, the full spoken shape: consent is given out loud.

    espeak-ng, or the committed rendering of the same phrase
      -> cltl-backend records it
      -> cltl-vad finds the speech
      -> cltl-asr transcribes it with Whisper
      -> cltl-context's InitService reads the transcript as an acceptance
      -> BDIService hands the conversation to cltl-eliza

Everywhere else in the suite the acceptance is typed into the chat UI, and what
``InitService`` matches is a string the test wrote. Here it matches Whisper's
output, which is a different object entirely: capitalised, punctuated, sometimes
a homophone. The check is ``"yes" in text.lower()``
(``cltl-context/src/cltl_service/intentions/init.py``), and whether a real
recogniser's output satisfies it is not something a hand-built stimulus can say.

That gap is not hypothetical about this platform in particular — it is the whole
reason a robot that works over text stops working when you speak to it. The old
``app/docker-app`` suite covered it with a gTTS-scripted microphone and nothing
replaced it until now.

`spoken_pipeline` is `audio_pipeline` plus cltl-context. It is the slowest thing
in the suite after the audio pipeline itself, and it is the only test where every
module runs at once.
"""
import pytest

from cltl_integration.drivers import audio
from cltl_integration.drivers.audio import StubAudioServer
from cltl_integration.drivers.conversation import (GREETING, TEXT_IN, TEXT_OUT,
                                                   TOPICS, Conversation)
from cltl_integration.fixtures import SPOKEN_CONSENT, SPOKEN_GREETING
from cltl_integration.topology import SPOKEN_PIPELINE

pytestmark = [pytest.mark.compose, pytest.mark.slow]

# Recording is paced at real time and Whisper transcribes two utterances on CPU,
# the first while it is still loading its model. None of that is a hang.
SPOKEN_TIMEOUT = 300.0


@pytest.fixture
def spoken(compose):
    for phrase in (SPOKEN_GREETING, SPOKEN_CONSENT):
        if not audio.can_speak(phrase):
            pytest.skip(f"cannot say {phrase!r}: no espeak-ng and no committed fixture")

    # Two utterances, one per reconnect of the backend's microphone thread: the
    # first is heard while the init dialogue is still asking, the second answers
    # it. `idle_ms` is long because once the script is exhausted every further
    # reconnect stores a recording, and a tight loop of them buries the log this
    # test would be read from.
    server = StubAudioServer(
        [audio.speech_for(SPOKEN_GREETING), audio.speech_for(SPOKEN_CONSENT)],
        idle_ms=5000, host="0.0.0.0").start()
    try:
        runner = compose(
            SPOKEN_PIPELINE,
            environment={"CLTL_AUDIO_URL": f"http://host.docker.internal:{server.port}"})
        runner.probe.subscribe(*TOPICS)
        yield Conversation(runner)
    finally:
        server.stop()


class TestSpokenConsent:
    def test_the_acceptance_is_understood(self, spoken):
        """Whisper's transcript of "yes" is a form InitService accepts."""
        spoken.open()

        spoken.await_handover(timeout=SPOKEN_TIMEOUT)

        transcripts = [event.payload.signal.text
                       for event in spoken.runner.probe.events(TEXT_IN)]
        assert any("yes" in text.lower() for text in transcripts), (
            f"the handover happened without an affirmative transcript: {transcripts}")

    def test_the_agent_takes_over_after_the_greeting(self, spoken):
        """Eliza is gated until the spoken handshake completes, then speaks.

        The negative half matters as much as the positive one: an ungated Eliza
        would answer the microphone over the top of the init dialogue.
        """
        spoken.open()
        spoken.runner.probe.clear()

        spoken.await_handover(timeout=SPOKEN_TIMEOUT)
        opening = spoken.runner.probe.await_event(
            TEXT_OUT, lambda event: GREETING not in event.payload.signal.text,
            timeout=SPOKEN_TIMEOUT)

        assert opening.payload.signal.text

    def test_the_spoken_conversation_reaches_the_chat_ui(self, spoken):
        """What was said out loud, and what was answered, are both on the page.

        The chat UI is where a person would look, and it is fed by a different
        path from the bus this test has been watching.
        """
        spoken.open()
        spoken.await_handover(timeout=SPOKEN_TIMEOUT)

        history = spoken.client.fetch_all(spoken.chat_id)
        texts = [utterance["text"] for utterance in history]

        assert any(GREETING in text for text in texts), (
            f"the init greeting never reached the chat: {texts}")
        assert any("yes" in text.lower() for text in texts), (
            f"the transcribed acceptance never reached the chat: {texts}")
