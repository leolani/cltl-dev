"""Slice: cltl-context — the scenario/BDI handshake every pipeline starts with.

Nothing in the platform does anything until this handshake completes.
``ChatUiService`` refuses to build a payload without a scenario id, the backend
gates its microphone on the scenario topic, and every intention-gated service
(``ElizaService``, ``KeywordService``, ``InitService``) sits inactive until a
``BDIService`` publishes an intention it recognises. So a break here presents as
a system that starts cleanly and then stays silent — the failure mode that is
hardest to localise from an end-to-end test, and the reason this slice exists.

The loop under test, all of it inside cltl-context:

    IntentionEvent(["init"])                        (published by the test)
      -> ContextService  ->  ScenarioStarted        on cltl.topic.scenario
      -> InitService     ->  greeting               on cltl.topic.text_out
      <- "yes"                                      (published by the test)
      -> InitService     ->  DesireEvent(["initialized"])
      -> BDIService      ->  IntentionEvent(["eliza"])

The four services are wired only through the event bus, so driving it from the
outside is the same thing the rest of the platform does.
"""
import pytest
from cltl.combot.event.emissor import TextSignalEvent
from cltl.combot.infra.event import Event
from cltl.combot.infra.time_util import timestamp_now
from emissor.representation.scenario import TextSignal

from cltl_integration.drivers.bdi import is_desire, is_intention, labels, publish_intention
from cltl_integration.topology import CONTEXT

SCENARIO_TOPIC = "cltl.topic.scenario"
INTENTION_TOPIC = "cltl.topic.intention"
DESIRE_TOPIC = "cltl.topic.desire"
TEXT_IN = "cltl.topic.text_in"
TEXT_OUT = "cltl.topic.text_out"

GREETING = "Do you want to talk to me?"


def _utterance(text: str, scenario_id: str = None) -> TextSignalEvent:
    signal = TextSignal.for_scenario(
        scenario_id, timestamp_now(), timestamp_now(), None, text)

    return TextSignalEvent.for_speaker(signal)


@pytest.fixture
def context(inprocess):
    runner = inprocess(CONTEXT)
    runner.probe.subscribe(SCENARIO_TOPIC, INTENTION_TOPIC, DESIRE_TOPIC, TEXT_OUT)
    return runner


def _start(runner):
    """Run the handshake up to the greeting and return the started scenario."""
    publish_intention(runner.event_bus, INTENTION_TOPIC, "init")
    started = runner.probe.await_event(SCENARIO_TOPIC)

    return started.payload.scenario


class TestScenarioLifecycle:
    def test_init_intention_starts_a_scenario(self, context):
        publish_intention(context.event_bus, INTENTION_TOPIC, "init")

        started = context.probe.await_event(SCENARIO_TOPIC)

        assert started.payload.type == "ScenarioStarted"
        assert started.payload.scenario.id

    def test_started_scenario_carries_the_agent_and_speaker(self, context):
        """Downstream consumers read these off the scenario, not off the event."""
        publish_intention(context.event_bus, INTENTION_TOPIC, "init")

        scenario = context.probe.await_event(SCENARIO_TOPIC).payload.scenario

        assert scenario.context.agent.name == "Leolani"
        assert scenario.context.speaker is not None

    def test_terminate_intention_stops_the_scenario(self, context):
        scenario = _start(context)
        context.probe.clear()

        publish_intention(context.event_bus, INTENTION_TOPIC, "terminate")

        stopped = context.probe.await_event(
            SCENARIO_TOPIC, lambda event: event.payload.type == "ScenarioStopped")

        assert stopped.payload.scenario.id == scenario.id
        assert stopped.payload.scenario.ruler.end, "the stopped scenario has no end time"


class TestBdiHandshake:
    def test_scenario_start_offers_the_greeting(self, context):
        _start(context)

        greeting = context.probe.await_event(TEXT_OUT)

        assert GREETING in greeting.payload.signal.text

    def test_confirming_the_greeting_achieves_initialized(self, context):
        scenario = _start(context)
        context.probe.await_event(TEXT_OUT)

        context.event_bus.publish(TEXT_IN, Event.for_payload(_utterance("yes", scenario.id)))

        desire = context.probe.await_event(
            DESIRE_TOPIC, lambda event: is_desire(event, "initialized"))

        assert "initialized" in desire.payload.achieved

    def test_initialized_hands_the_conversation_to_eliza(self, context):
        """The BDI model maps init/initialized to the eliza intention.

        This is what activates ElizaService's topic worker; without it the whole
        text pipeline is inert.
        """
        scenario = _start(context)
        context.probe.await_event(TEXT_OUT)

        context.event_bus.publish(TEXT_IN, Event.for_payload(_utterance("yes", scenario.id)))

        intention = context.probe.await_event(
            INTENTION_TOPIC, lambda event: is_intention(event, "eliza"))

        assert labels(intention) == {"eliza"}

    def test_unrelated_utterance_does_not_initialize(self, context):
        """InitService waits for a confirmation; anything else leaves it waiting."""
        scenario = _start(context)
        context.probe.await_event(TEXT_OUT)
        context.probe.clear()

        context.event_bus.publish(
            TEXT_IN, Event.for_payload(_utterance("what is the weather", scenario.id)))

        with pytest.raises(AssertionError):
            context.probe.await_event(DESIRE_TOPIC, timeout=1.0)


class TestGreetingIsOptional:
    """With no greeting configured, InitService initialises on scenario start.

    Worth its own test because it is the configuration a headless pipeline wants
    — one that should not wait for a human to say "yes" — and because it is the
    only path through ``InitService._process`` that skips the timeout logic.
    """

    @pytest.fixture
    def silent_context(self, inprocess, tmp_path):
        overlay = tmp_path / "no-greeting.config"
        overlay.write_text("[cltl.intentions.init]\ngreeting:\n")

        runner = inprocess(CONTEXT, extra_config=(overlay,))
        runner.probe.subscribe(SCENARIO_TOPIC, DESIRE_TOPIC, TEXT_OUT)
        return runner

    def test_initializes_without_asking(self, silent_context):
        publish_intention(silent_context.event_bus, INTENTION_TOPIC, "init")

        desire = silent_context.probe.await_event(
            DESIRE_TOPIC, lambda event: is_desire(event, "initialized"))

        assert "initialized" in desire.payload.achieved
        assert not silent_context.probe.events(TEXT_OUT), "a greeting was sent anyway"


class TestKeywordQuit:
    @pytest.mark.xfail(
        strict=True,
        reason="KeywordService.start() hardcodes intentions=['chat'], and "
               "KeywordService.from_config ignores the configured [cltl.keyword] "
               "intentions. The BDI model only ever produces 'init' and 'eliza', "
               "so the worker never activates and the goodbye keyword is dead "
               "code. Fix: read intentions from config as ElizaService does.")
    def test_goodbye_ends_the_conversation(self, context):
        scenario = _start(context)
        context.probe.await_event(TEXT_OUT)
        context.event_bus.publish(TEXT_IN, Event.for_payload(_utterance("yes", scenario.id)))
        context.probe.await_event(INTENTION_TOPIC, lambda event: is_intention(event, "eliza"))
        context.probe.clear()

        context.event_bus.publish(TEXT_IN, Event.for_payload(_utterance("Bye", scenario.id)))

        # Short timeout: a strict xfail pays this wait on every single run.
        quit_desire = context.probe.await_event(
            DESIRE_TOPIC,
            lambda event: is_desire(event, "quit"),
            timeout=2.0)

        assert quit_desire
