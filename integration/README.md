# integration

Integration tests for the Leolani platform modules.

This component does not ship an application. It composes the platform's modules
in varying combinations and asserts that they actually work together — at the
granularity of adjacent module pairs as well as whole pipelines.

See [`docs/integration-testing-design.md`](../docs/integration-testing-design.md)
for the full design.

## Why it lives here and not in a module

Integration is the one property no single module can test. It also cannot be
tested from the source trees: this component installs the **published sdists**
from `cltl-requirements/leolani`, exactly as a deployment would, so a module
whose `setup.py` fails to package a file fails here and nowhere else.

## Running

```bash
make -C integration build          # offline venv, every module from the local registry
make -C integration test           # tier 1 — in-process, ~3 min, no Docker
make -C integration test-compose   # tier 2 — the real images, ~18 min, needs Docker
```

Tier 2 runs the images tagged `latest`, and nothing verifies they were built
from the source you are looking at. Build them first:

```bash
docker pull ghcr.io/leolani/cltl-base:latest        # published; the components layer on it
docker pull rabbitmq:3.12-management
for c in cltl-backend cltl-chat-ui cltl-context cltl-eliza \
         cltl-emissor-data cltl-vad cltl-asr; do
    make -C $c docker-ghcr-build                     # ~6s each once the base is local
done
```

`make test-compose` refuses to start if any of them is missing, rather than
letting a stack come up on last month's code and report green.

The spoken-conversation test needs an offline text-to-speech binary; without it
that one test skips:

```bash
sudo apt-get install -y espeak-ng
```

It also downloads Whisper's 139 MB model into `.cache/whisper/` on first run and
reuses it after — a bind mount rather than a compose volume, so teardown's
`down -v` does not take it.

| Target | What it runs |
|---|---|
| `build` | Creates `venv/` from `cltl-requirements/{mirror,leolani}` with `--no-index` |
| `test` | Tier 1: modules composed in-process over `SynchronousEventBus` |
| `docker-images` | Checks every tier-2 image is on the daemon, and says how to build the missing ones |
| `test-compose` | Tier 2: the real container images via Docker Compose |
| `test-all` | Both tiers |
| `test-manual` | The `manual` tests — needs a person at the keyboard |
| `demos` | Lists the scenarios `demo-<name>` can run |
| `demo-<name>` | Runs one interactively and blocks until Ctrl-C |
| `clean` | Removes `venv/`, caches, test storage and `.demo/` |

Override pytest flags with `PYTEST_FLAGS`, e.g.

```bash
make -C integration test PYTEST_FLAGS="-v --log-cli-level=DEBUG -k eliza"
```

## Markers

| Marker | Meaning |
|---|---|
| `compose` | Needs a Docker Compose stack of the real images |
| `slow` | Takes minutes — ASR inference or image pulls |
| `manual` | Needs a human to observe the result; never run unattended |

`make test` selects `not compose and not manual`; `make test-compose` selects
`compose and not manual`, which includes `slow`; `make test-all` selects
`not manual`. Nothing runs a `manual` test except `make test-manual`, and a
manual test collected without a terminal skips rather than blocking on
`input()`.

## Layout

