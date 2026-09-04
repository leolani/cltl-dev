# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

The Eliza App is an event-driven conversational AI built on the CLTL (Computational Lexicology & Terminology Lab) framework. It is structured as a meta-repository of git submodules, each implementing one component of the system.

## Architecture

### Core Components

| Submodule | Role |
|---|---|
| `emissor` | Core data representation framework — multimodal signals, episodic knowledge graph |
| `cltl-combot` | Shared infrastructure: EventBus, ConfigurationManager, ResourceManager, DIContainer, TopicWorker |
| `cltl-backend` | REST API for raw audio/video device access and signal storage |
| `cltl-vad` | Voice activity detection (WebRTC VAD) — splits audio stream into speech segments |
| `cltl-asr` | Pluggable speech-to-text (Whisper, Google, Wav2Vec, SpeechBrain) |
| `cltl-eliza` | ELIZA-style conversational logic |
| `cltl-chat-ui` | Web-based text chat interface |
| `cltl-emissor-data` | Event-driven EMISSOR data persistence service |
| `cltl-context` | Conversation context and scenario management (lives in `app/src/eliza_app_service/context/`) |
| `app` | Application entry point — wires all containers, Flask dispatcher, `py-app/app.py` |
| `integration` | Integration tests for the modules — composes topologies and asserts the boundaries hold. Not a submodule; see `integration/README.md` and `docs/integration-testing-design.md` |

> **Known bug**: `app/src/eliza_app_service/context/service.py:24` calls `config_manager.get_config("eliza.context")` but the section in `default.config` is `[app.context]`. This raises `NoSectionError` at runtime; pending fix.

### Event System

