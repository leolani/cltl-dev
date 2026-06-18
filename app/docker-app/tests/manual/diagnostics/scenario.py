"""Test scenario creation and lifecycle for diagnostics."""
import json
import logging
import uuid
from pathlib import Path

from cltl.combot.event.emissor import Agent, LeolaniContext, ScenarioStarted, ScenarioStopped
from cltl.combot.infra.event.api import Event
from cltl.combot.infra.event.kombu import KombuEventBus
from cltl.combot.infra.time_util import timestamp_now
from emissor.representation.scenario import Modality, Scenario

logger = logging.getLogger(__name__)

SCENARIO_TOPIC = "cltl.topic.scenario"
_PAYLOAD_FILE = Path(__file__).parent / "payloads" / "scenario.json"


def _load_scenario_context() -> dict:
    with _PAYLOAD_FILE.open() as f:
        return json.load(f)


def create_test_scenario(bus: KombuEventBus) -> str:
    """Create and publish a ScenarioStarted event; return the scenario ID.

    The scenario context is populated from ``payloads/scenario.json`` and
    augmented with a fresh scenario_id so every run is isolated.
    The speaker field is set to ``test-diagnostic`` to make the test origin
    visible in EMISSOR recordings.
    """
    ctx_data = _load_scenario_context()
    scenario_id = str(uuid.uuid4())
    start = timestamp_now()

    context = LeolaniContext(
        agent=Agent(name=ctx_data.get("agent", "leolani")),
        speaker=Agent(name=ctx_data.get("speaker", "test-diagnostic")),
        location_id=ctx_data.get("location_id", str(uuid.uuid4())),
        location=ctx_data.get("location", "diagnostics"),
        persons=[],
        objects=[],
    )
    signals = {
        Modality.IMAGE.name.lower(): "./image.json",
        Modality.TEXT.name.lower(): "./text.json",
        Modality.AUDIO.name.lower(): "./audio.json",
    }
    scenario = Scenario.new_instance(scenario_id, start, None, context, signals)

    event = Event.for_payload(ScenarioStarted.create(scenario))
    bus.publish(SCENARIO_TOPIC, event)
    logger.info("Published ScenarioStarted for scenario %s", scenario_id)
    return scenario_id


def stop_test_scenario(bus: KombuEventBus, scenario_id: str) -> None:
    """Publish a ScenarioStopped event to cleanly close the test scenario."""
    stop = timestamp_now()
    context = LeolaniContext(
        agent=Agent(name="leolani"),
        speaker=Agent(name="test-diagnostic"),
        location_id=str(uuid.uuid4()),
        location="diagnostics",
        persons=[],
        objects=[],
    )
    scenario = Scenario.new_instance(scenario_id, stop, stop, context, {})
    event = Event.for_scenario_payload(scenario_id, ScenarioStopped.create(scenario))
    bus.publish(SCENARIO_TOPIC, event)
    logger.info("Published ScenarioStopped for scenario %s", scenario_id)
