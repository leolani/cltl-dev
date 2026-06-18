"""Diagnostic: send a text utterance to Eliza and expect a response."""
import json
import logging
import uuid
from pathlib import Path

from cltl.combot.event.emissor import TextSignalEvent, ConversationalAgent
from cltl.combot.infra.event.api import Event
from cltl.combot.infra.time_util import timestamp_now
from emissor.representation.scenario import TextSignal

from components.base import ComponentTest
from event_bus import collect_events
from storage_client import StorageClient

logger = logging.getLogger(__name__)

_TEXT_IN_TOPIC = "cltl.topic.text_in"
_TEXT_OUT_TOPIC = "cltl.topic.text_out"
_TIMEOUT = 30.0
_PAYLOAD_FILE = Path(__file__).parent.parent / "payloads" / "text_signal.json"


class ElizaTest(ComponentTest):
    name = "eliza"

    def run(self, scenario_id: str, bus, storage: StorageClient) -> bool:
        payload_data = json.loads(_PAYLOAD_FILE.read_text())
        utterance = payload_data["text"]

        signal_id = str(uuid.uuid4())
        now = timestamp_now()
        signal = TextSignal.for_scenario(
            scenario_id, now, now, None, utterance, signal_id=signal_id
        )
        event_payload = TextSignalEvent.for_speaker(signal)
        event = Event.for_scenario_payload(scenario_id, event_payload)

        with collect_events(bus, _TEXT_OUT_TOPIC, timeout=_TIMEOUT) as responses:
            logger.info("Publishing utterance %r to %s", utterance, _TEXT_IN_TOPIC)
            bus.publish(_TEXT_IN_TOPIC, event)

        if not responses:
            logger.error("No response from Eliza within %.1fs", _TIMEOUT)
            return False

        response_text = responses[0].payload.signal.text.strip()
        if not response_text:
            logger.error("Eliza responded but text is empty")
            return False

        annotations = responses[0].payload.signal.mentions
        is_agent = any(
            a.value == ConversationalAgent.LEOLANI.name
            for m in annotations
            for a in m.annotations
            if hasattr(a, "value")
        )
        if not is_agent:
            logger.warning("Response is missing LEOLANI agent annotation")

        logger.info("Eliza OK — response: %r", response_text)
        return True