Components communicate through an `EventBus`. Events carry a typed `payload` and `EventMetadata` (timestamp, topic, `scenario_id`, tenant). Two implementations exist — see [Event Bus](#event-bus) below.

### Service Pattern

Every CLTL component follows a four-file structure under `cltl_service/<component>/`:

```
cltl/<component>/
  api.py             # ABC defining the component interface (pure logic, no EventBus dependency)

cltl_service/<component>/
  schema.py          # Dataclasses for event payloads (extend EMISSOR base events)
  service.py         # TopicWorker-based service; has from_config(), start(), stop(), _process(event), app
  container.py       # DIContainer subclass; @property @singleton accessors; calls super().start()/stop()
```

**`from_config` classmethod** — every service has one; it reads configuration and returns a fully wired instance. Callers never call `__init__` directly.

**`app` property** — returns a Flask WSGI app (for services with HTTP endpoints) or `None`. The root `app.py` mounts non-`None` apps via `DispatcherMiddleware`.

**Lifecycle**:
1. `container.start()` calls `super().start()` first, then `service.start()`
2. `service.start()` creates a `TopicWorker` and calls `topic_worker.start().wait()`
3. Shutdown is the reverse: `service.stop()` → `topic_worker.stop()` + `topic_worker.await_stop()`, then `super().stop()`

## Package Structure

```
src/
  cltl/<component>/          # ABCs, pure-logic implementations — no EventBus/TopicWorker dependency
  cltl_service/<component>/  # Service wrappers, containers, schemas
```

- `setup.py` uses `find_namespace_packages(include=['cltl.*', 'cltl_service.*'], where='src')`
- `__init__.py` files **must be empty** — namespace packages break if they contain imports
- Config section names mirror the Python package path: `[cltl.asr]`, `[cltl.asr.whisper]`, `[cltl.backend]`; the app-layer exception is `[app.context]`

## Event Bus

Two implementations in `cltl.combot.infra.event`:

| `implementation` config value | Class | Module | When |
|---|---|---|---|
| `internal` | `SynchronousEventBus` | `memory.py` | Local dev, single process, tests |
| `kombu` | `KombuEventBus` | `kombu.py` | Docker Compose / multi-process (RabbitMQ) |

Toggle via `[cltl.event] implementation: internal|kombu` in `default.config`.

**Publishing**: `event_bus.publish(topic, Event.for_payload(payload))`. Pass `source=source_event` to propagate `scenario_id` and `tenant` from an upstream event.

**Topics** are plain strings (e.g., `cltl.topic.vad`, `cltl.topic.text_in`). Always read topic names from config — never hardcode them.

## DI Container Conventions

- All containers extend `InfraContainer` (which mixes in `KombuEventBusContainer`, `K8LocalConfigurationContainer`, `ThreadedResourceContainer`)
- Singleton services use `@property @singleton` — at most one instance per container class
- **`@singleton` cannot return `None`** — use `False` as the sentinel for "intentionally absent" optional services; callers guard with `if self.asr_service:`
- Call `ApplicationContainer.load_configuration()` before instantiating the container; it loads `default.config` plus optional `custom.config` and `credentials.config`
- `ApplicationContainer` in `app.py` uses multiple-inheritance MRO to compose all component containers; `start()`/`stop()` chain via `super()`

## Development Commands

### Initial Setup
```bash
git clone --recurse-submodules -j8 https://github.com/leolani/eliza-app.git
cd eliza-app
```

### Build and Run
```bash
# Build the entire project (run twice — first pass builds deps, second links them)
make build
make build

# Run locally
cd app && source venv/bin/activate
cd py-app && python app.py
```

The active root makefile is the lowercase `makefile`. A byte-identical `Makefile`
also exists, but GNU make searches `GNUmakefile` → `makefile` → `Makefile` and
stops at the first hit, so edits to `Makefile` have no effect.

The build requires Python 3.10 (see `.python-version`) and the PortAudio and
libsndfile system headers — `pyaudio` has no aarch64 wheel and compiles from
source:

```bash
sudo apt-get install -y portaudio19-dev libsndfile1 libasound2-dev
```

Component installs use `pip --no-index --find-links=cltl-requirements/mirror
--find-links=cltl-requirements/leolani`, so an empty `cltl-requirements/mirror/`
makes every venv unbuildable.

**A green `make build` is not evidence the build worked.** The `venv:` recipe in
`util/make/makefile.py.base.mk` chains its steps with `;` rather than `&&`, so a
failed `pip install` leaves the recipe exiting 0, and the following `touch venv`
marks the target up to date. A venv containing only `pip`/`setuptools`/`wheel`
is a failed install. Use the `build` skill to check.

### Integration tests

```bash
make -C integration build   # offline venv, every module from cltl-requirements/leolani
make -C integration test    # tier 1: modules composed in-process, no Docker
```

`integration/` installs the **published sdists** rather than the source trees, so
it is also the only thing in the repo that catches a `setup.py` that fails to
package a file.

Tier 2 runs the real container images and needs Docker — provided by the
`docker-in-docker` devcontainer feature — plus the component images built from
the current source:

```bash
make -C integration docker-images   # says what is missing and how to build it
make -C integration test-compose    # ~18 min
```

The spoken-conversation tests additionally need `espeak-ng` (`sudo apt-get
install -y espeak-ng`); without it they skip.

### Demos and manual tests

The same topologies, run without assertions:

```bash
make -C integration demos                 # what there is to run
make -C integration demo-text-pipeline    # in this process; Ctrl-C to stop
make -C integration demo-csplit DEMO_FLAGS='--tier compose'
make -C integration test-manual           # a person drives the chat UI
```

`python -m cltl_integration <scenario>` is the launcher behind `demo-<name>`;
it opens a scenario itself (the `init` intention when cltl-context is present,
`ScenarioStarted` otherwise) because the chat UI renders nothing without one.
`manual`-marked tests are excluded from `test`, `test-compose` and `test-all`,
and skip rather than hang when there is no terminal.

Tier 2 also runs the **client/server split** (`tests/compose/test_csplit.py`):
two compose projects on two networks, cltl-context and cltl-chat-ui on the
client and cltl-eliza and storage on the server, with the client's audio stored
remotely over HTTP. It is the only thing that exercises
`cltl-backend/src/storage_main.py` (`StorageContainer` rather than
`BackendContainer`). See `integration/src/cltl_integration/runner/split.py`.

### Component Management
```bash
make clean          # Clean all components. Also runs cltl-requirements' own clean,
                    # which deletes mirror/, leolani/ and requirements.lock.
                    # Prefer `make -C <component> clean`.
make install        # NOT a Python install — cltl-requirements has `install: docker`,
                    # so this builds the ghcr.io/leolani/cltl-base images.
make update-build   # Update build system (nested util submodule) across submodules
```

`make run` and `make stop` are defined in `util/make/makefile.parent.mk` but are
**dead targets**: they expand to `$(MAKE) --directory=$(project_name) run` with
the root `project_name ?= "eliza-app"` (literal quotes, no such directory), and
no component defines a `run` or `stop` target. Run the app with:

```bash
cd app && source venv/bin/activate
cd py-app && python app.py
```

## Configuration

### Files
- `app/py-app/config/default.config` — committed baseline (INI-style)
- `app/py-app/config/custom.config` — local overrides, committed but intentionally sparse
- `app/py-app/config/credentials.config` — secrets (gitignored); e.g. `GOOGLE_APPLICATION_CREDENTIALS`

### Syntax
- `$VAR` and `${VAR}` interpolation is supported in values
- `[environment]` section sets process environment variables at startup
- To disable an optional component, set `implementation:` (empty value) in its config section

### Key Settings
- Audio: 16 kHz, 1 channel, 480-sample frames (`[cltl.audio]`)
- ASR: Whisper by default; set `implementation:` to disable (`[cltl.asr]`)
- Backend: local server on port 8000 (`[cltl.backend]`)
- Event bus: `internal` by default; `kombu` for Docker (`[cltl.event]`)

## Runtime Endpoints

After `python app.py`:

| Path | Service |
|---|---|
| `http://localhost:8000/host` | Backend (audio/video device API) |
| `http://localhost:8000/chatui/static/chat.html` | Chat UI |
| `http://localhost:8000/emissor` | EMISSOR data API |
| `http://localhost:8000/storage` | Storage service |

## Deployment Options

1. **Local Python** — all components in one process (recommended for dev)
2. **Docker Compose** — containerised with RabbitMQ (`kombu` event bus); `docker-compose.yml` at root
3. **Kubernetes** — full orchestration for production

## Development Conventions

### Imports
- No wildcard imports (`from module import *`)
- Keep `__init__.py` files empty — namespace packages require it
- Never manipulate `sys.path` (no `sys.path.append`); use `importlib` for dynamic imports

### Adding a new component
1. Create `cltl/<component>/api.py` with the ABC (no EventBus/TopicWorker imports)
2. Create `cltl_service/<component>/schema.py` with event dataclasses
3. Create `cltl_service/<component>/service.py` with `from_config` and `TopicWorker`-based `_process`
4. Create `cltl_service/<component>/container.py` extending `InfraContainer` with `@property @singleton`
5. Add `[cltl.<component>]` section to `default.config`
6. Wire the container into `ApplicationContainer` via MRO in `app/py-app/app.py`

### Lazy ML imports
Heavy dependencies (torch, transformers, speechbrain) must be imported inside the relevant `if implementation ==` branch in the container's factory method — never at module top-level. This keeps unconfigured backends out of the import graph. See `cltl-asr/src/cltl_service/asr/container.py` for the reference pattern.

### Tests
Write unit tests with `pytest`. For service tests use `SynchronousEventBus` and `threading.Event` for synchronization. See `cltl-asr/tests/test_asr_service.py` for the reference pattern.
