"""Diagnostic: publish an AudioSignal and expect a VadMentionEvent back."""
import logging
import time
import uuid
from pathlib import Path

from cltl.combot.event.emissor import AudioSignalStarted, AudioSignalStopped
from cltl.combot.infra.event.api import Event
from cltl.combot.infra.time_util import timestamp_now
from emissor.representation.scenario import AudioSignal

from components.base import ComponentTest
from event_bus import collect_events
from storage_client import StorageClient, SAMPLING_RATE, CHANNELS

logger = logging.getLogger(__name__)

_MIC_TOPIC = "cltl.topic.microphone"
_VAD_TOPIC = "cltl.topic.vad"
_AUDIO_FILE = Path(__file__).parent.parent / "payloads" / "test_audio.raw"
_TIMEOUT = 15.0


class VadTest(ComponentTest):
    name = "vad"

    def run(self, scenario_id: str, bus, storage: StorageClient) -> bool:
        frames, audio_id = storage.load_audio_file(str(_AUDIO_FILE))
        storage.upload_audio(audio_id, frames)

        start = timestamp_now()
        signal = AudioSignal.for_scenario(
            scenario_id, start, start,
            f"cltl-storage:audio/{audio_id}",
            len(frames), CHANNELS,
            signal_id=audio_id,
        )

        started_event = Event.for_scenario_payload(
            scenario_id, AudioSignalStarted.create(signal)
        )
        stopped_event = Event.for_scenario_payload(
            scenario_id, AudioSignalStopped.create(signal)
        )

        with collect_events(bus, _VAD_TOPIC, timeout=_TIMEOUT) as vad_events:
            logger.info("Publishing AudioSignalStarted for audio/%s", audio_id)
            bus.publish(_MIC_TOPIC, started_event)
            time.sleep(0.1)
            bus.publish(_MIC_TOPIC, stopped_event)

        if not vad_events:
            logger.error("No VadMentionEvent received within %.1fs", _TIMEOUT)
            return False

        event = vad_events[0]
        mentions = event.payload.mentions
        if not mentions:
            logger.error("VadMentionEvent arrived but contained no mentions")
            return False

        segment = mentions[0].segment[0]
        if segment.container_id != audio_id:
            logger.error(
                "VadMentionEvent segment container_id mismatch: expected %s, got %s",
                audio_id, segment.container_id,
            )
            return False

        logger.info("VAD OK — received mention for audio/%s", audio_id)
        return True
