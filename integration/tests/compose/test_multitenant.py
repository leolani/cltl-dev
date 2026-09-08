"""Tier 2: one shared cltl-eliza and two tenant deployments on one broker.

The deployment the event bus's ``tenant`` setting exists for, and the first thing
in the repo to configure it. Three compose projects over the same
``docker-compose.yml``: a server that leaves the tenant empty and therefore serves
everyone, and two tenants that each set their own id.

What makes it different from ``test_csplit.py`` is where the boundary is. A split
is two networks — the isolation is the absence of a route. Here everything shares
one broker, one exchange and one virtual host, and the only thing keeping two
tenants apart is the routing key their buses bind. So the isolation is asserted
from several independent vantage points, because any one of them could be right by
accident: the untenanted probe beside the server, a tenanted probe inside each
tenant, the chat UI a person would be looking at, and each tenant's own disk.

The round trip under test is tenant -> server -> tenant -> server -> tenant:
cltl-chat-ui in the tenant creates the text signal, the shared cltl-eliza answers
it, cltl-emissor-data — a different module, in the tenant — picks the answer up and
files it, and then the whole thing again.
"""
import json
import time

import pytest

from cltl_integration.drivers.chat import ChatClient
from cltl_integration.drivers.conversation import (INTENTION_TOPIC,
                                                   SCENARIO_TOPIC, TEXT_IN,
                                                   TEXT_OUT, TOPICS,
                                                   Conversation)
from cltl_integration.runner.compose import ComposeRunner
from cltl_integration.topology import MULTITENANT

pytestmark = pytest.mark.compose

REPLY_TIMEOUT = 30.0
PERSIST_TIMEOUT = 15.0
# Long enough that anything routed to the idle tenant would have arrived. There is
# no event to wait for in a negative assertion, so the only alternative to a pause
# is to assert on nothing at all.
SETTLE = 5.0

FIRST = "I feel very anxious today"
SECOND = "My mother never listens to me"


@pytest.fixture
def deployment(tenants):
    """The deployment, with a probe on the server and one inside each tenant.

    The two kinds of probe are the two vantage points the mechanism has. The
    server's bus is untenanted, so it binds ``<topic>.#`` and sees every tenant —
    the only place from which "these two tenants received different things" is
    observable at once. A tenant's bus binds ``<topic>.<tenant>``, so it cannot
    observe another tenant at all; that its record stays empty is not a filter
    applied after the fact but the broker declining to deliver.
    """
    runner = tenants(MULTITENANT)
    runner.probe.subscribe(*TOPICS)
    for tenant in runner.tenants:
        runner.tenant(tenant).probe.subscribe(*TOPICS)

    return runner


def _say(view, conversation, text: str, timeout: float = REPLY_TIMEOUT):
    """Say something in the tenant and return the answer that comes back.

    The probe is cleared first so that the answer is one that arrived *after* this
    utterance. It is deliberately not claimed to be the answer *to* it: the shared
    cltl-eliza runs ungated (see config/topologies/multitenant_server.config), so
    it also answers the consent the BDI handshake needs, and which of two answers
    in flight arrives first is not this test's subject. What matters here is that
    an answer came back at all, and came back into this tenant.
    """
    view.probe.clear()
    conversation.say(text)

    return view.probe.await_event(
        TEXT_OUT, lambda event: bool(event.payload.signal.text), timeout=timeout)


