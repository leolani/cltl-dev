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
from cltl.commons.language_data.sentences import GOODBYE
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


def _reach_eliza(runner):
    """Complete the whole handshake, leaving the conversation in `eliza`.

    Where every keyword test has to start: the keyword worker is gated on the
    `eliza` intention, so nothing it does is observable until BDIService has
    published it. Clears the probe so the caller sees only what it triggers.
    """
    scenario = _start(runner)
    runner.probe.await_event(TEXT_OUT)
    runner.event_bus.publish(TEXT_IN, Event.for_payload(_utterance("yes", scenario.id)))
    runner.probe.await_event(INTENTION_TOPIC, lambda event: is_intention(event, "eliza"))
    runner.probe.clear()

    return scenario


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
    """Saying goodbye ends the conversation.

    The only user-initiated way to end a scenario, and it is gated on the
    `eliza` intention — which is what made it worth a slice test: before
    KeywordService read its intentions from configuration it was gated on a
    label no BDI model here produces, so the keyword was unreachable and the
    failure was invisible. Nothing about a dead keyword looks broken from the
    outside; the conversation simply never ends.

    The loop these tests close:

        "Bye"  ->  KeywordService  ->  DesireEvent(["quit"])
                -> BDIService      ->  IntentionEvent(["init"])
    """

    def test_goodbye_ends_the_conversation(self, context):
        scenario = _reach_eliza(context)

        context.event_bus.publish(TEXT_IN, Event.for_payload(_utterance("Bye", scenario.id)))

        quit_desire = context.probe.await_event(
            DESIRE_TOPIC, lambda event: is_desire(event, "quit"))

        assert "quit" in quit_desire.payload.achieved

    def test_goodbye_is_acknowledged(self, context):
        """The user gets a farewell, not a silent shutdown."""
        scenario = _reach_eliza(context)

        context.event_bus.publish(TEXT_IN, Event.for_payload(_utterance("Bye", scenario.id)))

        farewell = context.probe.await_event(TEXT_OUT)

        assert farewell.payload.signal.text in GOODBYE

    def test_quit_returns_the_bdi_to_init(self, context):
        """The quit desire closes the loop rather than just stopping.

        `[cltl.bdi] model` maps eliza/quit back to init, so the agent becomes
        ready to greet the next person. Gating the keyword on `eliza` is what
        makes that reachable.
        """
        scenario = _reach_eliza(context)

        context.event_bus.publish(TEXT_IN, Event.for_payload(_utterance("Bye", scenario.id)))

        intention = context.probe.await_event(
            INTENTION_TOPIC, lambda event: is_intention(event, "init"))

        assert labels(intention) == {"init"}

    def test_keyword_is_inert_before_the_conversation_starts(self, context):
        """Gated on `eliza`, so "Bye" during the init handshake is just text.

        The test that fails if someone "fixes" the gating by emptying
        [cltl.keyword] intentions: an ungated worker would quit here.
        """
        scenario = _start(context)
        context.probe.await_event(TEXT_OUT)
        context.probe.clear()

        context.event_bus.publish(TEXT_IN, Event.for_payload(_utterance("Bye", scenario.id)))

        with pytest.raises(AssertionError):
            context.probe.await_event(
                DESIRE_TOPIC, lambda event: is_desire(event, "quit"), timeout=1.0)

    def test_ordinary_utterance_does_not_quit(self, context):
        scenario = _reach_eliza(context)

        context.event_bus.publish(
            TEXT_IN, Event.for_payload(_utterance("how are you", scenario.id)))

        with pytest.raises(AssertionError):
            context.probe.await_event(
                DESIRE_TOPIC, lambda event: is_desire(event, "quit"), timeout=1.0)


