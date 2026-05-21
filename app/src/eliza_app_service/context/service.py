import logging
import uuid
from datetime import datetime

import requests
from cltl.combot.event.emissor import LeolaniContext, Agent, ScenarioStarted, ScenarioStopped, ScenarioEvent
from cltl.combot.infra.config import ConfigurationManager
from cltl.combot.infra.event import Event, EventBus
from cltl.combot.infra.resource import ResourceManager
from cltl.combot.infra.time_util import timestamp_now
from cltl.combot.infra.topic_worker import TopicWorker
from emissor.representation.scenario import Modality, Scenario

logger = logging.getLogger(__name__)


AGENT = Agent("Leolani", "http://cltl.nl/leolani/world/leolani")
SPEAKER = Agent("Human", "http://cltl.nl/leolani/world/human_speaker")


class ContextService:
    @classmethod
    def from_config(cls, event_bus: EventBus, resource_manager: ResourceManager, config_manager: ConfigurationManager):
        config = config_manager.get_config("eliza.context")
        scenario_topic = config.get("topic_scenario")
        intention_topic = config.get("topic_intention")
        desire_topic = config.get("topic_desire")

        return cls(scenario_topic, intention_topic, desire_topic,
                   event_bus, resource_manager)

    def __init__(self, scenario_topic: str, intention_topic: str, desire_topic: str,
                 event_bus: EventBus, resource_manager: ResourceManager):
        self._event_bus = event_bus
        self._resource_manager = resource_manager

        self._scenario_topic = scenario_topic
        self._intention_topic = intention_topic
        self._desire_topic = desire_topic

        self._topic_worker = None

        self.AGENT = AGENT
        self._scenario = None

    @property
    def app(self):
        return None

    def start(self, timeout=30):
        self._topic_worker = TopicWorker([self._intention_topic, self._desire_topic],
                                         self._event_bus, provides=[self._intention_topic],
                                         buffer_size=32, processor=self._process,
                                         resource_manager=self._resource_manager,
                                         name=self.__class__.__name__)
        self._topic_worker.start().wait()

    def stop(self):
        if not self._topic_worker:
            pass

        self._topic_worker.stop()
        self._topic_worker.await_stop()
        self._topic_worker = None

    def _process(self, event: Event):
        if event.metadata.topic == self._intention_topic:
            intentions = {intention.label for intention in event.payload.intentions}
            if "init" in intentions:
                self._start_scenario()
            if "terminate" in intentions:
                self._stop_scenario()
        elif event.metadata.topic == self._desire_topic:
            achieved = event.payload.achieved
            if "quit" in achieved:
                self._stop_scenario()
        else:
            logger.warning("Unhandled event: %s", event)

    def _start_scenario(self):
        scenario, capsule = self._create_scenario()
        self._event_bus.publish(self._scenario_topic, Event.for_payload(ScenarioStarted.create(scenario)))
        self._scenario = scenario
        logger.info("Started scenario %s", scenario)

    def _update_scenario_speaker(self, event):
        # TODO multiple mentions
        mention = event.payload.mentions[0]
        name_annotation = next(iter(filter(lambda a: a.type == "Entity", mention.annotations)))

        speaker_name = name_annotation.value.text
        self._scenario.context.speaker = Agent(speaker_name, str(f"http://cltl.nl/leolani/friends/{speaker_name}"))

        self._event_bus.publish(self._scenario_topic, Event.for_payload(ScenarioEvent.create(self._scenario)))
        logger.info("Updated scenario %s", self._scenario)

    def _stop_scenario(self):
        self._scenario.ruler.end = timestamp_now()
        self._event_bus.publish(self._scenario_topic,
                                Event.for_payload(ScenarioStopped.create(self._scenario)))
        logger.info("Stopped scenario %s", self._scenario)

    def _create_scenario(self):
        signals = {
            Modality.IMAGE.name.lower(): "./image.json",
            Modality.TEXT.name.lower(): "./text.json",
            Modality.AUDIO.name.lower(): "./audio.json"
        }

        scenario_start = timestamp_now()
        location = self._get_location()

        scenario_context = LeolaniContext(AGENT, SPEAKER, str(uuid.uuid4()), location, [], [])
        scenario = Scenario.new_instance(str(uuid.uuid4()), scenario_start, None, scenario_context, signals)

        capsule = {
            "type": "context",
            "context_id": scenario.id,
            "date": datetime.utcfromtimestamp(scenario_start//1000).strftime('%Y-%m-%d'),
            "place": None,
            "place_id": None,
            "country": location["country"],
            "region": location["region"],
            "city": location["city"]
        }

        return scenario, capsule

    def _get_location(self):
        try:
            return requests.get("https://ipinfo.io").json()
        except:
            return {"country": "", "region": "", "city": ""}
