"""A stand-in microphone: synthetic PCM served over cltl-backend's audio protocol.

``BackendService`` does not read a file or take an injected buffer. Its
microphone is a ``ClientAudioSource`` that opens a streaming HTTP GET against
``[cltl.backend] server_audio_url`` and expects::

    Content-Type: audio/L16; rate=16000; channels=1; frame_size=480
    Body:         raw little-endian int16 frames, 960 bytes each, until EOF

So a test that wants the backend to hear something has to speak that protocol.
This module does, in both tiers — the only difference is which host the
containers resolve it on.

Two deliberate differences from ``app/docker-app``'s stub:

* **No gTTS.** The prior art synthesises real speech, which needs the network,
  takes seconds per utterance and returns something slightly different every
  time. Nothing downstream of VAD cares whether the audio is intelligible, so
  this generates a voice-shaped harmonic stack instead: deterministic, offline,
  and classified as speech by ``webrtcvad`` at its most aggressive mode (mode 3,
  which is what ``WebRtcVAD`` defaults to). Only a test that runs a real ASR
  needs real speech, and that is a tier-2, ``slow``-marked concern.
* **Paced at real time.** The prior art streams as fast as the socket allows and
  then repeats its last utterance forever. Because ``BackendService``'s mic
  thread immediately reconnects when a stream ends, unpaced silence turns into a
  tight loop that stores an audio file and publishes a signal-started/stopped
  pair per iteration. Pacing at the frame rate keeps a stopped conversation
  idling the way a real microphone does.
"""
import logging
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Iterator, List, Optional, Sequence

import numpy as np
from flask import Flask, Response
from werkzeug.serving import make_server

logger = logging.getLogger(__name__)

RATE = 16000
CHANNELS = 1
DEPTH = 2
FRAME_SIZE = 480                                # samples; 30 ms at 16 kHz
BYTES_PER_FRAME = FRAME_SIZE * CHANNELS * DEPTH
FRAME_DURATION = FRAME_SIZE / RATE              # seconds

# Enough leading and trailing silence for WebRtcVAD to bracket the speech.
# With the harness defaults ([cltl.vad.webrtc] padding 600 ms, activity_window
# 300 ms, allow_gap 300 ms) FrameWiseVAD needs ~600 ms of lead-in to fill its
# padding buffer, and >300 ms of trailing silence before it declares the gap
# closed plus another ~300 ms it reads past the end. Cutting these too fine
# produces a test that passes on one machine and hangs on another.
LEAD_SILENCE_MS = 700
TRAIL_SILENCE_MS = 1000
SPEECH_MS = 600

# Harmonics of a 120 Hz fundamental, amplitude-modulated at a syllable-like
# 4 Hz. Chosen empirically: webrtcvad rejects a pure tone at mode 3 and accepts
# this in every frame.
_FUNDAMENTAL = 120.0
_HARMONICS = (1.0, 0.7, 0.5, 0.4, 0.3)
_MODULATION = 4.0
_AMPLITUDE = 6000.0


def silence(duration_ms: int) -> np.ndarray:
    return np.zeros(_samples(duration_ms), dtype=np.int16)


def speech(duration_ms: int = SPEECH_MS) -> np.ndarray:
    """Voice-shaped audio that WebRtcVAD classifies as speech."""
    t = np.arange(_samples(duration_ms)) / RATE
    wave = sum(gain * np.sin(2 * np.pi * _FUNDAMENTAL * (index + 1) * t)
               for index, gain in enumerate(_HARMONICS))
    envelope = 0.6 + 0.4 * np.sin(2 * np.pi * _MODULATION * t)

    return np.clip(_AMPLITUDE * wave * envelope, -32000, 32000).astype(np.int16)


# Offline text-to-speech for the one thing synthetic tones cannot do: be
# transcribed. Whisper renders espeak-ng's output accurately, and unlike the
# gTTS the prior art uses it needs no network, no mp3 decoder and returns the
# same samples every run.
ESPEAK = "espeak-ng"
ESPEAK_RATE = 22050
ESPEAK_WPM = 130


def speech_available() -> bool:
    return shutil.which(ESPEAK) is not None


def spoken(text: str, words_per_minute: int = ESPEAK_WPM,
           lead_ms: int = LEAD_SILENCE_MS,
           trail_ms: int = TRAIL_SILENCE_MS) -> np.ndarray:
    """Real speech saying *text*, padded for VAD, at the platform's rate.

    Only needed where something downstream has to *understand* the audio — a
    tier-2 pipeline running Whisper. Everything up to and including cltl-vad is
    happy with :func:`utterance`, which is faster and needs nothing installed.
    """
    if not speech_available():
        raise RuntimeError(
            f"{ESPEAK} is not installed; install it with `sudo apt-get install "
            f"-y espeak-ng` (see integration/README.md)")

    with tempfile.TemporaryDirectory() as work:
        wav = Path(work) / "speech.wav"
        subprocess.run([ESPEAK, "-w", str(wav), "-s", str(words_per_minute), text],
                       check=True, capture_output=True)
        samples = _read_mono_16k(wav)

    return np.concatenate((silence(lead_ms), samples, silence(trail_ms)))