```
src/cltl_integration/
  __main__.py           # the demo front end: run a scenario, no assertions
  modules.py            # the module registry — one source of truth
  topology.py           # Topology, config layering, deadlock validation
  serialization.py      # emissor type-var registration shared by both tiers
  images.py             # the tier-2 image freshness check
  runner/
    api.py              # Runner protocol + EventProbe
    inprocess.py        # tier 1: DI containers composed in this process
    compose.py          # tier 2: the images, under a unique compose project
    split.py            # tier 2: two compose projects — the client/server split
  drivers/
    audio.py            # synthetic PCM + a stub microphone HTTP server
    asr.py              # a recording stand-in for a speech recogniser
    bdi.py              # drive the intention/desire loop from outside
    chat.py             # ChatUI REST client
    conversation.py     # a text conversation, driven identically in both tiers
    image.py            # a synthetic PNG, and readers for signal mentions
    scenario.py         # publish ScenarioStarted without running cltl-context
compose/
  docker-compose.yml    # one file, all services; a topology selects a subset
config/
  base.config           # tier-neutral, every module disabled
  tier-inprocess.config # what differs for the in-process runner
  tier-compose.config   # what differs for the compose runner
  logging.config        # mounted into every container
  topologies/           # per-topology enable/disable, incl. the four csplit halves
tests/
  test_build_smoke.py   # the offline venv really contains the platform
  test_topology.py      # the harness itself, incl. the audio-lock guard
  test_demo_launcher.py # the demo front end, started and interrupted
  slices/               # 2-3 modules each
  pipelines/            # end to end, with intention gating on
  compose/              # tier 2, all marked `compose`
  manual/               # a person in the browser; excluded from every other target
```

### Coverage

| Test | Modules | Boundary |
|---|---|---|
| `slices/test_asr_eliza.py` | eliza | `AsrTextSignalEvent` on `text_in` -> reply on `text_out` |
| `slices/test_eliza_chatui.py` | chatui, eliza | HTTP post -> `text_in` -> `text_out` -> HTTP get |
| `slices/test_context_bdi.py` | context | init intention -> scenario -> greeting -> `initialized` -> `eliza` |
| `slices/test_backend_vad.py` | backend, vad | microphone -> audio storage -> `VadMentionEvent` |
| `slices/test_vad_asr.py` | backend, asr | `VadMentionEvent` -> audio fetched by range -> `AsrTextSignalEvent` |
| `slices/test_backend_storage.py` | backend | audio stored and served back over HTTP, by range |
| `slices/test_emissor_persistence.py` | emissor | event stream -> EMISSOR scenario on disk and over HTTP |
| `slices/test_chatui_image.py` | chatui, emissor, backend | image upload -> labelled regions -> `ImageSignalEvent` -> scenario on disk, and silence on `text_in` |
| `slices/test_intention_routing.py` | chatui, context, eliza | how intention gating is wired onto the bus |
| `pipelines/test_text_pipeline.py` | chatui, context, eliza | the whole text conversation, intention gating on |

Tier 2 (`tests/compose/`):

| Test | Asks |
|---|---|
| `test_stack.py` | do the images come up, read their mounted config, and run two at a time |
| `test_serialization.py` | do the real payload types survive marshal → RabbitMQ → unmarshal |
| `test_text_pipeline.py` | the same conversation as tier 1, across containers and a broker |
| `test_backend_vad.py` | audio recorded in one container, fetched by HTTP from another |
| `test_audio_pipeline.py` | speech in, an answer in the chat UI — six containers, Whisper, `slow` |
| `test_csplit.py` | the client/server split: two stacks, two networks, remote storage |
| `test_compose_file.py` | the compose file still says what the registry and the runners assume |

Five tests are `xfail(strict=True)` against defects in the modules, so that
fixing one turns the suite red and forces the marker out. Each carries the file,
the line and the fix in its `reason`.

### What the two tiers share

The `Conversation` driver, and nothing else by accident. Both runners expose an
event bus, a probe and `url(module)`, which is enough to drive a pipeline
identically — but an assertion means different things on either side. In tier 1
a subscriber is handed the object the publisher created; in tier 2 that object
has been marshalled to JSON, routed by a topic exchange and rebuilt. So the
serialization tests exist only in tier 2, and the tier-1 tests are the ones fast
enough to run on every build.

That split has already paid for itself: `tests/slices/test_intention_routing.py`
and `tests/compose/test_text_pipeline.py` document a defect that is *correct in
process and wrong in containers*, because an in-process worker's one-slot buffer
usually swallows a duplicated event that AMQP delivers far enough apart to be
processed twice.

