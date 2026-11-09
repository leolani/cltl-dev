"""Storage behind HTTP Basic Auth, reached the way cltl-vad and cltl-asr reach it.

A deployment can put the storage service behind a reverse proxy that demands
credentials, and hand its clients ``storage_url: http://user:pass@host/storage``.
``requests`` derives Basic Auth from the URL while *preparing* a request, which
happens on the ``cltl-storage:`` URL, before the adapter rewrites it, so the
credentials used to be dropped silently and the proxy answered 401. The fix
splits them off in ``CltlAudioAdapter``. Exercised end to end here: the real
storage service, a gate that rejects unauthenticated requests, the real client.
"""
import threading
from urllib.parse import quote

import numpy as np
import pytest
from cltl.backend.source.client_source import ClientAudioSource
from werkzeug.serving import make_server
from werkzeug.wrappers import Request, Response

from cltl_integration.drivers import audio
from cltl_integration.topology import BACKEND

AUDIO_ID = "auth-audio-signal"
USER, PASSWORD = "agent", "p@ss:w/rd"


class BasicAuthGate:
    """WSGI middleware answering 401 unless the Basic credentials match."""

    def __init__(self, app):
        self._app = app
        self.seen = []

    def __call__(self, environ, start_response):
        request = Request(environ)
        auth = request.authorization
        self.seen.append(auth.username if auth else None)
        if not auth or (auth.username, auth.password) != (USER, PASSWORD):
            return Response("unauthorized", 401)(environ, start_response)
        return self._app(environ, start_response)


@pytest.fixture
def gated_storage(inprocess):
    runner = inprocess(BACKEND)
    gate = BasicAuthGate(runner.container.storage_service.app)
    server = make_server("127.0.0.1", 0, gate, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    samples = audio.utterance()
    runner.container.audio_storage.store(AUDIO_ID, audio.frames(samples), audio.RATE)

    yield gate, f"127.0.0.1:{server.server_port}", samples

    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def _fetch(storage_url):
    with ClientAudioSource(f"cltl-storage:audio/{AUDIO_ID}", storage_url, 0, -1) as source:
        return np.concatenate(tuple(source.audio))


def test_credentials_in_the_storage_url_reach_the_server(gated_storage):
    gate, host, samples = gated_storage
    # Percent-encoded: the password contains '@', ':' and '/'.
    url = f"http://{quote(USER, safe='')}:{quote(PASSWORD, safe='')}@{host}/"

    served = _fetch(url)

    assert np.array_equal(served.ravel(), samples)
    assert set(gate.seen) == {USER}


def test_without_credentials_the_gate_refuses(gated_storage):
    _, host, _ = gated_storage

    with pytest.raises(Exception):
        _fetch(f"http://{host}/")
