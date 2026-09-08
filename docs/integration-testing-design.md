# Design: `integration/` — an integration-test component for the Leolani platform

Status: phases 1-5 implemented; prior art reviewed and ported · Date: 2026-09-08

## Context

`/workspaces/cltl-dev` is the development environment for the modular Leolani
platform: ten component submodules (`emissor`, `cltl-combot`, `cltl-backend`,
`cltl-vad`, `cltl-asr`, `cltl-eliza`, `cltl-chat-ui`, `cltl-emissor-data`,
`cltl-context`, `cltl-monitoring`), a local package registry (`cltl-requirements/`)
and a shared build system (`util/`).

Today the only thing that exercises the platform *as a whole* is `app/` — an
example ELIZA application. That is the wrong artefact for a development
environment: it is a product, not a test. Its four docker-compose integration
tests (`app/docker-app/tests/integration/`) only ever assert on the whole stack
through the chat UI, so a break anywhere between the microphone and the browser
produces the same single red test. Meanwhile the module-level suites have rotted:
`cltl-asr/tests/test_asr_service.py`, `cltl-eliza/tests/test_service.py` and
`cltl-chat-ui/tests/test_service.py` all call constructors that no longer exist,
and `cltl-context` — which owns the entire BDI/init handshake every end-to-end
test depends on — has no tests at all.

Worse, nobody would have noticed: `makefile.py.base.mk`'s `test:` recipe is
`source venv/bin/activate; python -m unittest; deactivate`. Chained with `;`, the
recipe's exit status is `deactivate`'s, which is always 0. **Every component's
unit tests currently report green regardless of outcome.**

**Goal:** replace the example app with a first-class *integration-test component*
that verifies the modules integrate with one another — at the granularity of
adjacent module pairs as well as whole pipelines, both in-process and as real
containers, with the same scenario definitions doubling as runnable demos.

It lives next to `app/` and is independent of it. `app/` is not touched by this
work; retiring it is a later, separate decision.

### Non-goals

- Fixing the stale module-level unit tests (separate, per-submodule work).
- ~~`cltl-monitoring` is out of scope~~ — **resolved.** It was excluded because
  it had no DI container, imported `cltl.object_recognition` and `cltl.friends`
  unconditionally at module level (neither exists in this workspace), and its
  `config/default.config` had no `[cltl.monitoring]` section. All three were
  fixed when the chat UI gained its Monitoring tab; the module is now in the
  registry, in `compose/docker-compose.yml`, and covered by the
  `chatui_monitoring` topology.
- ~~Retiring `app/`~~ — **done, separately.** Once phases 1-6 landed, `app/`
  was removed entirely: the example application, its two test harnesses, the
  `app/util` gitlink, and the root `Dockerfile`/`docker-compose.yml` that built
  and ran the `eliza-app` image. Nothing depended on it — the two-machine
  deployment stacks that replaced it run from the published
  `ghcr.io/leolani/cltl-*` images and never referenced it.

---

## Design

Three concepts, in order of dependency.

### 1. Module registry — one source of truth

Today the same dependency graph is transcribed by hand in four places:
the MRO in `app/py-app/app.py:68`, the services in
`app/docker-app/docker-compose.yml`, `app/requirements.txt`, and each
component's `project_dependencies`. They have already drifted (e.g. `cltl-asr`'s
makefile depends on `cltl-emissor-data`, its `setup.py` does not).

`src/cltl_integration/modules.py` declares an **ordered** tuple of `Module`
records:

```python
Module(
    key="vad",
    container="cltl_service.vad.container:VADContainer",
    image="ghcr.io/leolani/cltl-vad",
    requirement="cltl.vad[impl,service]",
    http=False,
)
```

Everything downstream — the in-process MRO, the compose service selection, the
venv requirements — derives from this one list.

Two ordering facts the registry must encode, both verified:

- **The event-bus override must be the first base**, not merely "somewhere in the
  MRO". `KombuEventBusContainer.event_bus` is a plain (non-singleton) property
  (`cltl-combot/src/cltl/combot/infra/event/kombu.py:59`), so anything later in
  the MRO wins by default. Build the type as
  `type("ApplicationContainer", (InfraOverride, *selected), {})`.
- **Start order is the reverse of the bases tuple.** Every container calls
  `super().start()` *before* starting its own services (`cltl-eliza/src/cltl_service/eliza/container.py:26`,
  `cltl-vad/.../container.py:29`, `cltl-backend/.../backend_container.py:98`). So
  `app.py`'s `(Infra, Eliza, Context, ChatUI, ASR, VAD, Emissor, Backend)` actually
  starts Backend → Emissor → VAD → ASR → ChatUI → Context → Eliza. Nothing is
  order-sensitive today, but the registry must not be written in topic-flow order
  under the illusion that that is start order.

`ASRContainer`'s docstring claims it "Requires `EmissorStorageContainer` in the
MRO to provide `emissor_data_client`". It does not — `AsrService.from_config`
takes no client, and `emissor_data_client` has **zero call sites repo-wide**. Do
not encode that dependency.

### 2. Topology — a named module subset + config overlay

```python
VAD_ASR = Topology(
    name="vad_asr",
    modules=("backend", "vad", "asr", "emissor"),
    overlay="config/topologies/vad_asr.config",
)
```

A Topology is the shared artefact: the same object is consumed by an automatic
test, by a container test, and by the demo launcher. Adding a scenario means
adding one Topology, not three parallel definitions.

`Topology.__post_init__` **validates** what it can decide from the declaration
alone: an unknown module, a duplicate, a `Deployment` whose halves overlap. It
does not validate configuration — the one rule that was written for it, the
audio-lock rule, turned out not to be a rule at all (see the risk table).

### 3. Runners — two realisations, one control surface

