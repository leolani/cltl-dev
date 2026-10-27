"""Publish status events without running a module that emits them.

No module in the platform reports status yet, so the status bar's emitter is
this: the test, or the demo launcher.
"""
import time

from cltl.combot.event.status import StatusEvent
from cltl.combot.infra.event import Event, EventBus

STATUS_TOPIC = "cltl.topic.status"


def publish_status(event_bus: EventBus, topic: str, scenario_id: str, message: str,
                   source: str = "test", **kwargs) -> None:
    """Publish one status. Scoped to the scenario, as the chat UI requires."""
    event_bus.publish(topic, Event.for_scenario_payload(scenario_id, StatusEvent(message, source, **kwargs)))


def demo(event_bus: EventBus, topic: str, scenario_id: str, pause: float = 2.0) -> None:
    """Loop through what the bar can show: progress, ending a status, levels, ttl."""
    def say(message, source, **kwargs):
        publish_status(event_bus, topic, scenario_id, message, source, **kwargs)
        time.sleep(pause)

    while True:
        for percent in (0, 25, 50, 75, 100):
            say(f"Loading the speech model ({percent}%)", "asr", progress=percent / 100,
                key="asr.model")
        say("Speech model loaded", "asr", key="asr.model", resolved=True)
        say("Listening", "vad", ttl=pause * 1.5)
        say("Camera is slow", "backend", level="warning", key="backend.camera")
        say("Camera lost", "backend", level="error", key="backend.camera")
        say("Camera is back", "backend", key="backend.camera", resolved=True)
