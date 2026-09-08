"""The phrases the suite speaks aloud, and the committed rendering of each.

Tier 2's audio tests need real speech — something Whisper can transcribe — and
``espeak-ng`` renders it offline and identically every run. But it arrives from
apt, not from ``cltl-requirements/mirror``, and a component that installs from an
offline registry so that a build needs no network should not gate its slowest and
most valuable test on a package that may not be there.

So every phrase lives here once, and ``make speech-fixtures`` commits espeak-ng's
rendering of it under ``fixtures/speech/``.
:func:`cltl_integration.drivers.audio.speech_for` prefers the binary and falls
back to the file, so the tests run either way and say the same words.

One list, so that a phrase cannot drift out of fixture coverage: adding one here
and running the target is the whole procedure, and a phrase used by a test but
missing from this list shows up as a failing render rather than a silent skip.
"""
import sys

from cltl_integration.drivers.audio import fixture_path, speech_available, write_fixture

#: Opens the conversation. Not a greeting the agent reacts to — it is simply the
#: first thing the microphone hears, before consent is asked.
SPOKEN_GREETING = "Hello"

#: Accepts the init dialogue's offer. ``InitService`` matches this with
#: ``"yes" in text.lower()``, so it has to survive transcription intact.
SPOKEN_CONSENT = "yes"

#: The utterance the agent answers. Chosen so an assertion can look for one
#: distinctive word ("anxious") rather than the whole transcript.
SPOKEN_COMPLAINT = "Hello, I feel very anxious today"

PHRASES = (SPOKEN_GREETING, SPOKEN_CONSENT, SPOKEN_COMPLAINT)


def main() -> int:
    if not speech_available():
        print("espeak-ng is not installed; nothing to render.\n"
              "  sudo apt-get install -y espeak-ng", file=sys.stderr)

        return 1

    for phrase in PHRASES:
        path = write_fixture(phrase)
        print(f"  {path.stat().st_size / 1024:6.1f} KiB  {path.name}  {phrase!r}")

    print(f"\n{len(PHRASES)} phrase(s) written to {fixture_path(PHRASES[0]).parent}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