```python
class Runner(Protocol):
    def url(self, module_key: str) -> str: ...   # host-reachable base URL
    @property
    def probe(self) -> EventProbe: ...           # subscribe / record / await events
    @property
    def storage_path(self) -> Path: ...
```

**`InProcessRunner`** (tier 1 — seconds, no Docker): synthesises the container
type as above, loads the merged config, enters the container, and mounts every
`http=True` module's `.app` under a `DispatcherMiddleware` served by
`werkzeug.serving.make_server` on a thread — the pattern already proven in
`cltl-backend/tests/test_storage_client.py`. Event bus is `SynchronousEventBus`,
so the probe subscribes directly.

The tier-1 server binds a **fixed, configurable port (default 8000)**, not an
ephemeral one, and it must be running before the container is constructed.
`VadService.from_config` builds a `ClientAudioSource.from_config(config_manager, url, …)`
(`cltl-vad/src/cltl_service/vad/service.py:30`) — and so does `AsrService` — which
resolves `cltl-storage:` URLs over **real HTTP** against `[cltl.backend] storage_url`.
There is no in-memory shortcut: even tier 1 runs a real socket. Since the URL is
baked into config at construction time, port-0 discovery is not available.
(Tier-1 runs are serialized anyway — see the static-config note below.)

**`ComposeRunner`** (tier 2 — minutes, Docker): brings the stack up under a
unique `--project-name` with ephemeral host ports, then discovers them with
`docker compose port`.

The test process joins the RabbitMQ bus to probe it — but **not** by constructing
`KombuEventBus('cltl-json', config_manager)` directly. The `cltl-json` serializer
is registered as a side effect of `KombuEventBusContainer.kombu_event_bus`
(`kombu.py:69`), so a bare construction fails. Instantiate a throwaway
`type("ProbeContainer", (InfraOverride,), {})` and take its `.event_bus`.

**The probe needs a readiness handshake in tier 2.** `KombuEventBus.subscribe`
declares an `exclusive`, `auto_delete` queue and starts a `ConsumerMixin` thread,
returning *before* the queue is bound. Events published in the first ~100 ms are
silently dropped. `EventProbe.subscribe()` must publish a sentinel on the probed
topic and block until it observes it. Tier 1's `SynchronousEventBus.subscribe` is
synchronous and lossless, so this is tier-2-only plumbing behind a shared API.

### What the two tiers actually share

The control surface is shared. Test *bodies* are shared only where the stimulus is
an event publish or an HTTP call. They are not shared where the tiers genuinely
differ in semantics:

| Divergence | Consequence |
|---|---|
| `SynchronousEventBus` hands the live object to handlers; Kombu round-trips through `marshal`/`unmarshal` | A payload that fails to unmarshal passes tier 1 and fails tier 2. Assertions on payload *fields* are two different tests |
| `ThreadedResourceManager` is a process-wide singleton | Tier 1: one registry for all modules. Tier 2: one **per container**, so the mic/TTS audio lock only actually synchronises in tier 1 |
| Tier 1 runs probe handlers on the publishing thread | A slow handler stalls the pipeline in tier 1 only |

So: tier 1 = fast wiring, config and packaging checks. Tier 2 = serialization,
images, deployment. A deliberately small, named set of tests runs on both.

**Tier 1 catches packaging bugs** that no current test can see, because
`integration/venv` installs the *published sdists* from `cltl-requirements/leolani`,
not the source trees. The `cltl-context` bug below is exactly that class.

---

## Directory layout

```
integration/
  makefile                       # component makefile, wired into root project_components
  VERSION
  requirements.txt               # every cltl.* module + test deps
  pytest.ini                     # markers: compose, slow, manual; global timeout
  README.md
  src/cltl_integration/
    modules.py                   # the Module registry (§1)
    topology.py                  # Topology + validation + the topology registry (§2)
    runner/
      api.py                     # Runner protocol, EventProbe
      inprocess.py               # InProcessRunner + reset_process_state()
      compose.py                 # ComposeRunner (port discovery, log capture)
    drivers/
      chat.py                    # ChatClient — moved from app/docker-app/.../helpers/
      audio.py                   # stub audio server — moved from app/docker-app/.../audio/
    __main__.py                  # `python -m cltl_integration <topology>` (demo launcher)
  config/
    base.config                  # topics, BDI model, module params — tier-neutral
    tier-inprocess.config        # $PLACEHOLDER values for tier 1
    tier-compose.config          # $PLACEHOLDER values for tier 2
    topologies/<name>.config     # per-topology enable/disable
    logging.config
  compose/
    docker-compose.yml           # one file, all services; select with `up <svc> ...`
  tests/
    conftest.py
    slices/                      # 2-3 modules each
    pipelines/                   # end-to-end
```

`src/cltl_integration/` is deliberately **not** under the `cltl.` / `cltl_service.`
namespace: it is a harness, not a platform component.

---

## Test taxonomy

### Slices (`tests/slices/`)

| Test | Modules | Stimulus → assertion | Tiers |
|---|---|---|---|
| `test_asr_eliza.py` | eliza | publish `TextSignalEvent` on `cltl.topic.text_in` → response on `cltl.topic.text_out` | both, shared body |
| `test_context_bdi.py` | context | `init` IntentionEvent → `ScenarioStarted` on `cltl.topic.scenario`; `initialized` desire → `eliza` intention | both, shared body |
| `test_eliza_chatui.py` | chatui, eliza | HTTP POST to ChatUI → reply via HTTP GET | both, via `runner.url("chatui")` |
| `test_emissor_persistence.py` | emissor + producers | events on subscribed topics → scenario written under storage | both, via `runner.storage_path` |
| `test_vad_asr.py` | vad, asr, backend(storage) | `VadMentionEvent` → `AsrTextSignalEvent` on `cltl.topic.text_in` | both; needs the storage HTTP server in tier 1 too; stub ASR in tier 1, Whisper in tier 2 |
| `test_backend_vad.py` | backend, vad | audio → `cltl.topic.microphone` → `VadMentionEvent` | both, tier-specific stimulus fixture: tier 1 injects an `AudioSource` (the `cltl-vad/tests_integration/test_vad_service.py` pattern), tier 2 runs the stub HTTP audio server |

