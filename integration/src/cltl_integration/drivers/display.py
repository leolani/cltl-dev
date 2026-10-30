"""Publish display events without running a module that asks for a page.

No module in the platform asks for a page to be shown yet, so the Display tab's
publisher is this: the test, or the demo launcher.
"""
import time

from cltl.combot.event.display import DisplayEvent
from cltl.combot.infra.event import Event, EventBus

DISPLAY_TOPIC = "cltl.topic.display"


def publish_display(event_bus: EventBus, topic: str, scenario_id: str, url: str, name: str) -> None:
    """Publish one page. Scoped to the scenario, as the chat UI requires."""
    event_bus.publish(topic, Event.for_scenario_payload(scenario_id, DisplayEvent(url, name)))


def demo(event_bus: EventBus, topic: str, scenario_id: str, base_url: str, pause: float = 4.0) -> None:
    """Loop through pages served by the chat UI itself, which are reachable by any browser
    that reached the chat UI, and one that is not allowed and so never appears."""
    pages = (("Routes", base_url + "/urlmap"),
             ("Scenario", base_url + "/chat/scenario"),
             ("Config", base_url + "/config"))

    while True:
        for name, url in pages:
            publish_display(event_bus, topic, scenario_id, url, name)
            time.sleep(pause)
        publish_display(event_bus, topic, scenario_id, "https://not-allowed.invalid/", "Refused")
        time.sleep(pause)
