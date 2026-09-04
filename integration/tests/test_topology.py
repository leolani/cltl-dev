"""Tests for the harness itself: topology validation and container synthesis.

The deadlock guard earns its place here. Without it the failure mode is a test
that hangs with no error, no log line and no timeout — the single most expensive
thing to debug in this codebase.
"""
import pytest

from cltl_integration.modules import MODULES
from cltl_integration.runner.inprocess import (HarnessInfraContainer,
                                               InProcessRunner,
                                               build_container_type)
from cltl_integration.topology import (DEPLOYMENTS, ELIZA, ELIZA_CHATUI,
                                       TOPOLOGIES, Deployment, Topology,
                                       TopologyError, load_config,
                                       validate_config, validate_topology)


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


class TestFileLevelValidation:
    """``validate_topology`` reads the config files instead of a ConfigurationManager.

    It is the only check available for a stack this process does not attach to —
    the client half of a split configures itself inside its own containers.
    """

    def test_every_registered_topology_passes_in_both_tiers(self):
        for topology in TOPOLOGIES.values():
            for tier in ("inprocess", "compose"):
                validate_topology(topology, tier)  # must not raise


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


class TestAudioLockGuard:
    """[cltl.backend.tts] without [cltl.backend.mic] blocks the backend forever.

    SynchronizedTextToSpeech.say takes a write lock on the audio resource with
    timeout=-1, which becomes event.wait(timeout=None). Only
    SynchronizedMicrophone.start ever provides that resource.
    """

    def test_tts_without_mic_is_rejected(self, tmp_path):
        overlay = tmp_path / "deadlock.config"
        overlay.write_text("[cltl.backend.tts]\ntopic: cltl.topic.text_out\n")
        topology = Topology(name="deadlock", modules=("backend",))

        runner = InProcessRunner(topology, storage_dir=tmp_path / "storage",
                                 extra_config=[overlay])

        with pytest.raises(TopologyError, match="block forever"):
            runner.start()

    def test_tts_with_mic_is_allowed(self, tmp_path, monkeypatch, clean_di):
        overlay = tmp_path / "ok.config"
        overlay.write_text(
            "[cltl.backend.tts]\ntopic: cltl.topic.text_out\n"
            "[cltl.backend.mic]\ntopic: cltl.topic.microphone\n")
        topology = Topology(name="tts_ok", modules=("backend",))

        monkeypatch.setenv("CLTL_HTTP_BASE", "http://127.0.0.1:8000")
        monkeypatch.setenv("CLTL_STORAGE_DIR", str(tmp_path / "storage"))
        # Every variable the tier config interpolates, or EnvInterpolation warns.
        monkeypatch.setenv("CLTL_AUDIO_URL", "")
        load_config(topology, "inprocess", extra_config=[overlay])
        container = build_container_type(topology.ordered_modules())()

        validate_config(container.config_manager, topology)  # must not raise


class TestFailedStartCleansUp:
    def test_port_is_released_after_a_failed_start(self, tmp_path, inprocess):
        """A half-started topology must not poison the next test in the process."""
        overlay = tmp_path / "deadlock.config"
        overlay.write_text("[cltl.backend.tts]\ntopic: cltl.topic.text_out\n")

        runner = InProcessRunner(Topology(name="deadlock", modules=("backend",)),
                                 storage_dir=tmp_path / "bad", extra_config=[overlay])
        with pytest.raises(TopologyError):
            runner.start()

        # The next topology must start normally on the same port.
        good = inprocess(ELIZA_CHATUI)
        assert good.url("chatui").endswith("/chatui")
