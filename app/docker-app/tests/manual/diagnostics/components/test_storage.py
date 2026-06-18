"""Diagnostic: verify the backend storage API (upload + retrieve)."""
import logging
from pathlib import Path

from cltl.combot.infra.event.kombu import KombuEventBus

from components.base import ComponentTest
from storage_client import StorageClient

logger = logging.getLogger(__name__)

_AUDIO_FILE = Path(__file__).parent.parent / "payloads" / "test_audio.raw"


class StorageTest(ComponentTest):
    name = "storage"

    def run(self, scenario_id: str, bus: KombuEventBus, storage: StorageClient) -> bool:
        frames, audio_id = storage.load_audio_file(str(_AUDIO_FILE))

        logger.info("Uploading %d samples as audio/%s", len(frames), audio_id)
        storage.upload_audio(audio_id, frames)

        downloaded = storage.download_audio(audio_id)
        uploaded = frames.tobytes()

        if downloaded != uploaded:
            logger.error(
                "Storage round-trip mismatch: uploaded %d bytes, got back %d bytes",
                len(uploaded), len(downloaded),
            )
            return False

        logger.info("Storage round-trip OK (%d bytes)", len(uploaded))
        return True
