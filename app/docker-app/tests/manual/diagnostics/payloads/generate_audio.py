"""Generate test audio payloads using gTTS.

Run this script once (or whenever you want to refresh the payloads):

    pip install gTTS pydub
    python payloads/generate_audio.py

Produces:
  test_audio.raw          — short utterance for VAD detection test
  test_audio_speech.raw   — same utterance for ASR transcription test

Both files are raw int16 mono PCM at 16 kHz, compatible with the storage API
(audio/L16; rate=16000; channels=1; frame_size=480).
"""
import io
import os
import struct
from pathlib import Path

RATE = 16000
SILENCE_MS = 300  # padding before and after speech


def _gtts_to_pcm(text: str) -> bytes:
    """Synthesise *text* with gTTS and return raw int16 mono PCM at RATE Hz."""
    from gtts import gTTS
    from pydub import AudioSegment

    mp3_buf = io.BytesIO()
    gTTS(text=text, lang="en").write_to_fp(mp3_buf)
    mp3_buf.seek(0)

    segment = AudioSegment.from_file(mp3_buf, format="mp3")
    segment = segment.set_frame_rate(RATE).set_channels(1).set_sample_width(2)
    return segment.raw_data


def _silence_pcm(ms: int) -> bytes:
    n_samples = int(RATE * ms / 1000)
    return b"\x00\x00" * n_samples


def _write_raw(path: Path, pcm: bytes) -> None:
    path.write_bytes(pcm)
    duration = len(pcm) / 2 / RATE
    print(f"Wrote {path.name}: {len(pcm):,} bytes ({duration:.2f}s)")


def main() -> None:
    here = Path(__file__).parent

    utterance = "Hello, this is a diagnostic test."
    speech_pcm = _gtts_to_pcm(utterance)
    padding = _silence_pcm(SILENCE_MS)
    full_pcm = padding + speech_pcm + padding

    _write_raw(here / "test_audio.raw", full_pcm)
    _write_raw(here / "test_audio_speech.raw", full_pcm)

    print(f"\nUtterance: {utterance!r}")
    print("Done. Both payloads use identical audio — replace test_audio_speech.raw")
    print("with a different utterance if you want ASR to transcribe something specific.")


if __name__ == "__main__":
    main()