def _read_mono_16k(path: Path) -> np.ndarray:
    """Read a WAV as mono int16 at RATE.

    espeak-ng writes 22.05 kHz and offers no way to change it, so this
    resamples. Linear interpolation with no anti-aliasing filter, which is
    crude — and entirely adequate: the point is to hand Whisper something
    intelligible, and it transcribes the result verbatim.
    """
    import soundfile

    data, rate = soundfile.read(str(path), dtype="float32")
    if data.ndim > 1:
        data = data.mean(axis=1)
    if rate != RATE:
        length = int(round(len(data) * RATE / rate))
        data = np.interp(np.linspace(0, len(data) - 1, length),
                         np.arange(len(data)), data)

    padding = -len(data) % FRAME_SIZE

    return np.concatenate((np.clip(data * 32767, -32768, 32767).astype(np.int16),
                           np.zeros(padding, dtype=np.int16)))


def utterance(speech_ms: int = SPEECH_MS,
              lead_ms: int = LEAD_SILENCE_MS,
              trail_ms: int = TRAIL_SILENCE_MS) -> np.ndarray:
    """One speech burst, padded either side so VAD can find its boundaries."""
    return np.concatenate((silence(lead_ms), speech(speech_ms), silence(trail_ms)))


def frames(samples: np.ndarray) -> List[np.ndarray]:
    """Split into the frame size the backend's microphone produces.

    Audio storage records the frame size of the first frame it is handed and
    reports it in the content type, so handing it one big array makes the
    storage service describe a frame the size of the whole recording.
    """
    return [samples[start:start + FRAME_SIZE]
            for start in range(0, len(samples), FRAME_SIZE)]


def _samples(duration_ms: int) -> int:
    """Sample count rounded up to a whole number of frames."""
    samples = int(RATE * duration_ms / 1000)
    remainder = samples % FRAME_SIZE

    return samples + (FRAME_SIZE - remainder if remainder else 0)


class StubAudioServer:
    """Serves a scripted sequence of utterances on ``GET /audio``.

    Each connection serves the next entry in *utterances*; once they are
    exhausted every further connection serves ``idle_ms`` of silence, so the
    backend's reconnect loop keeps turning without inventing more speech.

    Binds port 0 and reports the assigned port, because the tier-2 stub runs on
    the host alongside whatever else is using the machine. Use as a context
    manager.
    """

    def __init__(self, utterances: Sequence[np.ndarray] = (),
                 idle_ms: int = 500, host: str = "127.0.0.1", port: int = 0,
                 paced: bool = True):
        self._utterances = list(utterances)
        self._idle = silence(idle_ms)
        self._paced = paced
        self._host = host
        self._port = port

        self._lock = threading.Lock()
        self._served = 0
        self._server = None
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "StubAudioServer":
        return self.start()

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()

    def start(self) -> "StubAudioServer":
        self._server = make_server(self._host, self._port, self._app(), threaded=True)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="stub-audio-server", daemon=True)
        self._thread.start()
        logger.info("Stub audio server listening on %s with %s utterance(s)",
                    self.url, len(self._utterances))

        return self

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None

    # -- introspection -----------------------------------------------------

    @property
    def url(self) -> str:
        """Base URL, without the ``/audio`` path ClientAudioSource appends."""
        if self._server is None:
            raise RuntimeError("stub audio server is not started")
        host, port = self._server.server_address[:2]

        return f"http://{host}:{port}"

    @property
    def port(self) -> int:
        """The assigned port. Bound to 0.0.0.0 in tier 2, where the containers
        reach it as host.docker.internal, so the caller builds its own URL."""
        if self._server is None:
            raise RuntimeError("stub audio server is not started")

        return self._server.server_address[1]

    @property
    def served(self) -> int:
        """How many connections have been answered so far."""
        with self._lock:
            return self._served

    @property
    def utterances_exhausted(self) -> bool:
        with self._lock:
            return self._served >= len(self._utterances)

    # -- server ------------------------------------------------------------

    def _app(self) -> Flask:
        app = Flask("stub-audio")

        @app.route("/audio")
        def audio():
            samples = self._next_samples()
            mime = (f"audio/L16; rate={RATE}; channels={CHANNELS}; "
                    f"frame_size={FRAME_SIZE}")

            return Response(self._frames(samples), mimetype=mime)

        return app

    def _next_samples(self) -> np.ndarray:
        with self._lock:
            index = self._served
            self._served += 1

        if index < len(self._utterances):
            logger.info("Serving utterance %s (%.1fs)",
                        index, len(self._utterances[index]) / RATE)
            return self._utterances[index]

        return self._idle

    def _frames(self, samples: np.ndarray) -> Iterator[bytes]:
        raw = samples.astype(np.int16).tobytes()
        for start in range(0, len(raw), BYTES_PER_FRAME):
            if self._paced:
                time.sleep(FRAME_DURATION)
            yield raw[start:start + BYTES_PER_FRAME]