def _await_texts(storage_path, scenario_id: str, expected,
                 timeout: float = PERSIST_TIMEOUT):
    """Poll a tenant's persisted text signals until all of `expected` are there.

    Waits on content rather than on a count. The conversation carries turns this
    test did not ask for — the BDI greeting, the consent, and the shared eliza's
    ungated answer to that consent — so any count is reached before the turns that
    matter have been written, and the assertion passes on the wrong four signals.

    A failed parse counts as "not yet" rather than as an error. With
    `flush_interval: 0` cltl-emissor-data rewrites this file after every single
    event, truncating it first, so a reader that arrives mid-write legitimately
    sees an empty or half-written file.
    """
    path = storage_path / "emissor" / scenario_id / "text.json"
    expected = set(expected)
    end = time.monotonic() + timeout
    texts = []
    while time.monotonic() < end:
        if path.exists():
            try:
                texts = [signal["text"] for signal in json.loads(path.read_text())]
            except (json.JSONDecodeError, KeyError, TypeError):
                texts = []
            if expected <= set(texts):
                return texts
        time.sleep(0.05)

    raise AssertionError(
        f"{sorted(expected - set(texts))} never reached {path}; it holds {texts}")


class TestTenantRoundTrip:
    def test_the_round_trip_stays_inside_its_tenant(self, deployment):
        """tenant -> server -> tenant -> server -> tenant, with the other tenant idle.

        The scenario is opened by publishing the init intention on tenant A's own
        bus, and that alone exercises the tenanted publish path: an untenanted
        publish would be routed to the bare ``cltl.topic.intention`` key, which no
        tenant deployment binds, and the conversation would simply never start.
        """
        view = deployment.tenant("tenant-a")
        idle = deployment.tenant("tenant-b")

        conversation = Conversation(view).open().confirm()
        assert conversation.scenario_id

        first = _say(view, conversation, FIRST)
        second = _say(view, conversation, SECOND)

        # Two separate answers came back from the one cltl-eliza in the
        # deployment, and both came back tagged with the tenant that asked.
        assert first.id != second.id, "only one answer arrived for two utterances"
        for reply in (first, second):
            assert reply.metadata.tenant == "tenant-a"
            assert reply.payload.signal.text

        # cltl-chat-ui holds them — the module a person is looking at.
        assert conversation.replies(), "the chat UI never showed Eliza's answers"

        # cltl-emissor-data, a different module in the same tenant, filed the whole
        # exchange into this tenant's own store: both utterances and both answers.
        _await_texts(view.storage_path, conversation.scenario_id,
                     {FIRST, SECOND,
                      first.payload.signal.text, second.payload.signal.text})

        # ...and tenant B, which did nothing throughout, saw none of it. Three
        # levels, because any one of them could be right for the wrong reason.
        time.sleep(SETTLE)
        assert idle.probe.events() == [], (
            "tenant-b's bus received tenant-a's traffic: "
            f"{[(event.metadata.topic, event.metadata.tenant) for event in idle.probe.events()]}")
        assert not ChatClient(idle.url("chatui")).current()["scenario_id"]
        assert not list((idle.storage_path / "emissor").glob("*"))


class TestSharedServer:
    def test_the_server_relays_each_tenant_to_itself(self, deployment):
        """Both tenants talking at once, seen from the one place that sees both.

        The server's probe is untenanted, exactly like the shared cltl-eliza it
        sits beside, so what it records is what the shared half of the deployment
        receives. Every reply on it must belong to one tenant and to that tenant's
        scenario.
        """
        first, second = deployment.tenants
        a = Conversation(deployment.tenant(first)).open().confirm()
        b = Conversation(deployment.tenant(second)).open().confirm()

        assert a.scenario_id and b.scenario_id
        assert a.scenario_id != b.scenario_id, "both tenants opened the same scenario"

        _say(deployment.tenant(first), a, FIRST)
        _say(deployment.tenant(second), b, SECOND)

        replies = deployment.probe.events(TEXT_OUT)
        scenarios = {tenant: {event.metadata.scenario_id for event in replies
                              if event.metadata.tenant == tenant}
                     for tenant in deployment.tenants}
        assert scenarios[first] == {a.scenario_id}
        assert scenarios[second] == {b.scenario_id}

        # Nothing on text_out is untenanted. The one thing that could be is
        # ElizaService's greeting branch, which publishes Event.for_payload with no
        # `source` (cltl-eliza/src/cltl_service/eliza/service.py:83-86) and so loses
        # the tenant — it would be routed to the bare key and delivered to nobody.
        # config/topologies/multitenant_server.config makes that branch unreachable;
        # this fails readably if someone makes it reachable again.
        assert not [event for event in replies if not event.metadata.tenant]

        # And from inside, each tenant saw only its own.
        for tenant, scenario in ((first, a.scenario_id), (second, b.scenario_id)):
            seen = {event.metadata.scenario_id
                    for event in deployment.tenant(tenant).probe.events(TEXT_OUT)}
            assert seen == {scenario}, tenant


