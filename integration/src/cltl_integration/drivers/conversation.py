"""Drive a text conversation the way a person would, in either tier.

The steps are identical whichever runner is behind it — publish the initial
intention, wait for the scenario, accept the greeting, then talk — because both
runners expose the same three things this needs: an event bus, a probe, and the
chat UI's URL. That is what makes a pipeline assertion portable between a
process and a compose stack, and it is the only place the two tiers are allowed
to share a test body.

What the tiers do *not* share is what an assertion proves. In tier 1 the payload
handed to a subscriber is the object the publisher created; in tier 2 it has
been through ``marshal``/``unmarshal`` and back, so an assertion on a payload
field is genuinely a different test.
"""
import logging

from cltl_integration.drivers.bdi import is_desire, is_intention, publish_intention
from cltl_integration.drivers.chat import ChatClient

logger = logging.getLogger(__name__)

SCENARIO_TOPIC = "cltl.topic.scenario"
INTENTION_TOPIC = "cltl.topic.intention"
DESIRE_TOPIC = "cltl.topic.desire"
TEXT_IN = "cltl.topic.text_in"
TEXT_OUT = "cltl.topic.text_out"

TOPICS = (SCENARIO_TOPIC, INTENTION_TOPIC, DESIRE_TOPIC, TEXT_IN, TEXT_OUT)

GREETING = "Do you want to talk to me?"
CONFIRMATION = "yes"


class Conversation:
    """The chat UI plus the bus, driven the way the application drives them."""

    def __init__(self, runner):
        self.runner = runner
        self.client = ChatClient(runner.url("chatui"))
        self.chat_id = None
        self.scenario_id = None

    def open(self) -> "Conversation":
        """Publish the initial intention, as ``app/py-app/app.py`` does at startup."""
        publish_intention(self.runner.event_bus, INTENTION_TOPIC, "init")

        started = self.runner.probe.await_event(SCENARIO_TOPIC)
        self.scenario_id = started.payload.scenario.id
        self.runner.probe.await_event(
            TEXT_OUT, lambda event: GREETING in event.payload.signal.text)

        self.client.await_scenario()
        self.chat_id = self.client.start_session()

        return self

    def confirm(self) -> "Conversation":
        """Accept the greeting and wait for the handover to Eliza."""
        self.client.send(self.chat_id, CONFIRMATION)
        self.runner.probe.await_event(
            DESIRE_TOPIC, lambda event: is_desire(event, "initialized"))
        self.runner.probe.await_event(
            INTENTION_TOPIC, lambda event: is_intention(event, "eliza"))

        return self

    def say(self, text: str) -> None:
        self.client.send(self.chat_id, text)

    def replies(self):
        return self.client.receive(self.chat_id, from_sequence=self.client.initial_sequence)