## The client/server split

The platform is meant to be deployable with the person-facing half on a robot and
the expensive half on a server. `tests/compose/test_csplit.py` runs that
deployment for real — two compose projects over the same `docker-compose.yml`,
on two bridge networks:

```
client project                         server project
---------------------------            ---------------------------
cltl-backend  (devices,                cltl-backend  (storage_main.py)
               remote storage)  --->   cltl-emissor-data
cltl-context  (BDI handshake)   --->   cltl-eliza
cltl-chat-ui                           rabbitmq
```

Nothing resolves across the two networks: `rabbitmq` and `backend` are names on
the server's network only. The client reaches them the way a client on another
machine would — through `host.docker.internal`, at the ports the server project
published. `SplitRunner` starts the server, reads its ephemeral ports, and hands
them to the client stack as `CLTL_CONTAINER_AMQP_URL` and
`CLTL_CONTAINER_STORAGE_URL`.

What only this can cover:

- The BDI handshake completes with **cltl-context and cltl-eliza in different
  deployments**, so every intention and desire is routed by a broker the client
  does not own.
- **The client stores no audio.** `[cltl.backend] audio_storage: remote` PUTs
  every recording to the server, and the server's cltl-vad and cltl-asr read it
  back from their own side of that store. A monolith gets that URL right by
  accident, because its local fallback happens to point at the same disk.
- **The server runs `src/storage_main.py`** — `StorageContainer` rather than
  `BackendContainer`. Nothing else in the suite starts that entry point.

A `Deployment` is two `Topology` objects rather than one with more modules in
it, and it validates the pairing: the server half must carry the storage
endpoint, and no module except cltl-backend may appear on both halves — two
instances of one module bind their queues to the same routing key, so RabbitMQ
shares the events out between them instead of delivering to both.

Only one of the two stacks may be a `ComposeRunner`. The test process's
configuration lives in a class attribute of `LocalConfigurationContainer`, so a
second `load_configuration` replaces the first rather than adding to it. The
server owns it, because the server owns the broker the probe joins; the client
half is a plain `ComposeStack`, which brings containers up and otherwise keeps
out of the way.

## Writing a slice test

A slice names the modules it covers, so a failure localises the blame. Ask for a
topology from the `inprocess` fixture, subscribe the probe to the topics you care
about, inject a stimulus, and assert on what comes back:

```python
@pytest.fixture
def eliza(inprocess):
    runner = inprocess(ELIZA)
    runner.probe.subscribe("cltl.topic.text_out")
    return runner


def test_transcript_gets_a_reply(eliza):
    eliza.event_bus.publish("cltl.topic.text_in", Event.for_payload(asr_event("hello")))

    reply = eliza.probe.await_event("cltl.topic.text_out")

    assert reply.payload.signal.text
```

Use the *real* payload type the upstream module publishes (here
`AsrTextSignalEvent` from `cltl_service.asr.schema`), not a stand-in — that is
what makes it a contract test rather than a unit test.

Adding a scenario means adding one `Topology` in `topology.py` plus an overlay in
`config/topologies/`. The same object will drive the tier-2 test and the demo.

When a module cannot run as shipped — every ASR backend needs torch or a cloud
account — replace just that piece with `container_overrides`, which land in the
synthesised container's own namespace and therefore beat every base:

```python
asr = RecordingASR()
runner = inprocess(VAD_ASR, container_overrides=asr_override(asr))
```