class TestBrokerRouting:
    """The mechanism itself, asked of RabbitMQ rather than inferred from delivery.

    ``deployment_bindings`` is snapshotted once every stack is up and before any
    probe subscribes, so these are the deployment's own queues and not the test's.
    """

    def test_the_shared_server_binds_tenant_agnostically(self, deployment):
        bindings = deployment.deployment_bindings

        assert bindings[ComposeRunner.binding_key(TEXT_IN)] >= 1, (
            "the shared cltl-eliza is not bound to every tenant's utterances")
        # ...and is on no tenant's intention topic. Not an oversight: TopicWorker
        # keeps one process-global active flag, so a shared module gated on an
        # intention is switched on and off by whichever tenant spoke last. See
        # config/topologies/multitenant_server.config and
        # tests/slices/test_intention_routing.py::TestTenantScopedGating.
        assert bindings[ComposeRunner.binding_key(INTENTION_TOPIC)] == 0

    def test_each_tenant_binds_only_its_own_key(self, deployment):
        bindings = deployment.deployment_bindings

        for tenant in deployment.tenants:
            for topic in (TEXT_IN, TEXT_OUT, SCENARIO_TOPIC, INTENTION_TOPIC):
                assert bindings[ComposeRunner.binding_key(topic, tenant)] >= 1, \
                    (topic, tenant)

        # No tenant deployment binds the wildcard form; if one did it would
        # receive every other tenant's traffic and this whole file would be
        # asserting nothing.
        assert bindings[ComposeRunner.binding_key(TEXT_OUT)] == 0

    def test_nothing_is_bound_for_a_tenant_that_is_not_deployed(self, deployment):
        """Every key on the exchange ends in a deployed tenant, or in the wildcard.

        The empty-topic keys are expected: ContextService.start passes its speaker
        topic unconditionally (cltl-context/src/cltl_service/context/service.py:56)
        while [cltl.context] topic_speaker is empty in base.config, so each tenant
        also binds a bare ".<tenant>". Tenant-scoped like everything else, and
        harmless, but it is a key with no topic in front of it.
        """
        suffixes = {key.rsplit(".", 1)[1] for key in deployment.deployment_bindings}

        assert suffixes <= set(deployment.tenants) | {"#"}, sorted(suffixes)


class TestTenantContext:
    def test_a_tenant_deployment_knows_its_tenant(self, deployment):
        """cltl-context complains about untenanted events; here it must not.

        BDIService and InitService both log a warning when an event reaches them
        with no tenant on its metadata (cltl-context/src/cltl_service/bdi/service.py:65,
        cltl_service/intentions/init.py:85). Every other deployment in the suite
        logs those on every event. This is the first one that should be quiet, and
        a silent regression in the CLTL_TENANT plumbing would make it noisy again.
        """
        view = deployment.tenant("tenant-a")
        _say(view, Conversation(view).open().confirm(), FIRST)

        logs = view.logs()

        assert "run in a tenant context" not in logs, (
            "cltl-context saw untenanted events in a tenant deployment")
        # The canary for the whole chain: if CLTL_TENANT never reached the
        # container, EnvInterpolation says so and the tenant becomes the literal
        # string "$CLTL_TENANT" — which routes, binds and fails silently.
        assert "Unexpanded environment variable" not in logs