Each test names the modules it covers, so a failure localises the blame — the
property the current whole-stack suite lacks.

Only `backend`(storage), `chat-ui` and `emissor-data` expose a Flask `.app`;
`vad`, `asr` and `eliza` return `None`. The `http` flag records this.

### Pipelines (`tests/pipelines/`)

- `test_text_pipeline.py` — chatui → context/BDI → eliza → chatui (both tiers)
- `test_audio_pipeline.py` — stub audio → backend → vad → asr(whisper) → eliza → chatui (tier 2, `slow`)
- `test_csplit.py` — client/server split deployment (tier 2 only; a split has no
  in-process meaning). Two compose projects on two networks: cltl-context and
  cltl-chat-ui on the client, cltl-eliza and storage on the server, the client's
  audio stored remotely. `slow` for the spoken half.

Ported from `app/docker-app/tests/integration/test_*.py`, which stayed in place
until the new suite was green and were removed with `app/`.

### Demos

The same Topology objects, launched without assertions:

```bash
make demo-text-chat     # python -m cltl_integration text_chat --tier compose
```

Prints the discovered URLs and blocks until Ctrl-C. Kept deliberately thin (a
~20-line `__main__.py` over the existing runners) so it cannot rot into a second
product.

---

## Build integration

### `integration/makefile`

`project_dependencies` = every component; includes `makefile.base.mk`,
`makefile.component.mk`, `makefile.py.base.mk`, `makefile.git.mk`.

| Target | Runs |
|---|---|
| `test` | tier 1 — `pytest -m "not compose"` |
| `test-compose` | tier 2 — `pytest -m compose` (preceded by an image-freshness check) |
| `test-all` | both |
| `demo-<name>` | `python -m cltl_integration <name>` |

Three details the `app/makefile` precedent gets wrong and this one must not:

- **`build: venv` does not replace `build: py-install`.** Neither rule carries a
  recipe, so make *accumulates* prerequisites and the component still runs
  `setup.py sdist` and copies the tarball into `cltl-requirements/leolani` — the
  registry every Dockerfile installs from (that is why `cltl.eliza-app-*.tar.gz`
  is in there today). This component is a consumer, not a producer: give it **no
  `setup.py`**, set `sources :=` explicitly (the default
  `$(shell find $(project_root)/$(project_name)/src/*)` needs `src/` to exist),
  and add the harness to the venv with a `pip install -e .`-free path — the tests
  run with `rootdir=integration` and import `cltl_integration` from `src/` via
  `pythonpath = src` in `pytest.ini`.
- **`venv:` chains with `;` instead of `&&`,** so a failed `pip install` still
  reports success and `touch venv` marks the target done. Override `venv` with an
  `&&`-chained recipe.
- **`test:` has the same bug** (`makefile.py.base.mk:44`). Fix it in `cltl-build`
  for every component, not just here.

Add `integration` to `project_components` in the root `makefile`, and add a
`test:` dispatch target to `util/make/makefile.parent.mk` — there is none today,
so `make test` at the root fails outright. `util/` is a shared submodule, so that
change lands in `cltl-build` and propagates via `make update-build`.

### Test dependencies via `cltl-requirements`

`pytest` is absent from `cltl-requirements/requirements.txt`, so it is not in
`mirror/`, so no `--no-index` venv can install it. This is a live hole, not just
a future one: `app/venv` has no pytest either, so `make -C app test-integration-text`
cannot run from a clean offline build.

Verified mirror state: `requests`, `PyYAML`, `gTTS`, `pydub`, `exceptiongroup`,
`tomli`, `packaging`, `attrs` are **already present**; `pytest`, `pluggy`,
`iniconfig` are **missing**. So the new spec is small:

```
# cltl-requirements/requirements.test.txt
pytest
pluggy
iniconfig
pytest-timeout
```

with a rule mirroring the existing `requirements.lock` one, downloading into the
**same** `mirror/` so `--find-links` needs no change and `Dockerfile.base`
(which installs `requirements.base.txt`) is untouched:

```makefile
requirements.test.lock: requirements.test.txt
	@pip download --requirement requirements.test.txt -d mirror \
		| grep Collecting | cut -f 2 -d ' ' > requirements.test.lock

build: $(mirror_lock) requirements.test.lock $(artifacts)
```

Add `requirements.test.lock` to `cltl-requirements/.gitignore`.

`pytest-timeout` is mandatory, not optional — see the unbounded-`wait()` risk below.

---

## Configuration strategy

Three layers plus environment interpolation.

1. **`config/base.config`** — tier-neutral: topic names, BDI model, VAD/ASR
   parameters, chat-ui settings. Lift from `app/docker-app/config/default.config`.
   Note the per-module `config/default.config` files in each submodule use
   *legacy* topic names (`cltl.mic`, `cltl.chat.utterance`) and must never be
   used — a container that falls back to them comes up **healthy** and wired to
   nothing.
