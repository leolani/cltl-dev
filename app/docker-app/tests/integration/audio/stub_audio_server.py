"""
Stub audio server that streams gTTS speech as raw 16-bit PCM over HTTP.

Implements the same streaming protocol as cltl-backend's BackendServer:
  GET /audio
  Content-Type: audio/L16; rate=16000; channels=1; frame_size=480
  Body: continuous stream of 960-byte raw PCM frames until EOF

The stream is structured as:
  [600 ms silence] [gTTS speech resampled to 16 kHz mono int16] [600 ms silence]

Silence padding is required by VAD:
  - Pre-speech:  ≥ 600 ms so the sliding window initialises as non-speech
  - Post-speech: ≥ 300 ms so the allow_gap threshold fires before stream EOF

Usage:
  python stub_audio_server.py "Hello, how are you?"  # serves on port 9000
  python stub_audio_server.py --port 9001 "Text here"
"""
import io
import logging
import struct
import sys

from flask import Flask, Response
from gtts import gTTS
from pydub import AudioSegment

logger = logging.getLogger(__name__)

RATE = 16000
CHANNELS = 1
FRAME_SIZE = 480            # samples per frame
DEPTH = 2                   # bytes per sample (int16)
BYTES_PER_FRAME = FRAME_SIZE * CHANNELS * DEPTH
SILENCE_PRE_MS = 600
SILENCE_POST_MS = 600

app = Flask(__name__)


def _text_to_pcm(text: str) -> bytes:
    """Convert text to 16 kHz mono 16-bit PCM bytes via gTTS + pydub."""
    mp3_buffer = io.BytesIO()
    gTTS(text=text, lang="en").write_to_fp(mp3_buffer)
    mp3_buffer.seek(0)
    segment = AudioSegment.from_file(mp3_buffer, format="mp3")
    segment = (segment
               .set_frame_rate(RATE)
               .set_channels(CHANNELS)
               .set_sample_width(DEPTH))
    return segment.raw_data


def _silence_pcm(duration_ms: int) -> bytes:
    n_samples = int(RATE * duration_ms / 1000) * CHANNELS
    return struct.pack(f"<{n_samples}h", *([0] * n_samples))


def _pad_to_frame_boundary(pcm: bytes) -> bytes:
    remainder = len(pcm) % BYTES_PER_FRAME
    if remainder:
        pcm += b"\x00" * (BYTES_PER_FRAME - remainder)
    return pcm


def _generate_frames(text: str):
    logger.info("Generating audio for: %r", text)
    payload = (
        _silence_pcm(SILENCE_PRE_MS)
        + _text_to_pcm(text)
        + _silence_pcm(SILENCE_POST_MS)
    )
    payload = _pad_to_frame_boundary(payload)
    logger.info("Streaming %d frames (%.1f s)", len(payload) // BYTES_PER_FRAME,
                len(payload) / (RATE * CHANNELS * DEPTH))
    for i in range(0, len(payload), BYTES_PER_FRAME):
        yield payload[i:i + BYTES_PER_FRAME]


@app.route("/health")
def health():
    return "OK", 200


@app.route("/audio")
def audio():
    text = app.config["TEXT"]
    mime = f"audio/L16; rate={RATE}; channels={CHANNELS}; frame_size={FRAME_SIZE}"
    return Response(_generate_frames(text), mimetype=mime)


def main():
    import argparse
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-8s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description="Stub audio server for integration tests")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("text", nargs="*", default=["Hello"])
    args = parser.parse_args()
    app.config["TEXT"] = " ".join(args.text)
    app.run(host="0.0.0.0", port=args.port, threaded=True)


if __name__ == "__main__":
    main()
