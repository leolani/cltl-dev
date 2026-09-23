"""cltl-backend's audio storage, as cltl-vad and cltl-asr actually use it.

Neither of those modules ever receives audio on the event bus. They receive an
id and a sample range, and fetch the samples back over HTTP from the storage
service. That makes the storage endpoint a module boundary in its own right —
and a silent one, because a range served slightly wrong still yields a
plausible-looking transcript.

The second class here is a regression test for a defect this harness turned up
rather than a boundary between two modules. It was an `xfail` until the storage
classes were fixed to create their own directory; it is kept as a live test so
that the fix cannot quietly regress behind the runners' own `mkdir`.
"""
import numpy as np
import pytest
from cltl.backend.impl.cached_storage import CachedAudioStorage, CachedImageStorage
from cltl.backend.source.client_source import ClientAudioSource

from cltl_integration.drivers import audio
from cltl_integration.topology import BACKEND

AUDIO_ID = "test-audio-signal"


@pytest.fixture
def storage(inprocess):
    return inprocess(BACKEND)


def _fetch(runner, audio_id: str, offset: int = 0, length: int = -1) -> np.ndarray:
    """Read audio back the way AsrService does, through the real client."""
    storage_url = runner.config_manager.get_config("cltl.backend").get("storage_url")
    url = f"cltl-storage:audio/{audio_id}"

    with ClientAudioSource(url, storage_url, offset, length) as source:
        return np.concatenate(tuple(source.audio)), source.rate


class TestStorageService:
    def test_stored_audio_is_served_back_unchanged(self, storage):
        samples = audio.utterance()
        storage.container.audio_storage.store(AUDIO_ID, audio.frames(samples), audio.RATE)

        served, rate = _fetch(storage, AUDIO_ID)

        assert rate == audio.RATE
        assert np.array_equal(served.ravel(), samples)

    def test_a_range_serves_exactly_that_range(self, storage):
        """The property cltl-asr's transcript quality rests on.

        ``AsrService`` asks for ``[segment.start, segment.stop)`` in samples. If
        the service interpreted those as frames or bytes, or was off by one, the
        pipeline would still produce text — of the wrong audio.
        """
        samples = audio.utterance()
        storage.container.audio_storage.store(AUDIO_ID, audio.frames(samples), audio.RATE)
        start, stop = audio.FRAME_SIZE * 3, audio.FRAME_SIZE * 11

        served, _ = _fetch(storage, AUDIO_ID, offset=start, length=stop - start)

        assert len(served) == stop - start
        assert np.array_equal(served.ravel(), samples[start:stop])

    def test_unknown_audio_is_not_served(self, storage):
        with pytest.raises(ValueError):
            _fetch(storage, "no-such-audio")


class TestFreshStorageRoot:
    """Storing into a directory that does not exist yet.

    Not a two-module boundary, but worth pinning at this level. Until
    ``CachedAudioStorage.__init__`` was fixed it called
    ``os.makedirs(os.path.dirname(self._storage_path))`` — creating the *parent*
    of the directory it writes into rather than the directory itself — so every
    topology with a microphone logged a libsndfile "System error" per recording
    and persisted nothing, while the live pipeline carried on working because
    cltl-vad and cltl-asr read from the in-memory cache rather than from disk.
    That combination, visibly fine and quietly lossy, is what makes it worth a
    test rather than a comment.

    Exercised directly against the storage classes rather than through a
    topology, so that the runners' own `mkdir` cannot mask a regression.
    """

    def test_storage_creates_its_own_directory(self, tmp_path):
        audio_storage = CachedAudioStorage(str(tmp_path / "audio"))

        audio_storage.store(AUDIO_ID, audio.frames(audio.speech()), audio.RATE)

        assert (tmp_path / "audio" / f"{AUDIO_ID}.wav").exists()

    def test_image_storage_creates_its_own_directory(self, tmp_path):
        """``CachedImageStorage`` repeated the same mistake, so pin it too."""
        CachedImageStorage(str(tmp_path / "image"))

        assert (tmp_path / "image").is_dir()