2. **`config/tier-inprocess.config` / `config/tier-compose.config`** — only the
   keys that genuinely differ by tier:

   | key | tier 1 | tier 2 |
   |---|---|---|
   | `[cltl.event] implementation` | `internal` | `kombu` |
   | `[cltl.event.kombu] server` | — | `amqp://…@rabbitmq:5672/` |
   | `[cltl.backend] storage_url`, `[cltl.backend.remote_storage] storage_url` | `http://127.0.0.1:$PORT/storage/` | `http://eliza-backend:8000/storage/` |
   | `[cltl.backend] server_audio_url` | `http://127.0.0.1:$STUB_PORT` | `http://host.docker.internal:$STUB_PORT` |
   | storage paths (`audio_storage_path`, `[cltl.emissor-data] path`, `[cltl.event_log] log_dir`) | absolute host path | `<workdir>/storage/…` |

   Rather than templating these files, **reuse `EnvInterpolation`**
   (`cltl-combot/src/cltl/combot/infra/config/local.py:42`), which already
   expands `$VAR`/`${VAR}` from `os.environ` at read time. The runner sets the
   variables in its own env (tier 2: via compose `environment:`) and the config
   files stay static. Set them all — `before_read` warns on every unexpanded `$`.

3. **`config/topologies/<name>.config`** — enables exactly what this topology
   needs (`[cltl.asr] implementation:` empty vs `whisper`, `[cltl.backend.mic] topic:`
   empty vs set). The existing `custom.config`/`audio.config` pattern.

Tier 1 calls `load_configuration(base, [tier, topology])` — `ConfigParser.read()`
applies later files last. Tier 2 must mount `base.config` as `config/default.config`
and a **pre-merged** tier+topology file as `config/custom.config`, because
`ADDITIONAL_CONFIGS` is a fixed two-element list. Merge with `ConfigParser`, not
by concatenating text.

Use `[cltl.context]` and the `cltl-context` `ContextService` throughout. The
app-local `ContextService` reads `eliza.context` while `app/py-app/config/default.config`
declares `[app.context]` — a live `NoSectionError`. Standardising on the module's
own service sidesteps it and matches where the project is heading.

### Process state that must be reset between tier-1 topologies

`DIContainer._reset()` is necessary but **not** sufficient. It must be called on
`DIContainer` itself — `_reset` is a `@classmethod` doing `cls._singletons = dict()`,
so calling it on a subclass shadows the attribute instead of clearing the one
`@singleton` writes to. A `reset_process_state()` helper must additionally:

- Reload config *after* the reset. `LocalConfigurationContainer.__config`
  (`local.py:76`) is a name-mangled class attribute, not a singleton, and survives
  `_reset()`. Order: reset → `load_configuration()` → instantiate.
- **Snapshot and restore `os.environ`.** `load_configuration` permanently copies
  the `[environment]` section into `os.environ` (`local.py:66`), and
  `EnvInterpolation` substitutes `os.environ` into every value on read. Topology
  N leaks into topology N+1's interpolation.
- Pass **absolute** config paths and a **freshly built list**. The defaults are
  CWD-relative (`config/default.config`, `["config/custom.config", "config/credentials.config"]`),
  and `K8LocalConfigurationContainer.load_configuration` does
  `configs += [k8_config_file]` on the caller's list (`k8config.py:20`).
- Assert `threading.enumerate()` returns to baseline. `VadService.stop()` shuts
  its `ThreadPoolExecutor` down with `wait=False` (`cltl-vad/.../service.py:71`),
  so detect tasks survive into the next test and keep hitting the *old* HTTP port.
  `TopicWorker.await_stop()` waits 10 s and returns silently on timeout.
- Configure logging once, in `conftest.py`. `fileConfig(..., disable_existing_loggers=False)`
  re-adds handlers on every call.

---

## Prerequisites

**1. `cltl-context` does not package its container — RESOLVED in phase 3.**
`setup.py:15` uses `find_namespace_packages(include=['cltl.*', 'cltl_service.*'],
where='src')`, which matches sub-packages but not the namespace root, so
`src/cltl_service/context_container.py` was absent from
`cltl-requirements/leolani/cltl.context-*.tar.gz` and no venv built from the
registry could import `ContextComponentsContainer`. `app/py-app/app.py` had been
unable to start for the same reason.

Fixed by moving the file to `src/cltl_service/context/container.py` — the
convention every other component follows — and updating its two importers
(`cltl-context/src/main.py`, `app/py-app/app.py`). Every component's `setup.py`
carries the same include list, so the next module placed at the namespace root
hits this identically; `tests/test_build_smoke.py` keeps the strict-xfail
machinery in place for it.

**2. Docker — RESOLVED by a devcontainer rebuild.** The container as originally
built had no `docker` binary and no `/var/run/docker.sock`, and could not
acquire one in place: its capability bounding set had neither `CAP_SYS_ADMIN`
nor `CAP_NET_ADMIN`, `/sys/fs/cgroup` was mounted read-only, and
`unshare --user` returned `EPERM` even under `sudo`. That ruled out a daemon
inside the container (rootful or rootless), ruled out Podman, and — with no
socket bind-mounted and no daemon reachable over TCP — ruled out talking to the
host's. Which is why tier 1 was built first, even though tier 2 has more prior
art to reuse.

`.devcontainer/devcontainer.json` now declares
`ghcr.io/devcontainers/features/docker-in-docker:2`. After a rebuild, tier 2,
`make -C cltl-requirements install` and the existing `app/docker-app` suite all
run.

docker-in-docker rather than docker-outside-of-docker, deliberately: this
workspace is bind-mounted from the host, so a shared host daemon would resolve
compose bind mounts such as `${CLTL_CONFIG_DIR}` against host paths that do not
exist there — and Docker answers a missing bind source by creating an empty
directory rather than failing, so every module would start healthy on the legacy
topic names baked into its own image. The feature declares `"privileged": true`,
which the host must permit.

Two things a rebuild costs, both of which the harness now reports rather than
hides: the system audio headers `pyaudio` compiles against
(`portaudio19-dev libsndfile1 libasound2-dev`, per CLAUDE.md) and every `venv/`,
whose interpreter symlinks pointed into a pyenv that no longer exists.

