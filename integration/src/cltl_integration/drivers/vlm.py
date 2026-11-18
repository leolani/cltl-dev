"""A stand-in llama-server: the OpenAI-compatible chat endpoint cltl-vlm-object calls.

``LlamaCppObjectDetector`` talks to llama-server through the ``openai`` client, so
all a module under test needs is ``POST /v1/chat/completions`` answering with a
chat completion whose message content is the JSON the detection schema asks for::

    {"scene": "office", "scene_description": "...",
     "objects": [{"label": "chair", "bbox_2d": [xmin, ymin, xmax, ymax]}]}

Boxes are normalised to 0-1000, as the real model reports them. The stub records
every request, so a test can assert that the image really was sent (as a
``data:image/png`` URL) and not only that something came back.

Shaped on ``drivers.camera.StubImageServer``: port 0, a context manager, started
before the topology because its URL goes into the configuration.
"""
import json
import logging
import threading
import time
from typing import List, Optional, Union

from flask import Flask, jsonify, request
from werkzeug.serving import make_server

logger = logging.getLogger(__name__)

DEFAULT_REPLY = {
    "scene": "office",
    "scene_description": "A chair and a lamp in a small office.",
    "objects": [
        {"label": "chair", "bbox_2d": [0, 0, 500, 500], "confidence": 0.9},
        {"label": "lamp", "bbox_2d": [500, 500, 1000, 1000]},
    ],
}


class StubLlamaServer:
    """Answers every chat completion with *reply*, recording what it was asked.

    *reply* is a dict (serialised as the message content) or a raw string, which
    is how a test makes the "model" answer with something that is not JSON.
    """

    def __init__(self, reply: Union[dict, str] = None, host: str = "127.0.0.1", port: int = 0):
        self.reply = DEFAULT_REPLY if reply is None else reply

        self._host = host
        self._port = port
        self._lock = threading.Lock()
        self._requests: List[dict] = []
        self._server = None
        self._thread: Optional[threading.Thread] = None

    def __enter__(self) -> "StubLlamaServer":
        return self.start()

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()

    def start(self) -> "StubLlamaServer":
        self._server = make_server(self._host, self._port, self._app(), threaded=True)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="stub-llama-server", daemon=True)
        self._thread.start()
        logger.info("Stub llama-server listening on %s", self.url)

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

    @property
    def url(self) -> str:
        """Base URL, without the ``/v1`` the openai client appends."""
        if self._server is None:
            raise RuntimeError("stub llama-server is not started")
        host, port = self._server.server_address[:2]

        return f"http://{host}:{port}"

    @property
    def requests(self) -> List[dict]:
        with self._lock:
            return list(self._requests)

    def await_request(self, count: int = 1, timeout: float = 15.0) -> List[dict]:
        deadline = time.monotonic() + timeout
        while len(self.requests) < count:
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"the stub llama-server received {len(self.requests)} request(s) "
                    f"in {timeout}s, expected {count}")
            time.sleep(0.05)

        return self.requests

    def _app(self) -> Flask:
        app = Flask("stub-llama")

        @app.route("/v1/chat/completions", methods=["POST"])
        def completions():
            with self._lock:
                self._requests.append(request.get_json())

            content = self.reply if isinstance(self.reply, str) else json.dumps(self.reply)

            return jsonify({
                "id": "chatcmpl-stub", "object": "chat.completion", "created": 0, "model": "stub",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            })

        return app
