"""Publish scenario lifecycle events without running cltl-context.

Several services refuse to do anything until they have seen a ``ScenarioStarted``
event — ``ChatUiService._create_payload`` raises ``ValueError`` on a null
scenario id, and ``BackendService`` gates its microphone on the scenario topic.
Pulling cltl-context into a topology just to get that event would turn every
two-module slice into a five-module one, and would make the slice depend on the
BDI handshake it is not trying to test.

It also avoids a side effect: ``ContextService._create_scenario`` calls
``requests.get("https://ipinfo.io")`` on every scenario start, inside a bare
``except``. Tests should not make network calls to geolocate the CI machine.
"""
import uuid
from typing import Optional

from cltl.combot.event.emissor import Agent, LeolaniContext, ScenarioStarted, ScenarioStopped
from cltl.combot.infra.event import Event, EventBus
from cltl.combot.infra.time_util import timestamp_now
from emissor.representation.scenario import Modality, Scenario

AGENT = Agent("Leolani", "http://cltl.nl/leolani/world/leolani")
SPEAKER = Agent("Human", "http://cltl.nl/leolani/world/human_speaker")

SIGNALS = {
    Modality.IMAGE.name.lower(): "./image.json",
    Modality.TEXT.name.lower(): "./text.json",
    Modality.AUDIO.name.lower(): "./audio.json",
}


def new_scenario(scenario_id: Optional[str] = None) -> Scenario:
    context = LeolaniContext(AGENT, SPEAKER, str(uuid.uuid4()), "test-location", [], [])
    return Scenario.new_instance(
        scenario_id or str(uuid.uuid4()), timestamp_now(), None, context, SIGNALS)


def start_scenario(event_bus: EventBus, scenario_topic: str,
                   scenario_id: Optional[str] = None) -> Scenario:
    """Publish ``ScenarioStarted`` and return the scenario."""
    scenario = new_scenario(scenario_id)
    event_bus.publish(scenario_topic, Event.for_payload(ScenarioStarted.create(scenario)))
    return scenario


def stop_scenario(event_bus: EventBus, scenario_topic: str, scenario: Scenario) -> None:
    scenario.ruler.end = timestamp_now()
    event_bus.publish(scenario_topic, Event.for_payload(ScenarioStopped.create(scenario)))