class TestCustomKeywords:
    """The quit vocabulary is configuration, not a hardcoded list.

    The only end-to-end check that `[cltl.keyword] keywords` is wired at all: a
    typo in the key name is invisible to every other test, because the service
    falls back to GOODBYE and goes on working.
    """

    @pytest.fixture
    def custom_context(self, inprocess, tmp_path):
        overlay = tmp_path / "keywords.config"
        # Only `keywords` — `intentions: eliza` is still inherited from
        # base.config, so these tests run the full handshake like any other.
        overlay.write_text("[cltl.keyword]\nkeywords: enough, stop talking\n")

        runner = inprocess(CONTEXT, extra_config=(overlay,))
        runner.probe.subscribe(SCENARIO_TOPIC, INTENTION_TOPIC, DESIRE_TOPIC, TEXT_OUT)
        return runner

    def test_configured_keyword_ends_the_conversation(self, custom_context):
        scenario = _reach_eliza(custom_context)

        custom_context.event_bus.publish(
            TEXT_IN, Event.for_payload(_utterance("enough", scenario.id)))

        quit_desire = custom_context.probe.await_event(
            DESIRE_TOPIC, lambda event: is_desire(event, "quit"))

        assert "quit" in quit_desire.payload.achieved

    def test_multi_word_configured_keyword_matches(self, custom_context):
        """Comma-splitting and per-element stripping survive the real config pipeline.

        The overlay writes "enough, stop talking" with a space after the comma,
        so a match here also proves the value was stripped.
        """
        scenario = _reach_eliza(custom_context)

        custom_context.event_bus.publish(
            TEXT_IN, Event.for_payload(_utterance("stop talking", scenario.id)))

        quit_desire = custom_context.probe.await_event(
            DESIRE_TOPIC, lambda event: is_desire(event, "quit"))

        assert "quit" in quit_desire.payload.achieved

    def test_default_goodbye_is_replaced_not_extended(self, custom_context):
        """Configuring keywords overrides GOODBYE rather than adding to it.

        The behaviour most likely to surprise an operator, and invisible to a
        test that only checks the positive case.
        """
        scenario = _reach_eliza(custom_context)

        custom_context.event_bus.publish(
            TEXT_IN, Event.for_payload(_utterance("Bye", scenario.id)))

        with pytest.raises(AssertionError):
            custom_context.probe.await_event(
                DESIRE_TOPIC, lambda event: is_desire(event, "quit"), timeout=1.0)


class TestConfiguredFarewell:
    """What the agent says on its way out is configuration too.

    Separate from TestCustomKeywords because each overlay needs its own
    topology: a runner holds port 8000 and the process-global DI registry, so a
    test class gets one.
    """

    @pytest.fixture
    def farewell_context(self, inprocess, tmp_path):
        overlay = tmp_path / "farewell.config"
        overlay.write_text("[cltl.keyword]\ngreetings: Until next time\n")

        runner = inprocess(CONTEXT, extra_config=(overlay,))
        runner.probe.subscribe(SCENARIO_TOPIC, INTENTION_TOPIC, DESIRE_TOPIC, TEXT_OUT)
        return runner

    def test_configured_farewell_is_used(self, farewell_context):
        """`greetings` replaces the GOODBYE pool the agent says goodbye with."""
        scenario = _reach_eliza(farewell_context)

        farewell_context.event_bus.publish(
            TEXT_IN, Event.for_payload(_utterance("Bye", scenario.id)))

        farewell = farewell_context.probe.await_event(TEXT_OUT)

        assert farewell.payload.signal.text == "Until next time"


class TestSilentFarewell:
    """An empty `greetings` ends the conversation without saying anything.

    The quit desire is unconditional, so the scenario still ends — only the
    text output is suppressed. What a headless pipeline with no text sink wants.
    """

    @pytest.fixture
    def silent_context(self, inprocess, tmp_path):
        overlay = tmp_path / "silent.config"
        overlay.write_text("[cltl.keyword]\ngreetings:\n")

        runner = inprocess(CONTEXT, extra_config=(overlay,))
        runner.probe.subscribe(SCENARIO_TOPIC, INTENTION_TOPIC, DESIRE_TOPIC, TEXT_OUT)
        return runner

    def test_empty_farewell_quits_silently(self, silent_context):
        scenario = _reach_eliza(silent_context)
        silent_context.probe.clear()

        silent_context.event_bus.publish(
            TEXT_IN, Event.for_payload(_utterance("Bye", scenario.id)))

        quit_desire = silent_context.probe.await_event(
            DESIRE_TOPIC, lambda event: is_desire(event, "quit"))

        assert "quit" in quit_desire.payload.achieved
        assert not silent_context.probe.events(TEXT_OUT), "a farewell was published anyway"