**3. `mirror/` wheels are `aarch64`-only.** 38 binary wheels are
`manylinux*_aarch64`; the mirror is architecture-locked to this machine.
Document, do not fix here.

---

## Implementation phases

**Phase 1 — skeleton that proves the build.** `integration/` with makefile,
`VERSION`, `requirements.txt`, `pytest.ini`; `requirements.test.txt` +
`requirements.test.lock` rule in `cltl-requirements`; root `makefile` and
`makefile.parent.mk` wiring; fix the `test:` and `venv:` `;`-vs-`&&` recipes in
`cltl-build`. One trivial test. Success = `make -C integration test` runs pytest
from an offline venv **and a deliberately failing test turns the target red**.

**Phase 2 — tier 1 runner.** `modules.py`, `topology.py`, `runner/api.py`,
`runner/inprocess.py`, `reset_process_state()`, the config layers. Two slice
tests (`test_asr_eliza`, `test_eliza_chatui`) — neither touches `cltl-context`,
so this phase proves the runner without depending on the prerequisite.

**Phase 3 — remaining slices, tier 1. Done.** Prerequisite 1 cleared, then
context/BDI, backend→vad, vad→asr, backend storage, emissor persistence and the
text pipeline: 80 tests, ~2.5 minutes, no Docker.