The real `AsrService` still reads its topics, buffering and audio loader from
configuration; only `speech_to_text` is a double. Values the configuration
interpolates go in through `environment=`, which must be set before the config is
read — that is how the stub microphone's port reaches `[cltl.backend]
server_audio_url`.

## Demos

The third front end onto the same `Topology` objects, after a tier-1 test and a
tier-2 test — the one with no assertions in it:

```bash
make demos                      # what there is to run
make demo-text-pipeline         # in this process, ~2s to start
make demo-csplit DEMO_FLAGS='--tier compose'
make demo-audio_pipeline DEMO_FLAGS='--tier compose --say "I feel very anxious"'
```

It starts the modules, opens a scenario, prints the URLs it discovered — the
chat page among them — and blocks until Ctrl-C. Underscores and hyphens both
work, so `demo-text-pipeline` finds `text_pipeline`.

Opening the scenario is the part that is easy to forget and impossible to
notice: `ChatUiService` will not render an utterance without one and
`BackendService` will not record without one. With cltl-context in the scenario
the launcher publishes the `init` intention and lets the BDI loop open it,
exactly as `app/py-app/app.py` does at startup; without a BDI loop it publishes
`ScenarioStarted` itself. `tests/test_demo_launcher.py` covers both paths by
starting the launcher as a subprocess, holding a conversation through it, and
interrupting it — because a demo that comes up and answers nothing looks exactly
like a demo that works.

A topology with a microphone gets a stub one, started before the runner because
`[cltl.backend] server_audio_url` is read when the backend is constructed.
`--say TEXT` fills it with espeak-ng; without `--say`, or without espeak-ng, it
hears the synthetic tone the automatic tests use.

## Manual tests

```bash
make test-manual
```

Three tests, one question, three deployments: hold a conversation in the browser
against the topology running in this process, against the container images, and
across the client/server split. They exist because the automatic suite drives
cltl-chat-ui's REST endpoints and never loads its page — the HTML, the
JavaScript and the static assets inside the image are covered by nothing else.

Each one cross-checks the answer it is given against the utterances the service
recorded, so a distracted `y` cannot make a broken chat UI pass.

## Notes

- **Docker comes from the `docker-in-docker` devcontainer feature.** The daemon
  runs inside this container, which is why compose bind mounts resolve against
  container paths and the stub microphone is reachable at
  `host.docker.internal`. Sharing the host's daemon instead would resolve
  `${CLTL_CONFIG_DIR}` against a path that does not exist there, and Docker
  answers a missing bind source by creating an empty directory rather than
  failing — every module would come up on its built-in legacy defaults.
- **A tier-2 run leaves nothing behind.** Unique project name, no
  `container_name`, no fixed ports, no bind-mounted broker state, and
  `down -v` on teardown. Two stacks can run at once; there is a test for it.
  Container logs are written to the run's `docker.log` before teardown, because
  they are the only account of a tier-2 failure.
- This component deliberately has **no `setup.py`** and does not include
  `util/make/makefile.py.base.mk`. That file's `build` target chains to
  `py-install`, which would copy an sdist into `cltl-requirements/leolani` — the
  registry every component and Dockerfile installs from. A test harness must
  never publish itself there.
- It references the shared build system at `../util/make/` rather than carrying
  a nested `util/` submodule copy, so there is nothing extra to keep in sync.
- The runner creates the storage directories itself. `CachedAudioStorage`
  appears to do so but calls `os.makedirs` on the *parent* of its storage path,
  so a fresh storage root loses every recording to a libsndfile "System error"
  that `BackendService` catches and retries forever. See
  `tests/slices/test_backend_storage.py`; `app/py-app` has the same problem.
- Teardown costs about a second per topic worker. `TopicWorker.stop()` clears a
  flag but does not interrupt the worker's blocking `Queue.get`, so each one
  lingers until its timeout expires — three seconds for `InitService`, which is
  `scheduled=3`. That is most of the suite's wall-clock time.
- Tier 1 binds a **fixed** port (default 8000), not an ephemeral one.
  `[cltl.backend] storage_url` is read when services are constructed, and the VAD
  and ASR audio loaders resolve `cltl-storage:` URLs over real HTTP against it, so
  the address has to be known before the container exists. In-process runs are
  serialized anyway: the parsed configuration lives in a class attribute.
