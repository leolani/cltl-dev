"""Tests for the harness itself: topology definition and container synthesis.

Cheap, and they fail for reasons that are otherwise expensive to see. A topology
naming a module that does not exist, or an overlay that was renamed and not
followed, shows up here in milliseconds rather than as a stack that comes up
healthy and wired to nothing.
"""
import pytest

from cltl_integration.modules import MODULES
from cltl_integration.runner.inprocess import (HarnessInfraContainer,
                                               InProcessRunner,
                                               build_container_type)
from cltl_integration.topology import (DEPLOYMENTS, ELIZA, ELIZA_CHATUI,
                                       MULTITENANT_SERVER, TENANT_DEPLOYMENTS,
                                       TOPOLOGIES, Deployment, TenantDeployment,
                                       Topology, TopologyError, merged_config)


class TestTopologyDefinition:
    def test_unknown_module_is_rejected(self):
        with pytest.raises(TopologyError, match="unknown module"):
            Topology(name="bogus", modules=("eliza", "not-a-module"))

    def test_duplicate_modules_are_rejected(self):
        with pytest.raises(TopologyError, match="duplicate"):
            Topology(name="bogus", modules=("eliza", "eliza"))

    def test_modules_are_returned_in_registry_order(self):
        """Container MRO order is load-bearing; the order a topology is written in is not."""
        written_backwards = Topology(name="t", modules=("chatui", "eliza"))

        assert [m.key for m in written_backwards.ordered_modules()] == ["eliza", "chatui"]

    def test_registered_topologies_resolve_their_config(self):
        for topology in TOPOLOGIES.values():
            base, additional = topology.config_files("inprocess")
            assert base.exists(), f"{topology.name}: missing {base}"
            for path in additional:
                assert path.exists(), f"{topology.name}: missing {path}"

    def test_unknown_tier_is_rejected(self):
        with pytest.raises(TopologyError, match="unknown tier"):
            ELIZA.config_files("nonsense")


class TestDeploymentDefinition:
    """The client/server split — two stacks, and the rules that keep it one system."""

    def test_a_server_without_storage_is_rejected(self):
        """The client's `audio_storage: remote` has to have somewhere to go."""
        with pytest.raises(TopologyError, match="storage endpoint"):
            Deployment(name="bogus",
                       server=Topology(name="s", modules=("eliza",)),
                       client=Topology(name="c", modules=("backend", "chatui")))

    def test_a_module_on_both_halves_is_rejected(self):
        """Two instances of one module share out its topics rather than both getting them.

        Each container's TopicWorker binds its own queue to the same routing key,
        so RabbitMQ round-robins between them: half the utterances get answered.
        The failure looks like flakiness, not like a misconfiguration.
        """
        with pytest.raises(TopologyError, match="both halves"):
            Deployment(name="bogus",
                       server=Topology(name="s", modules=("backend", "eliza")),
                       client=Topology(name="c", modules=("backend", "eliza")))

    def test_the_backend_is_expected_on_both_halves(self):
        """It is the one module that is genuinely two different things here:
        storage on the server, devices and a storage *proxy* on the client."""
        deployment = Deployment(
            name="ok",
            server=Topology(name="s", modules=("backend", "eliza")),
            client=Topology(name="c", modules=("backend", "chatui")))

        assert [t.name for t in deployment.topologies()] == ["s", "c"]

    def test_registered_deployments_resolve_their_config(self):
        for deployment in DEPLOYMENTS.values():
            for topology in deployment.topologies():
                base, additional = topology.config_files("compose")
                assert base.exists(), f"{topology.name}: missing {base}"
                for path in additional:
                    assert path.exists(), f"{topology.name}: missing {path}"


