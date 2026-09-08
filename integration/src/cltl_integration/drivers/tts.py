"""A stand-in loudspeaker: the endpoint a remote TTS system would expose.

``BackendService`` subscribes a topic worker to ``[cltl.backend.tts] topic`` and
hands every payload to ``SynchronizedTextToSpeech.say``. Where that ends up is
decided by ``[cltl.backend.text_output]``: ``console`` prints it, an empty
``remote_url`` drives a local sound card, and anything else POSTs it to a remote
speaker::

    POST <remote_url>/text
    Content-Type: text/plain
    Body:         ^startTag(<gesture>) <the reply> ^stopTag(<gesture>)

So the reply-spoken leg is testable without a sound device — which matters,
because there is no sound device in this container or in any image, and "the
machine has no speakers" was for a long time the reason this leg had no coverage
at all.

It needs some. ``SynchronizedTextToSpeech.say`` wraps its entire body in a bare
``except:`` that only logs (``cltl-backend/src/cltl/backend/impl/sync_tts.py``),
so a backend that cannot speak fails *silently*: the reply is still published,
still recorded, still visible in the chat UI, and simply never comes out of the
robot. Nothing downstream notices, and neither does a test that stops at the bus.

The gesture tags are not noise to be stripped and forgotten. ``gestures`` is
unset in the harness configuration, so ``AnimatedRemoteTextOutput`` picks a
random one per utterance and wraps the text in it; a receiver that assumed plain
text would be quietly wrong about every reply. :attr:`StubTextOutput.texts` keeps
what was actually sent and :attr:`spoken` gives the words back.
"""
import logging
import re
import threading
import time
from typing import Callable, List, Optional

from flask import Flask, request
from werkzeug.serving import make_server

logger = logging.getLogger(__name__)

#: ``^startTag(name) … ^stopTag(name)``, per AnimatedRemoteTextOutput.
GESTURE = re.compile(r"\^startTag\((?P<gesture>\w+)\)\s*(?P<text>.*?)\s*\^stopTag\(\1\)\Z",
                     re.DOTALL)


def gesture_of(text: str) -> Optional[str]:
    """The animation the backend asked for, or None if it sent bare text."""
    match = GESTURE.match(text)

    return match.group("gesture") if match else None


def words_of(text: str) -> str:
    """The utterance without its animation tags."""
    match = GESTURE.match(text)

    return match.group("text") if match else text


class StubTextOutput:
    """Records everything POSTed to ``/text``. Use as a context manager.

    Binds port 0 and reports the assigned port, for the same reason
    :class:`~cltl_integration.drivers.audio.StubAudioServer` does: the harness
    runs alongside whatever else is using the machine.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self._host = host
        self._port = port

        self._lock = threading.Lock()
        self._texts: List[str] = []
        self._received = threading.Event()
        self._server = None
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "StubTextOutput":
        return self.start()

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()

    def start(self) -> "StubTextOutput":
        self._server = make_server(self._host, self._port, self._app(), threaded=True)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="stub-text-output", daemon=True)
        self._thread.start()
        logger.info("Stub text output listening on %s", self.url)

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
        """Base URL, without the ``/text`` path AnimatedRemoteTextOutput appends."""
        if self._server is None:
            raise RuntimeError("stub text output is not started")
        host, port = self._server.server_address[:2]

        return f"http://{host}:{port}"

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("stub text output is not started")

        return self._server.server_address[1]

    @property
    def texts(self) -> List[str]:
        """Every body received, verbatim — gesture tags included."""
        with self._lock:
            return list(self._texts)

    @property
    def spoken(self) -> List[str]:
        """The same, as words."""
        return [words_of(text) for text in self.texts]

    def await_text(self, match: Optional[Callable[[str], bool]] = None,
                   timeout: float = 15.0) -> str:
        """Block until an utterance arrives (optionally one that *match* accepts).

        Raises ``AssertionError`` rather than returning, so a test that never
        hears anything says what it did hear.
        """
        deadline = time.monotonic() + timeout
        while True:
            for text in self.spoken:
                if match is None or match(text):
                    return text
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(
                    f"nothing was spoken within {timeout}s. Received: {self.texts!r}")
            self._received.wait(timeout=min(remaining, 0.05))
            self._received.clear()

    # -- server ------------------------------------------------------------

    def _app(self) -> Flask:
        app = Flask("stub-text-output")

        @app.route("/text", methods=["POST"])
        def text():
            body = request.get_data(as_text=True)
            with self._lock:
                self._texts.append(body)
            self._received.set()
            logger.info("Spoken: %r", body)

            return "", 204

        return app
