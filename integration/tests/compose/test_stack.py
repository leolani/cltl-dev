"""Tier 2, the deployment itself: does the stack we ship actually come up.

Everything here is invisible to tier 1 by construction. Tier 1 composes Python
objects; these assertions are about images, working directories, mounted
configuration and a broker — the layer between "the modules integrate" and "the
thing we deploy runs".

The configuration check is the one that earns its place. Each image ships its
own ``config/default.config`` full of *legacy* topic names (``cltl.mic``,
``cltl.chat.utterance``). A container whose mounted configuration failed to land
— wrong working directory in the compose file, a typo in a volume — starts
cleanly, reports healthy, and listens on topics nobody publishes to. There is no
error anywhere; the pipeline is simply silent.
"""
import pytest
import requests

from cltl_integration.modules import BY_KEY
from cltl_integration.runner.compose import missing_images, required_images
from cltl_integration.topology import TEXT_PIPELINE

pytestmark = pytest.mark.compose

SCENARIO_TOPIC = "cltl.topic.scenario"
TEXT_OUT = "cltl.topic.text_out"


class TestImages:
    def test_every_module_image_is_present(self):
        """Fails fast with the build command rather than mid-test with a pull."""
        missing = missing_images(TEXT_PIPELINE)

        assert not missing, (
            f"missing images: {missing}. Build them with `make docker-ghcr-build` "
            f"in each component, and `docker pull rabbitmq:3.12-management`.")

    def test_required_images_cover_the_topology_and_the_broker(self):
        images = required_images(TEXT_PIPELINE)

        for key in TEXT_PIPELINE.modules:
            assert any(image.startswith(BY_KEY[key].image + ":") for image in images)
        assert any("rabbitmq" in image for image in images)


class TestStack:
    def test_every_module_reports_healthy(self, compose):
        """`up --wait` already gates on this; asserting it names the failure."""
        runner = compose(TEXT_PIPELINE)

        for key in TEXT_PIPELINE.modules:
            response = requests.get(f"{runner.base_url(key)}/health", timeout=10)
            assert response.status_code == 200, f"{key} is not healthy"

    def test_containers_read_the_mounted_configuration(self, compose):
        """Proves the volume targets match the images' working directories.

        Driven through behaviour rather than by reading a file out of a
        container: a module that fell back to its built-in defaults would be
        listening on `cltl.chat.utterance`, so nothing it publishes ever reaches
        `cltl.topic.scenario`.
        """
        runner = compose(TEXT_PIPELINE)
        runner.probe.subscribe(SCENARIO_TOPIC)

        from cltl_integration.drivers.bdi import publish_intention
        publish_intention(runner.event_bus, "cltl.topic.intention", "init")

        started = runner.probe.await_event(SCENARIO_TOPIC, timeout=30)

        assert started.payload.scenario.id

    def test_two_stacks_can_run_at_once(self, compose):
        """No container_name, no fixed ports, no shared broker volume.

        The prior art in app/docker-app could not do this: it pinned
        `container_name` and bind-mounted RabbitMQ's mnesia directory, so runs
        had to be serialized and retried.
        """
        first = compose(TEXT_PIPELINE)
        second = compose(TEXT_PIPELINE)

        assert first.project != second.project
        assert first.port("rabbitmq", 5672) != second.port("rabbitmq", 5672)
        assert requests.get(f"{first.base_url('eliza')}/health", timeout=10).ok
        assert requests.get(f"{second.base_url('eliza')}/health", timeout=10).ok


class TestProbe:
    def test_subscription_is_bound_before_subscribe_returns(self, compose):
        """Without the handshake the probe silently misses early events.

        ``KombuEventBus.subscribe`` starts a consumer thread and returns; the
        queue is declared and bound some time later, and anything published in
        between is routed nowhere. This publishes immediately after subscribing
        and requires the event back.

        ``cltl.topic.scenario`` on purpose: ChatUiService and InitService are
        already bound to it, so a readiness check that only asked whether
        *something* was bound would pass on their queues and return before the
        probe's own existed. This is the test that caught that.
        """
        runner = compose(TEXT_PIPELINE)
        runner.probe.subscribe(SCENARIO_TOPIC)

        from cltl_integration.drivers.scenario import start_scenario
        scenario = start_scenario(runner.event_bus, SCENARIO_TOPIC)

        observed = runner.probe.await_event(SCENARIO_TOPIC, timeout=15)

        assert observed.payload.scenario.id == scenario.id