class TestTenantDeploymentDefinition:
    """One shared server and N tenants — the rules that keep them apart."""

    SERVER = Topology(name="s", modules=("eliza",))
    TENANT = Topology(name="t", modules=("context", "chatui"))

    def _deployment(self, **kwargs) -> TenantDeployment:
        return TenantDeployment(**{"name": "bogus", "server": self.SERVER,
                                   "tenant": self.TENANT,
                                   "tenants": ("tenant-a", "tenant-b"), **kwargs})

    def test_one_tenant_is_rejected(self):
        with pytest.raises(TopologyError, match="at least two tenants"):
            self._deployment(tenants=("tenant-a",))

    def test_duplicate_tenant_ids_are_rejected(self):
        """Two stacks on one id bind the same keys and split the traffic."""
        with pytest.raises(TopologyError, match="duplicate tenant ids"):
            self._deployment(tenants=("tenant-a", "tenant-a"))

    @pytest.mark.parametrize("tenant", ["tenant.a", "tenant-*", "#", "Tenant"])
    def test_an_id_that_is_not_one_routing_key_word_is_rejected(self, tenant):
        """`<topic>.<tenant>` is an AMQP routing key, and '.', '*' and '#' are
        its separator and its two wildcards — an id containing one silently
        changes which keys the pattern matches."""
        with pytest.raises(TopologyError, match="routing-key word"):
            self._deployment(tenants=(tenant, "tenant-b"))

    def test_a_module_on_the_server_and_in_the_tenants_is_rejected(self):
        """The server's `<topic>.#` and the tenant's `<topic>.<tenant>` both match,
        so the tenant's events are delivered to both and processed twice."""
        with pytest.raises(TopologyError, match="processed twice"):
            self._deployment(tenant=Topology(name="t", modules=("chatui", "eliza")))

    def test_the_same_module_on_two_tenants_is_the_point(self):
        """The inverse of a Deployment's rule: two tenants bind different keys,
        so neither can see the other's traffic however identical they are."""
        deployment = self._deployment(name="ok")

        assert [t.name for t in deployment.topologies()] == ["s", "t"]
        assert deployment.tenants == ("tenant-a", "tenant-b")

    def test_registered_tenant_deployments_resolve_their_config(self):
        for deployment in TENANT_DEPLOYMENTS.values():
            for topology in deployment.topologies():
                base, additional = topology.config_files("compose")
                assert base.exists(), f"{topology.name}: missing {base}"
                for path in additional:
                    assert path.exists(), f"{topology.name}: missing {path}"

    def test_every_half_carries_the_tenant_placeholder(self):
        """Each stack's tenant arrives from its environment, not from its files:
        one configuration, deployed unchanged to the server and every tenant."""
        for deployment in TENANT_DEPLOYMENTS.values():
            for topology in deployment.topologies():
                config = merged_config(topology, "compose")
                assert config.get("cltl.event.kombu", "tenant") == "$CLTL_TENANT", \
                    topology.name

    def test_the_shared_server_is_ungated_and_deaf_to_intentions(self):
        """Both halves of one decision — the reasons are in the overlay's header.

        Gating a module that serves every tenant is meaningless while
        TopicWorker keeps one process-global flag, and merely leaving
        `intentions` empty is not enough: ElizaService still subscribes to a
        non-empty intention topic, which on an untenanted bus is every tenant's.
        """
        config = merged_config(MULTITENANT_SERVER, "compose")

        assert config.get("cltl.eliza", "intentions") == ""
        assert config.get("cltl.eliza", "topic_intention") == ""
        assert config.get("cltl.eliza", "topic_desire") == ""


class TestRegisteredTopologies:
    def test_every_topology_resolves_its_config_in_both_tiers(self):
        """An overlay renamed without following it leaves a topology that starts
        on base.config alone: every module disabled, and every assertion silent
        rather than failing."""
        for topology in TOPOLOGIES.values():
            for tier in ("inprocess", "compose"):
                base, additional = topology.config_files(tier)
                assert base.exists(), f"{topology.name}/{tier}: missing {base}"
                for path in additional:
                    assert path.exists(), f"{topology.name}/{tier}: missing {path}"


class TestContainerSynthesis:
    def test_event_bus_override_comes_first(self):
        """Anything later in the MRO would win and quietly connect to RabbitMQ."""
        container_type = build_container_type(ELIZA_CHATUI.ordered_modules())

        assert container_type.__mro__[1] is HarnessInfraContainer

    def test_every_registered_module_can_be_composed(self):
        """Guards against a container that cannot be linearised with the others."""
        container_type = build_container_type(MODULES)

        mro_names = [cls.__name__ for cls in container_type.__mro__]
        for module in MODULES:
            _, class_name = module.container_ref()
            assert class_name in mro_names

    def test_an_override_wins_over_every_base(self):
        """How a topology swaps in a test double — see drivers.asr.asr_override.

        Placed in the synthesised class' own namespace rather than a mixin, so
        it does not depend on where the component container lands in the MRO.
        """
        sentinel = object()

        container_type = build_container_type(
            ELIZA_CHATUI.ordered_modules(), {"eliza_service": sentinel})

        assert container_type.eliza_service is sentinel


class TestFailedStartCleansUp:
    def test_port_is_released_after_a_failed_start(self, tmp_path, inprocess):
        """A half-started topology must not poison the next test in the process.

        The trigger is a typo in a config value, which is what this actually
        happens to: ``ASRContainer._create_asr_implementation`` raises on an
        implementation it does not recognise, by which point the runner has
        already bound its port and loaded the configuration.
        """
        overlay = tmp_path / "bad_asr.config"
        overlay.write_text("[cltl.asr]\nimplementation: no-such-recogniser\n")

        runner = InProcessRunner(Topology(name="bad_asr", modules=("asr",)),
                                 storage_dir=tmp_path / "bad", extra_config=[overlay])
        with pytest.raises(ValueError, match="Unsupported implementation"):
            runner.start()

        # The next topology must start normally on the same port.
        good = inprocess(ELIZA_CHATUI)
        assert good.url("chatui").endswith("/chatui")