Two harness capabilities arrived with it, both needed by more than one slice:
`container_overrides` places a test double in the synthesised container's own
namespace (used to run the real `AsrService` with a stub recogniser, since every
shipped ASR backend needs torch or a cloud account), and `environment` sets the
variables the tier config interpolates before it is read (used to hand the stub
microphone's ephemeral port to `[cltl.backend] server_audio_url`).

Three defects surfaced that no existing test could see. Each is recorded as an
`xfail(strict=True)` naming the file, the line and the fix, so repairing one
turns the suite red:

* **`CachedAudioStorage` never creates its own directory.** `cached_storage.py:44`
  calls `os.makedirs(os.path.dirname(self._storage_path))` — the parent of the
  directory it writes into. `CachedImageStorage` repeats it at line 176. Nothing
  creates `app/py-app/storage/audio` either, so a fresh checkout of the
  application persists no audio at all: every write fails with a bare libsndfile
  "System error", which `BackendService`'s recording thread catches, logs and
  retries forever while the live pipeline keeps working off the in-memory cache.
* **`KeywordService` can never activate.** `start()` hardcodes
  `intentions=["chat"]` and `from_config` ignores the configured
  `[cltl.keyword] intentions`, while the BDI model only ever produces `init` and
  `eliza`. The goodbye keyword — the only way for a user to end a conversation —
  is dead code.
* **cltl-asr annotates the speaker differently from everything else.**
  `asr/schema.py:16` passes the `ConversationalAgent.SPEAKER` enum member where
  `TextSignalEvent.for_speaker`/`for_agent` pass `.name`. The same fact then has
  three representations: `'SPEAKER'` from every other producer, the enum object
  from cltl-asr in tier 1, and `'speaker'` in tier 2, where marshal/unmarshal
  lowercases it. Exactly the tier-1/tier-2 divergence this design predicted,
  except that it does not fail — it silently differs.

Two further observations, reported but not filed as tests:

* **Shutdown costs ~1 second per topic worker.** `TopicWorker.stop()` clears
  `_running` but does not interrupt the worker's blocking `Queue.get`, so each
  worker lingers until its timeout — three seconds for `InitService`, which is
  `scheduled=3`. This is most of the suite's wall-clock time and would be a
  one-line fix (put a sentinel on the buffer).
* **A narrow startup race in the init handshake.** `TopicWorker._check_intention`
  activates gated workers on the *publishing* thread, but `ContextService` reacts
  to the same event on its own thread. If it publishes `ScenarioStarted` before
  the publish loop reaches `InitService`, that event is dropped and the handshake
  stalls with no error. `app/py-app/app.py`'s `time.sleep(1)` before publishing
  the init intention does not close this window. Not observed in ~30 runs.

**Phase 4 — tier 2. Done.** `runner/compose.py` over a single hand-written
`compose/docker-compose.yml`; a topology selects its services on the command
line. 38 tests, ~18 minutes, behind `make test-compose`.

The audio pipeline — speech in, an answer in the chat UI, through six containers
and Whisper — replaces `app/docker-app`'s gTTS stub with **espeak-ng**. Offline,
identical samples every run, no mp3 decoder, and Whisper transcribes it
verbatim; the prior art's version needs the network and never produces quite the
same audio twice. Whisper's 139 MB model is bind-mounted from `.cache/whisper/`
rather than a compose volume, because teardown runs `down -v`.

**The client/server split. Done, and it needed a new type rather than a new
topology.** `Deployment` is two `Topology` objects — the near half and the far
half — and `runner/split.py` runs them as two compose projects over the *same*
`docker-compose.yml`, differing only in which services they name and in two
environment variables. The design assumed a second compose file and a shared
network; neither is needed, and the shared network would have been wrong.

Four things the implementation settled that the design left open:

* **Two networks, not one.** Each compose project gets its own bridge network, so
  `rabbitmq` and `backend` resolve on the server's side only. The client reaches
  them through `host.docker.internal` at the ports the server project published —
  which is the deployment being rehearsed, a client on another machine. Putting
  both halves on one network would have let DNS paper over exactly the
  configuration this is meant to test.
* **`--no-deps` for the client.** Every service in the compose file declares
  `depends_on: rabbitmq`, so `up client-services` would silently start a second
  broker on the client's network and the split would quietly become two
  disconnected systems.
* **Only one half may attach to this process.** `LocalConfigurationContainer`
  keeps the parsed configuration in a class attribute, so a second
  `load_configuration` replaces the first rather than adding to it. That forced
  `ComposeRunner` apart into a `ComposeStack` (containers, ports, logs) and a
  `ComposeRunner` (that plus the process's configuration, event bus and probe).
  The server is the runner because the server owns the broker; the client is a
  bare stack.
* **The server runs `src/storage_main.py`.** `StorageContainer` rather than
  `BackendContainer` — a different DI container and a different entry point, and
  the only place in the suite that one runs. `compose/docker-compose.yml` takes
  it from `${CLTL_BACKEND_MAIN:-src/main.py}`.

That last point produced the one bug in the harness itself worth recording, and
it is the same class as the `CLTL_AMQP_URL` leak the compose file already guards
against: `ComposeStack._compose_env` starts from `os.environ`, and the server
runner puts its own `CLTL_BACKEND_MAIN` there, so the client stack inherited the
server's entry point and came up with **no microphone at all**. Every text test
still passed — with the mic off, `main.py` and `storage_main.py` serve the same
`/storage` and the same `/health` — and the spoken test simply waited 300
seconds. `tests/compose/test_csplit.py::TestEntryPoints` now reads the command
back off the daemon, because the compose file only says what the command would
be *given the environment*, and the environment is what a two-stack deployment
gets wrong.

An incidental finding, not filed as a test: cltl-context calls
`https://ipinfo.io` on every scenario start. Tier 1 patches that out
(`conftest.offline_location`), but a container cannot be patched, so every
tier-2 run geolocates the machine it is on and writes the result into the
scenario's `LeolaniContext`. It is a bare `except` with no timeout, so on a host
that blackholes outbound traffic it hangs rather than fails.

Docker arrived by adding `ghcr.io/devcontainers/features/docker-in-docker:2` to
`.devcontainer/devcontainer.json` and rebuilding — see Prerequisite 2, which
also records why docker-**in**-docker rather than sharing the host's daemon.
Component images build in about six seconds each on top of the published
`ghcr.io/leolani/cltl-base`, so tier 2 runs against images built from the source
in the working tree rather than whatever was last pushed.

What the tiers share is the `Conversation` driver and nothing else by accident.
Both runners expose an event bus, a probe and `url(module)`, which is enough to
drive a pipeline identically — but the assertions diverge exactly where the
design predicted. `tests/compose/test_serialization.py` exists only here,
because in tier 1 nothing is serialised.

Three things the implementation had to get right that the design under-specified:

* **The probe's readiness check has to count bindings, not detect them.** The
  design called for waiting until the probe's queue is bound, and the obvious
  implementation — ask RabbitMQ whether anything is bound to the topic — is
  wrong: the modules are bound to those same topics, so it returns yes before
  the probe's own queue exists. It has to compare the binding count either side
  of the subscribe, which makes the readiness hook a context manager rather than
  a callback. Kombu offers no cheaper signal: it binds a *copy* of the queue to
  the channel, so the caller's object never learns its server-assigned name.
* **`docker-in-docker`, not `docker-outside-of-docker`.** The workspace is
  bind-mounted from the host, so a shared daemon would resolve
  `${CLTL_CONFIG_DIR}` against a host path that does not exist — and Docker
  answers a missing bind source by creating an empty directory rather than
  failing. Every module would come up on the legacy topic names baked into its
  own image and the pipeline would be silent.
* **A failed `venv` build has to delete the venv.** `python -m venv` creates the
  directory before pip runs, and make treats an existing directory as an
  up-to-date target, so one failed install made every later `make build` report
  "Nothing to be done" over an unusable environment. Same class of bug as the
  `;`-vs-`&&` recipe fixed in phase 1, and it surfaced immediately after the
  devcontainer rebuild dropped the system audio headers.

**The finding this phase existed to produce.** `ElizaService` is subscribed to
the intention topic **twice**: `TopicWorker.run()` subscribes everything in
`self._topics` and then subscribes `self._intention_topic` again, and
`ElizaService.start` passes `[input_topic, intention_topic]`. Every
`IntentionEvent` is therefore delivered and processed twice. The visible
consequence is a blank message from the agent — `ElizaService._process` answers
an activation with `Eliza.respond(None)` and publishes the result without the
`if response:` guard its other branch has, and `Eliza.respond` returns its
greeting only the first time.

It is correct in process and wrong in containers. The worker's one-slot
`OVERWRITE` buffer usually swallows the duplicate in tier 1, where both
deliveries land on the publishing thread microseconds apart; over AMQP they
arrive far enough apart to both be processed. Neither tier alone would have
found it: tier 1 does not show the symptom, and an end-to-end smoke test would
have seen a blank line and called it cosmetic. Because the symptom is a race,
the test asserts on the subscription instead —
`tests/slices/test_intention_routing.py`, deterministic and in the fast tier.

**Phase 5 — the other two front ends. Done.** `src/cltl_integration/__main__.py`
plus `make demos` / `make demo-<name>` / `make test-manual`. The design's "~20
line `__main__.py`" was optimistic by about a factor of ten, and the extra is
entirely one thing the design did not name: **something has to open a scenario.**

`ChatUiService._create_payload` raises on a null scenario id and
`BackendService` will not record without one, so a demo that just starts the
modules comes up healthy and does nothing at all — no error, no log, an empty
page. With cltl-context in the scenario the launcher publishes the `init`
intention and lets the BDI loop open it, exactly as `app/py-app/app.py` does at
startup; without a BDI loop there is nobody to ask, so it publishes
`ScenarioStarted` itself. Those two branches are the whole reason
`tests/test_demo_launcher.py` exists: it starts the launcher as a subprocess,
reads the URLs out of its own report, holds a conversation through them, sends
SIGINT and checks the port was released — once per branch. A demo that answers
nothing is indistinguishable from a working one unless something talks to it.

That test immediately found a defect in the launcher, and it is a good argument
for having written it. The report was printed a line at a time while the modules
logged to stderr from their own threads, and about one run in ten a log record
landed *inside* a report line — leaving a person with
`http://127.0.0.1:8000/chatui2026-09-04/static/chat.html`. The report is now
built and written in a single call, and the test reads the two streams
separately and anchors its parse to the end of the line, so an interleaving
cannot be silently absorbed again. Twenty consecutive runs since.

**Manual tests are the third front end, and they are narrow on purpose.**
`tests/manual/test_chat_ui.py` runs one scenario three ways — in process, from
the images, and across the client/server split — and asks a person the same
question each time. They cover the one thing nothing else does: the automatic
suite drives cltl-chat-ui's REST endpoints and never loads its page, so the
HTML, the JavaScript and the static assets inside the image have no coverage at
all. Each test cross-checks the answer against the utterances the service
recorded, so a distracted `y` cannot pass an empty conversation, and a manual
test collected without a terminal skips rather than blocking on `input()`.

What did *not* get built, and why: nothing that needs audio hardware. A manual
test for text-to-speech would be the obvious fourth, since no automatic test
ever asserts that a reply was audible — but there is no sound device in this
devcontainer or in any of the images, so it would skip everywhere it could be
run. Same for a manual test of Whisper against a real human voice rather than
espeak-ng.

Phases 1–3 are the "automatic (required)" tier and are useful on their own.

---

## Phase 6 — the prior art, reviewed

Before retiring `app/docker-app/tests/`, both harnesses there were read in full
and mined. There were two, not one: the documented compose suite, and
`tests/manual/diagnostics/` — a container that joins an **already running**
stack's network and runs four per-component probes, reporting through its exit
code. Nothing references it: no makefile target, no README, no commit since it
was added.

**Already present here, usually improved.** The ChatClient (ported verbatim into
`drivers/chat.py`, then given an injected base URL instead of a hardcoded
`localhost:8003`); the stub microphone (`drivers/audio.py` — same 16 kHz/mono/480
framing and the same `audio/L16; rate=…; channels=…; frame_size=…` content type
`ClientAudioSource` parses strictly, same VAD-shaped silence padding, but
deterministic and offline instead of a gTTS call per utterance, and paced at real
time so an exhausted stub idles instead of spinning); readiness on ChatUI's
`scenario_id` rather than a health endpoint, with the same reasoning about
`MemoryChats` minting an `id` eagerly; config-by-bind-mount over `custom.config`;
`_raise_for_status` carrying the response body; per-run storage redirection;
`docker compose logs` captured on teardown; and the `container_id == audio_id`
correlation assertion, which `VadTest` had once and this suite has in four
places.

**Obsolete rather than missing.** `_compose_up`'s down-first plus five retries
defended against "port is already allocated" between fixed host ports, and the
one-pytest-process-per-module rule in `app/makefile` defended against the same
collision. Unique project names and ephemeral ports remove the failure, so there
is nothing left to retry around.

**Three real gaps, now closed.**

1. **Consent given by voice** (`tests/compose/test_spoken_consent.py`, topology
   `spoken_pipeline`). The old audio test scripted the microphone with
   `("Hello", "yes")` and drove the BDI handshake through it. Nothing here did:
   `audio_pipeline` has no cltl-context and the csplit-audio tests stop at the
   transcript. What it proves is narrow and not otherwise reachable — that
   `InitService`'s `"yes" in text.lower()` is satisfied by *Whisper's* output,
   which is capitalised, punctuated and occasionally not what was said.
2. **The reply, spoken** (`tests/slices/test_backend_tts.py`, topologies
   `backend_tts` and `backend_tts_mic`, driver `drivers/tts.py`).
   `[cltl.backend.tts]` was never run by anything — only validated against. It
   needs no sound device: with `remote_type` neither `console` nor `sound`,
   `AnimatedRemoteTextOutput` POSTs the reply to `$CLTL_TTS_URL/text`. It matters
   because `SynchronizedTextToSpeech.say` wraps its whole body in a bare
   `except:` that only logs, so a backend that cannot speak keeps publishing,
   keeps recording, keeps answering in the chat UI, and is simply silent. This is
   also what settled the withdrawn audio-lock risk above.
3. **Speech without an apt package** (`fixtures/speech/`, `fixtures.py`,
   `make speech-fixtures`). The diagnostics harness committed its PCM payload;
   this component gated its slowest and most valuable tests on `espeak-ng` being
   installed. Every phrase the suite speaks now lives in one list with espeak-ng's
   rendering committed beside it, and `audio.speech_for` prefers the binary and
   falls back to the file. `--say` at the command line still needs espeak-ng,
   which is the one place a phrase has no committed rendering.

### Retiring the prior art

Not done here, and deliberately a separate decision. What it involves, recorded
now while the review is fresh:

- `app/docker-app/tests/integration/` — 26 tracked files. The only thing that
  references them is `app/makefile:43-69`, five `test-integration*` targets. On
  disk they also carry ~347 MB of untracked run residue under `storage/`,
  including a 66 MB `storage/test.log` that every container appends to across
  every run (`logging.config` opens it in append mode on a shared mount), plus
  3.1 MB and 2.9 MB under the two csplit directories. `storage/clean.sh` exists
  to clear this and is invoked by nothing.
- `app/docker-app/tests/manual/` — 18 tracked files, referenced by nothing at
  all: no makefile target, no README, no commit since it was added. Includes
  248 KB of committed binary in two `.raw` payloads which are byte-identical to
  each other, because `generate_audio.py` writes the same buffer to both and
  says so in a comment.

**Both were removed** together with the rest of `app/`, once this review had
established that nothing in them was still needed. The `eliza-app` image they
exercised went with them; it had no remaining consumer.

**Deliberately not ported: the attach-to-a-running-stack mode.** It is the
diagnostics harness's whole reason for existing and the one capability this
component lacks — every runner here *owns* the stack it tests. It would fit as a
third `Runner` needing only an AMQP URL and a module→URL map. Left until
`app/docker-client/` and `app/docker-eliza-server/` settle, since those are what
would be probed.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| ~~**TTS audio-lock deadlock.**~~ **Withdrawn — this risk does not exist.** The reasoning was: `SynchronizedTextToSpeech.say` takes `get_write_lock(AUDIO_RESOURCE_NAME)` with `timeout=-1` (`sync_tts.py:68`), the only provider is `SynchronizedMicrophone.start` (`sync_microphone.py:45`), so backend-TTS without the microphone must block forever. The premise is right and the conclusion does not follow: `BackendService.start` calls `Backend.start` unconditionally (`cltl_service/backend/backend.py`), which starts the microphone **object** regardless of `[cltl.backend.mic] topic`. The topic gates the recording thread, not the resource | Measured both ways in `tests/slices/test_backend_tts.py`: with the microphone off the reply is spoken in 0.1 s, and with it recording the mic mutes itself and the reply still arrives. The guard, `validate_config` and `validate_topology` are removed; that test is now where the finding lives |
| **Any failure inside `TopicWorker.run()` before `_started.set()` hangs `container.start()` forever.** All 12 call sites do `topic_worker.start().wait()` with no timeout. Realistic trigger: `event_bus.subscribe` against an unreachable broker | Global `timeout` + `--timeout-method=thread` in `pytest.ini`; runner `start()` takes an explicit timeout |
| `DIContainer._singletons` is class-level and process-wide | `reset_process_state()` as specified above; call `_reset()` on `DIContainer`, never a subclass |
| `@singleton` returns `False` (not `None`) for absent optional services | Guard with truthiness, never `is not None` |
| Starting the backend twice in one process raises `ValueError: Resource already provided` — `SynchronizedMicrophone.start` calls `provide_resource` uncaught, unlike `TopicWorker` which swallows it | Runner enforces one container lifecycle per topology and asserts teardown completed |
| A partial topology blocking on an unmet `TopicWorker` dependency | Not a risk, verified: no service anywhere declares `requires=` — only `provides=`. And `__resolve_dependencies` uses `_DEPENDENCY_TIMEOUT = 10` and raises `TopicError` |
| Tier 2 silently tests stale images. `docker-ghcr-build` is a separate root target, not part of `make build`, and compose uses `${VERSION:-latest}` while every component has its own timestamped VERSION | Tier-2 target runs `docker image inspect` on every required image and fails fast; use `latest` deliberately |
| Unique `--project-name` still collides because `docker-compose.yml` pins `container_name: ${COMPOSE_PROJECT_NAME}-rabbitmq` and bind-mounts `./storage/rabbitmq` | Drop `container_name` and the mnesia bind mount from the harness compose file; that also removes the prior art's need to `rmtree` stale mnesia data |
| Fixed host ports forced the existing suite into serialized sessions with 5 `up` retries (`app/docker-app/tests/integration/conftest.py:65`) | Ephemeral host ports + unique project name; intra-network traffic keeps using service names (verified: `storage_url: http://eliza-backend:8000/storage/`, `server: amqp://…@rabbitmq:5672/`) |
| The stub audio server dials *in* from containers on a hardcoded 9876 | Bind the stub on port 0, read `getsockname()[1]`, export it as `$STUB_PORT`, *then* `compose up`; the backend fragment needs `extra_hosts: "host.docker.internal:host-gateway"` |
| Whisper pulls ~140 MB on first run | Only in `slow`-marked tier-2 topologies, with a warmed cache volume |
| A tier-2 failure without container logs is undebuggable | Keep the prior art's `docker compose logs` → file on teardown |

---

## Verification

```bash
# Phase 1
make -C cltl-requirements build                  # mirror now carries pytest
make -C integration build                        # offline venv, all modules from leolani/
ls integration/venv/lib/python3.10/site-packages | grep -c cltl   # not just pip/setuptools/wheel
make -C integration test                         # green
#   then: add a failing test and confirm the target turns RED (regression guard
#   against the `;`-vs-`&&` bug that makes every other component's tests green)

# Phases 2-3 (tier 1) — 110 passed, 4 xfailed, ~4 min
make -C integration test

# Phase 4 (tier 2) — 40 passed, 1 xfailed, ~20 min (includes the client/server split)
docker pull ghcr.io/leolani/cltl-base:latest     # published; components layer on it
docker pull rabbitmq:3.12-management
for c in cltl-backend cltl-chat-ui cltl-context cltl-eliza \
         cltl-emissor-data cltl-vad cltl-asr; do
    make -C $c docker-ghcr-build                 # ~6s each
done
make -C integration docker-images                # fails fast if any is missing
make -C integration test-compose
docker compose ls                                # no stacks left behind

# Phase 5 — the demo and manual front ends
make -C integration demos                        # lists topologies and deployments
make -C integration demo-text-pipeline           # prints URLs, chat UI reachable, clean Ctrl-C
make -C integration demo-csplit DEMO_FLAGS='--tier compose'
make -C integration test-manual                  # 4 tests, a person in the browser
#   without a terminal they skip; `pytest -m manual` alone must never hang

# Phase 6 — the ports from the prior art
make -C integration speech-fixtures              # byte-reproducible: git diff is empty
sudo mv /usr/bin/espeak-ng /usr/bin/espeak-ng.hidden
make -C integration test-compose PYTEST_FLAGS="-q -k 'spoken_consent or SpokenConversation'"
#   must PASS on the committed fixtures, not skip
sudo mv /usr/bin/espeak-ng.hidden /usr/bin/espeak-ng
make -C integration demo-backend_tts             # reports a stub loudspeaker URL

# Regression: the existing app suite is untouched
make -C app test-integration-text
```
