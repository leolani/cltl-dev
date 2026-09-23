"""Slice: cltl-backend's camera -> `cltl.topic.image`, and into image storage.

The image counterpart of ``test_backend_vad``, and narrower: nothing downstream
consumes the camera in this topology, so the seam under test is the one inside
cltl-backend itself. ``BackendContainer`` builds a ``ClientImageSource`` and
wraps it in an ``ImageCamera``; ``BackendService`` opens that camera, stores
every frame and publishes an ``ImageSignalEvent`` whose ``files[0]`` is a
``cltl-storage:image/<id>`` URL.

That wiring is the point. Both accessors returned a falsy ``[]`` placeholder
until now, which ``Backend.start()`` skips silently and
``BackendService._record_images`` would have died on with
``AttributeError: 'list' object has no attribute '__enter__'`` the moment a
deployment set a non-zero rate. A test that only checked "no crash on startup"
would have passed against the stubs.

Needs something on the other end of ``[cltl.backend] server_image_url`` speaking
the capture protocol — see ``cltl_integration.drivers.camera``, which is the
camera's stub, distinct from ``drivers.image``'s PNG bytes for the chat UI's
upload path.
"""
import time

import numpy as np
import pytest

from cltl_integration.drivers.camera import StubImageServer
from cltl_integration.drivers.scenario import start_scenario
from cltl_integration.topology import BACKEND, BACKEND_IMAGE

IMAGE_TOPIC = "cltl.topic.image"
SCENARIO_TOPIC = "cltl.topic.scenario"

#: What backend_image.config configures. Asserted against rather than read back,
#: so that a change to the overlay has to be a deliberate change here too.
RATE = 4


@pytest.fixture
def image_server():
    servers = []

    def _start() -> StubImageServer:
        server = StubImageServer().start()
        servers.append(server)
        return server

    yield _start

    for server in reversed(servers):
        server.stop()


@pytest.fixture
def backend_image(image_server, inprocess):
    """Start the stub camera, then a topology pointed at it.

    ``image_server`` is requested before ``inprocess`` for the reason the audio
    slice gives: the stub's port goes into the configuration, so it has to exist
    first, and reverse-order teardown then stops the topology before the camera
    disappears from under its capture thread.
    """
    def _start(topology=BACKEND_IMAGE, **kwargs):
        server = image_server()
        runner = inprocess(topology,
                           environment={"CLTL_IMAGE_URL": server.url}, **kwargs)
        runner.probe.subscribe(IMAGE_TOPIC)

        return runner, server

    return _start


class TestRecording:
    def test_camera_records_only_within_a_scenario(self, backend_image):
        """BackendService's image thread idles until it sees a scenario.

        The same gate as the microphone's, and worth pinning for the same
        reason: the thread is running and the stub is serving, so "nothing
        happens" is a deliberate gate rather than a broken wire.
        """
        runner, server = backend_image()

        with pytest.raises(AssertionError):
            runner.probe.await_event(IMAGE_TOPIC, timeout=2.0)

        assert server.served == 0, (
            f"the camera captured {server.served} frame(s) with no scenario open")

    def test_recording_publishes_a_storage_backed_image_signal(self, backend_image):
        """The end-to-end assertion: source -> camera -> storage -> event.

        This is what fails with the `[]` stubs, and it fails inside the capture
        thread, where ``BackendService`` catches and logs — so the visible
        symptom is this timeout rather than a traceback.
        """
        runner, _ = backend_image()
        start_scenario(runner.event_bus, SCENARIO_TOPIC)

        event = runner.probe.await_event(IMAGE_TOPIC, timeout=20.0)

        signal = event.payload.signal
        assert signal.files, "the image signal carries no storage URL"
        assert signal.files[0] == f"cltl-storage:image/{signal.id}", (
            "consumers resolve this URL against [cltl.backend] storage_url; any "
            "other shape is unfetchable")

    def test_captured_pixels_reach_storage_unchanged(self, backend_image):
        """The frame survives base64 ndarray -> storage -> HTTP GET.

        Fetched back through the storage service the same way a consumer would,
        which is what makes this more than a round trip through the stub.
        """
        runner, server = backend_image()
        start_scenario(runner.event_bus, SCENARIO_TOPIC)

        event = runner.probe.await_event(IMAGE_TOPIC, timeout=20.0)
        signal = event.payload.signal

        stored = runner.container.image_storage.get(signal.id)

        assert np.array_equal(stored.image, server.image.image), (
            "the stored frame differs from the one the camera served")

    def test_signal_bounds_match_the_camera_resolution(self, backend_image):
        """``ImageSignal.for_scenario`` takes its bounds from ``image.bounds``.

        A frame whose shape was lost in serialization still produces a plausible
        signal, so assert the geometry rather than only the URL.
        """
        runner, server = backend_image()
        start_scenario(runner.event_bus, SCENARIO_TOPIC)

        event = runner.probe.await_event(IMAGE_TOPIC, timeout=20.0)

        height, width = server.image.image.shape[:2]
        assert tuple(event.payload.signal.ruler.bounds) == (0, 0, width, height), (
            "the signal's MultiIndex is built from image.bounds.to_diagonal()")


class TestCameraIsOptional:
    def test_camera_is_absent_when_the_rate_is_zero(self, backend_image):
        """The regression guard for the falsy-sentinel decision.

        Every topology but ``backend_image`` leaves ``[cltl.backend.image] rate``
        at 0, and ``BackendContainer.camera`` returns ``[]`` there rather than an
        ``ImageCamera``. That matters beyond tidiness: ``ImageCamera.record()``
        only short-circuits below zero, so a camera built at rate 0 would capture
        in an unthrottled loop. This test fails if someone later "simplifies" the
        container to construct one unconditionally.
        """
        runner, server = backend_image(topology=BACKEND)
        start_scenario(runner.event_bus, SCENARIO_TOPIC)

        with pytest.raises(AssertionError):
            runner.probe.await_event(IMAGE_TOPIC, timeout=3.0)

        assert server.served == 0, (
            f"a topology with rate 0 captured {server.served} frame(s): the "
            "camera was built when it should have been absent")

    def test_camera_accessor_is_falsy_without_a_rate(self, backend_image):
        """``Backend.start()`` guards on ``if self._camera:``, so this is the
        property the absent case actually depends on."""
        runner, _ = backend_image(topology=BACKEND)

        assert not runner.container.camera


class TestPacing:
    def test_images_are_paced_at_the_configured_rate(self, backend_image):
        """``ImageCamera`` was handed the rate the service gated on.

        A camera built with the wrong rate still passes every test above — it
        just runs hot, capturing as fast as the socket allows. Measuring the
        count over a window is what distinguishes the two.
        """
        runner, server = backend_image()
        start_scenario(runner.event_bus, SCENARIO_TOPIC)

        server.await_capture(1, timeout=20.0)

        window = 2.0
        start = server.served
        time.sleep(window)
        captured = server.served - start

        expected = RATE * window
        assert captured <= expected * 2.5, (
            f"captured {captured} frame(s) in {window}s, expected about "
            f"{expected:.0f} at {RATE}/s: the camera is not pacing")
        assert captured >= 1, (
            f"captured {captured} frame(s) in {window}s at {RATE}/s: the camera "
            "stopped after the first frame")
