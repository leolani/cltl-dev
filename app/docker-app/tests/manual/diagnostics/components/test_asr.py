"""Diagnostic: trigger the full VAD→ASR pipeline and expect a transcription."""
import logging
import time
from pathlib import Path

from cltl.combot.event.emissor import AudioSignalStarted, AudioSignalStopped
from cltl.combot.infra.event.api import Event
from cltl.combot.infra.time_util import timestamp_now
from emissor.representation.scenario import AudioSignal

from components.base import ComponentTest
from event_bus import collect_events
from storage_client import StorageClient, CHANNELS

logger = logging.getLogger(__name__)

_MIC_TOPIC = "cltl.topic.microphone"
_ASR_TOPIC = "cltl.topic.text_in"
# VAD+Whisper can be slow; give it generous time.
_TIMEOUT = 60.0
_AUDIO_FILE = Path(__file__).parent.parent / "payloads" / "test_audio_speech.raw"


class AsrTest(ComponentTest):
    name = "asr"

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

        with collect_events(bus, _ASR_TOPIC, timeout=_TIMEOUT) as asr_events:
            logger.info("Publishing AudioSignalStarted for speech audio/%s", audio_id)
            bus.publish(_MIC_TOPIC, started_event)
            time.sleep(0.1)
            bus.publish(_MIC_TOPIC, stopped_event)

        if not asr_events:
            logger.error("No ASR TextSignalEvent received within %.1fs", _TIMEOUT)
            return False

        text = asr_events[0].payload.signal.text.strip()
        if not text:
            logger.error("ASR event arrived but transcription is empty")
            return False

        logger.info("ASR OK — transcription: %r", text)
        return True
