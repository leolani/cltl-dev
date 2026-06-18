"""HTTP client for the backend storage API."""
import logging
from typing import Tuple

import numpy as np
import requests

logger = logging.getLogger(__name__)

_AUDIO_MIME = "audio/L16"
_CONTENT_TYPE_SEP = ";"

SAMPLING_RATE = 16000
CHANNELS = 1
FRAME_SIZE = 480
SAMPLE_WIDTH = 2  # bytes (int16)


class StorageClient:
    """Wraps PUT/GET /storage/audio/<id> endpoints of the backend storage service."""

    def __init__(self, base_url: str, timeout: float = 30.0):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    @classmethod
    def from_config(cls, config_manager) -> "StorageClient":
        config = config_manager.get_config("cltl.backend")
        url = config.get("storage_url").rstrip("/")
        return cls(url)

    def upload_audio(self, audio_id: str, frames: np.ndarray) -> None:
        """Upload *frames* (int16 numpy array) under *audio_id*."""
        url = f"{self._base_url}/audio/{audio_id}"
        content_type = (
            f"{_AUDIO_MIME}"
            f"{_CONTENT_TYPE_SEP} rate={SAMPLING_RATE}"
            f"{_CONTENT_TYPE_SEP} channels={CHANNELS}"
            f"{_CONTENT_TYPE_SEP} frame_size={FRAME_SIZE}"
        )
        raw = frames.astype(np.int16).tobytes()
        response = requests.put(
            url,
            data=raw,
            headers={"Content-Type": content_type},
            timeout=(self._timeout, None),
        )
        response.raise_for_status()
        logger.debug("Uploaded %d bytes of audio as %s", len(raw), audio_id)

    def download_audio(self, audio_id: str) -> bytes:
        """Return the raw PCM bytes stored under *audio_id*."""
        url = f"{self._base_url}/audio/{audio_id}"
        response = requests.get(url, timeout=(self._timeout, None), stream=True)
        response.raise_for_status()
        data = b"".join(response.iter_content(chunk_size=None))
        logger.debug("Downloaded %d bytes of audio for %s", len(data), audio_id)
        return data

    def load_audio_file(self, path: str) -> Tuple[np.ndarray, str]:
        """Load a WAV or raw PCM file and return (frames, audio_id).

        Accepts 16-bit mono WAV files or raw int16 PCM files (.raw / .pcm).
        Returns a contiguous int16 array shaped (n_samples,).
        """
        import uuid
        import os

        audio_id = str(uuid.uuid4())
        ext = os.path.splitext(path)[1].lower()

        if ext == ".wav":
            import wave
            with wave.open(path) as wf:
                raw = wf.readframes(wf.getnframes())
        else:
            with open(path, "rb") as f:
                raw = f.read()

        frames = np.frombuffer(raw, dtype=np.int16)
        return frames, audio_id
