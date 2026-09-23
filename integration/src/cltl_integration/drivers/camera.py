"""A stand-in camera: synthetic frames served over cltl-backend's image protocol.

``BackendService`` does not read an image file or take an injected buffer. Its
camera is an ``ImageCamera`` wrapping a ``ClientImageSource``, which GETs
``[cltl.backend] server_image_url`` + ``/image`` and parses the body with
``cltl.backend.api.serialization.image_hook``::

    Content-Type: application/json; resolution=VGA
    Body:         {"image": {"__type": "np.ndarray", "data": <base64>,
                             "shape": [h, w, 3], "dtype": "uint8"},
                   "view": [x0, x1, y0, y1],
                   "depth": null}

So a test that wants the backend to see something has to speak that protocol.
This is the camera counterpart of ``drivers.audio.StubAudioServer`` and is
shaped on it deliberately: same lifecycle, same port-0 binding, same
``await_connection`` so a test waits for the capture rather than for the publish
that eventually causes it.

Separate from ``drivers/image.py``, which is not this. That module hand-rolls
PNG bytes for the chat UI's *upload* path and commits in its docstring to
staying free of numpy and cv2. The camera protocol is a different wire format
entirely — raw base64 ndarray, no image codec — so it needs numpy and cannot
share that module's constraints.

No cv2 here either, for the reason the whole image path avoids it: encoding is
``BackendJSONEncoder`` over an ndarray, so nothing in this file needs a codec.
"""
import json
import logging
import threading
import time
from typing import Optional, Sequence

import numpy as np
from cltl.backend.api.camera import Bounds, CameraResolution, Image
from cltl.backend.api.serialization import BackendJSONEncoder
from flask import Flask, Response
from werkzeug.serving import make_server

logger = logging.getLogger(__name__)

#: The view a system camera reports. Copied from
#: ``cltl.backend.source.cv2_source.SYSTEM_VIEW`` rather than imported — that
#: module does ``import cv2`` at the top, and importing it here would make the
#: stub drag in a codec the rest of this path is careful not to need.
SYSTEM_VIEW = Bounds(-0.55, -0.41 + np.pi / 2, 0.55, 0.41 + np.pi / 2)

#: Small enough to keep the base64 body cheap, large enough to be a real frame.
DEFAULT_RESOLUTION = CameraResolution.QQVGA

DEFAULT_COLOR = (255, 0, 0)


def frame(resolution: CameraResolution = DEFAULT_RESOLUTION,
          color: Sequence[int] = DEFAULT_COLOR) -> Image:
    """A deterministic single-colour RGB frame at *resolution*."""
    if len(color) != 3:
        raise ValueError(f"Expected an RGB triple, was {color!r}")

    pixels = np.full((resolution.height, resolution.width, 3), 0, dtype=np.uint8)
    pixels[:, :] = color

    return Image(pixels, SYSTEM_VIEW)


class StubImageServer:
    """Serves a frame on ``GET /image``, counting how often it is asked.

    Every request returns the same frame: nothing downstream of the backend
    cares what the pixels are, and a deterministic frame lets a test assert the
    bytes came back unchanged. The count is the interesting part — it is what a
    test uses to check that the camera captured at all, and that
    ``ImageCamera`` paced it at the configured rate rather than as fast as the
    socket allows.

    Binds port 0 and reports the assigned port, for the same reason as
    ``StubAudioServer``: the tier-2 stub runs on the host alongside whatever
    else is using the machine. Use as a context manager.
    """

    def __init__(self, resolution: CameraResolution = DEFAULT_RESOLUTION,
                 color: Sequence[int] = DEFAULT_COLOR,
                 host: str = "127.0.0.1", port: int = 0):
        self._resolution = resolution
        self._frame = frame(resolution, color)

        self._host = host
        self._port = port

        self._lock = threading.Lock()
        self._served = 0
        self._server = None
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "StubImageServer":
        return self.start()

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()

    def start(self) -> "StubImageServer":
        self._server = make_server(self._host, self._port, self._app(), threaded=True)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="stub-image-server", daemon=True)
        self._thread.start()
        logger.info("Stub image server listening on %s at %s",
                    self.url, self._resolution.name)

        return self

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None

    # -- introspection -----------------------------------------------------

    @property
    def url(self) -> str:
        """Base URL, without the ``/image`` path ClientImageSource appends."""
        if self._server is None:
            raise RuntimeError("stub image server is not started")
        host, port = self._server.server_address[:2]

        return f"http://{host}:{port}"

    @property
    def port(self) -> int:
        """The assigned port. Bound to 0.0.0.0 in tier 2, where the containers
        reach it as host.docker.internal, so the caller builds its own URL."""
        if self._server is None:
            raise RuntimeError("stub image server is not started")

        return self._server.server_address[1]

    @property
    def served(self) -> int:
        """How many captures have been answered so far."""
        with self._lock:
            return self._served

    @property
    def image(self) -> Image:
        """The frame every request returns, for comparison against what arrived."""
        return self._frame

    def await_capture(self, count: int = 1, timeout: float = 15.0) -> int:
        """Block until the backend's image thread has captured *count* times.

        Recording starts on a ScenarioStarted event, so anything that depends on
        the camera actually being open has to wait for the capture rather than
        for the publish that eventually causes it.
        """
        deadline = time.monotonic() + timeout
        while self.served < count:
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"the camera captured {self.served} time(s) in {timeout}s, "
                    f"expected {count}")
            time.sleep(0.05)

        return self.served

    # -- server ------------------------------------------------------------

    def _app(self) -> Flask:
        app = Flask("stub-image")
        mime = f"application/json; resolution={self._resolution.name}"
        body = json.dumps(self._frame, cls=BackendJSONEncoder)

        @app.route("/image")
        def image():
            with self._lock:
                self._served += 1

            return Response(body, mimetype=mime)

        return app
