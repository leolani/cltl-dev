---
name: add-submodule
description: Integrate a module from an online git repository into the CLTL meta-repo as a new submodule, and generate whatever it lacks to take part in the Python and Docker builds (makefile, Dockerfile, main.py, setup.py/requirements, nested util, cltl-requirements entries, root makefile, docs, smoke test), plus a review that published events propagate scenario_id and tenant via source=. Use when asked to add, integrate, import or onboard a new module, component or submodule from a git URL. Commits locally only — never pushes.
---

Onboarding skill for the CLTL meta-repo: add a git submodule and make it
buildable like the existing components.

Usage: `/add-submodule <git-url> [<dir-name>]`

## Rules

- **Never push**, tag, open PRs, amend, rebase or `git submodule update --remote`.
  Commit locally and say so: *"Nothing has been pushed."* Pushing the scaffold
  branch upstream is the user's step.
- **Never overwrite a file the upstream repo already has.** For anything that
  would change one, show the diff and ask.
- Scaffolds go **inside the submodule, on a new local branch** (`add-platform-build`),
  committed there. Parent-repo pointers are recorded afterwards with `/submodule-git`.
- Read `CLAUDE.md` first; it is the source of truth for the conventions below
  (service pattern, empty `__init__.py`, config naming, slim-base criteria).
- The reference component is **`cltl-monitoring`** (slim base, HTTP service,
  optional extras). Copy its shape, not its content. `cltl-asr` is the reference
  for a heavy/full-base module with lazy ML imports.

## Step 0 — Inputs and preconditions

Ask (AskUserQuestion) for anything not given:

1. **Git URL** and **directory name** (`cltl-<x>`; default: repo name).
2. **Branch to track.** Existing submodules use `ref_scenarios` or `main`; list
   the remote's branches with `git ls-remote --heads <url>` and offer them.
3. **Python package** (`cltl.<x>` / `cltl_service.<x>`) — infer from upstream
   `src/` if present, else confirm.

Preconditions: the directory must not exist; `git status --short` must show
nothing except known churn (`*/VERSION`, `.devcontainer`, untracked locals).
Do not proceed over unrelated staged changes.

## Step 1 — Add the submodule

```bash
git submodule add -b <branch> <url> <dir>
```

The parent has no remote; `git submodule add` stages `.gitmodules` and the
gitlink only. Do not commit the parent yet (Step 11).

## Step 2 — Inspect upstream and write a gap list

Compare `<dir>` with this checklist and **show the table to the user** before
generating anything:

| Item | Needed | Present? |
|---|---|---|
| `setup.py` — `find_namespace_packages(include=['cltl.*','cltl_service.*'], where='src')`, reads `VERSION`, `data_files`, `package_data` for non-py assets | yes | |
| `VERSION` (contains `dev` so the build restamps it) | yes | |
| `requirements.txt` — `.` plus `cltl.combot[external]` (kombu is imported at module level by `InfraContainer`) | yes | |
| `makefile` | yes | |
| `Dockerfile` | yes | |
| `config/default.config`, `config/logging.config` | yes | |
| `src/main.py` | yes | |
| `src/cltl/<x>/api.py` (no EventBus/TopicWorker imports) | yes | |
| `src/cltl_service/<x>/{schema,service,container}.py` with `from_config`, `app`, `@property @singleton` | yes | |
| every `__init__.py` under `cltl*`/`cltl_service*` **empty** | yes | |
| `tests/` | yes | |
| nested `util` submodule | yes | |

Also check: wildcard imports, `sys.path` manipulation, hard-coded topic names,
heavy imports at module top level (torch, transformers, speechbrain — must be
lazy, inside the `if implementation ==` branch; see
`cltl-asr/src/cltl_service/asr/container.py`). Report these as findings; do
not silently rewrite upstream logic.

If the upstream is not a CLTL-pattern module at all (no service layer), stop
and ask whether to write the `cltl/` + `cltl_service/` wrapper or abort.

## Step 3 — Nested `util`

Every component carries `leolani/cltl-build` as `util`. If `<dir>/util` is
missing:

```bash
cd <dir> && git submodule add -b main https://github.com/leolani/cltl-build.git util
```

Never run `make update-build` here.

## Step 4 — Generate only what is missing

Do all of this on a new branch in the submodule (`git -C <dir> switch -c add-platform-build`).

**`makefile`** (template: `cltl-monitoring/makefile`)

```make
SHELL = /bin/bash

project_dependencies ?= $(addprefix $(project_root)/, \
		emissor \
		cltl-requirements \
		cltl-combot \
		<other modules this one imports, from install_requires>)

git_remote ?= https://github.com/leolani

include util/make/makefile.base.mk
include util/make/makefile.component.mk
include util/make/makefile.py.base.mk
include util/make/makefile.git.mk
include util/make/makefile.docker.mk

clean: py-clean
```

`project_dependencies` come from `install_requires` entries that are `cltl.*`
or `emissor`; map each to its directory. `makefile.d` is generated — never write it.

**`Dockerfile`** (template: `cltl-monitoring/Dockerfile`). Requirements:

- `ARG base_image=<chosen in Step 5>` **in the Dockerfile**. Do not rely on
  `docker_base`; each Dockerfile owns its base.
- `COPY --from=leolani . /leolani/`, install with
  `pip install --no-index --no-build-isolation --find-links=/leolani -r requirements.txt`,
  then remove `/leolani` and `__pycache__`.
- OCI labels (`source`, `description`, `licenses`), `WORKDIR /<dir-without-extra-hyphens-as-upstream-uses>`
  — record the WORKDIR; the integration registry needs it (cltl-chat-ui builds to `/cltl-chatui`).
- `HEALTHCHECK` with `python -c urllib.request` (no `curl` on slim), only if the
  module serves HTTP; otherwise omit it.
- `CMD ["python", "src/main.py"]`.

**`src/main.py`** (template: `cltl-monitoring/src/main.py`; it is a top-level
module and deliberately **not** packaged). It must:

- configure logging from `CLTL_LOGGING_CONFIG` or `config/logging.config`;
- `register_type_var(PAYLOAD)`, `SIG`, `MEN`;
- define `ApplicationContainer(<X>Container)` and call
  `ApplicationContainer.load_configuration()` before instantiating it;
- start the container, and mount `container.<x>_service.app` under a
  `DispatcherMiddleware` **only when it is truthy** (`@singleton` cannot return
  `None`; optional services are `False`);
- stop the container on shutdown (reverse of start).

**`config/default.config`** — standalone defaults only, `[cltl.event]
implementation: internal`, a `[cltl.<x>]` section whose name mirrors the Python
package path, topics read from config. Add a header comment that these are for
standalone runs and the deployment's own config overrides them.

**`setup.py` / `requirements.txt` / `VERSION`** — fix gaps only. Remember
`package_data` for any non-Python asset or the sdist silently drops it (this is
what `integration/` catches). Do not name `opencv-python` or
`opencv-python-headless` in any `install_requires` or requirements file; import
cv2 inside a `try` instead.

## Step 5 — Base image decision

Scan the module's imports and requirements and apply the four slim-incompatibility
criteria from `CLAUDE.md`:

1. a third-party dependency not already in `requirements.slim.txt`;
2. a test library (`mock`, `parameterized`, …) in a runtime extra;
3. PyAudio or another capture-device library;
4. naming cv2 as a dependency.

Also treat torch / transformers / whisper / spacy / webrtcvad as full-base.
Present the verdict, **recommend slim unless something above hits**, and confirm
with the user. Record the answer: it decides the `ARG base_image` default
(`ghcr.io/leolani/cltl-base-slim:latest` or `ghcr.io/leolani/cltl-base:latest`)
and which requirements file gets new packages.

## Step 6 — `cltl-requirements`

Component images install with `--no-index --find-links=/leolani`, so any
third-party package the module needs and the chosen base lacks cannot resolve.

- Add new third-party deps to `cltl-requirements/requirements.txt` (offline
  mirror, used by venvs) **and** to `requirements.base.txt` or
  `requirements.slim.txt` according to Step 5. Pin versions like the existing
  entries; comment why each was added.
- `cltl-requirements/leolani/` is fed only by this repo's submodules' sdists —
  a `cltl.*` dependency that is not a submodule here cannot be installed. Flag
  it to the user (cf. `cltl.friends` / `cltl.leolani` in
  `docs/plans/monitoring-optional-names.md`) rather than guessing.
- Tell the user the base images must be rebuilt (`make -C cltl-requirements docker`)
  and that this is a parent-repo change (cltl-requirements is not a submodule).

## Step 7 — Root makefile

Add `${project_root}/<dir>` to `project_components` in the root `makefile`,
**after every module it depends on and before modules that depend on it**
(`integration` stays last). The root makefile is the lowercase `makefile`.

## Step 8 — Docs

- `CLAUDE.md`: a row in the component table; a bullet under *Key Settings* for
  any config switch a deployment must know about; list the base image if it is
  one of the slim components.
- `.claude/skills/submodule-git/SKILL.md`: update the gitlink count and the
  list of submodules.
- `.claude/skills/build/SKILL.md`: add the module to the build-order list if one
  is kept there.

## Step 9 — Event-propagation review (`source=`)

Every event a module publishes **while handling an upstream event** must carry
that event's `scenario_id` and `tenant`. The API is in
`cltl-combot/src/cltl/combot/infra/event/api.py`:

- `Event.for_payload(payload, source=<upstream event>)`
- `Event.for_scenario_payload(scenario_id, payload, source=<upstream event>)`

`Event.with_source` is what copies tenant and scenario. A bare
`Event.for_payload(payload)` drops both, and downstream scenario-bound services
(chat-ui status bar, monitoring, emissor-data) silently discard or misfile it.

Procedure:

1. Grep the module's `src/` for every `.publish(`, `Event.for_payload(`,
   `Event.for_scenario_payload(` and direct `Event(` construction.
2. Classify each site:
   - **(a) In a handler** (`_process(event)` or anything the incoming event is
     in scope for) → must pass `source=event`. Missing = **finding**.
   - **(b) Originates events** with no upstream event (device capture loop,
     timer, HTTP request handler) → must use `for_scenario_payload` with an
     explicit scenario id from state, or be documented as intentionally
     scenario-less. Anything else = **finding**.
   - **(c) Deferred publish** — a payload stored in a queue, dict or thread and
     published later: check the source event (or at least scenario id and
     tenant) travels with it. If not = **finding**.
3. Check the tests: at least one asserts `event.metadata.scenario_id` and
   `tenant` on an emitted event. If none does, say so.
4. Report as a table: `file:line | category | problem | suggested fix`.

Fixes edit upstream logic, so **ask before applying** them on the scaffold
branch. Findings must be fixed or explicitly waived by the user before Step 12.

## Step 10 — Smoke test

Add `<dir>/tests/test_<x>_service.py` following `cltl-asr/tests/test_asr_service.py`:
`SynchronousEventBus`, `threading.Event` for synchronisation, a service built
via `from_config`. Assert that:

- the module imports and the container starts and stops;
- a published input event produces the expected output event on the configured
  topic;
- **the output event carries the input event's `scenario_id` and `tenant`**
  (set both on the input via `Event.for_scenario_payload` / metadata).

Do not add `__init__.py` imports; keep package `__init__.py` files empty.

## Step 11 — Verify

Use the `build` skill. A green `make build` proves nothing — its recipe chains
with `;`, so a failed `pip install` still exits 0.

1. `make -C <dir> build`, then confirm `<dir>/venv` contains the module's
   dependencies (not just pip/setuptools/wheel) and that the sdist landed in
   `cltl-requirements/leolani/`.
2. `make -C <dir> test` — the smoke test passes.
3. If Docker is available: `make -C <dir> docker-ghcr-build`, then check the
   container starts and (if HTTP) the HEALTHCHECK goes healthy. If Docker is
   absent, say that this was not verified.
4. Report what was and was not verified, with output for failures.

## Step 12 — Commit locally

1. In the submodule: commit the scaffold on `add-platform-build` in logical
   commits (build files; main/config; tests; event-propagation fixes).
2. Hand off to `/submodule-git` to record the pointer and commit the parent
   changes (`.gitmodules`, gitlink, root `makefile`, `cltl-requirements/`,
   `CLAUDE.md`, skills).
3. State plainly: *"Committed locally in <dir> and in the parent. Nothing has
   been pushed. The `add-platform-build` branch must be pushed upstream (and
   `.gitmodules` pointed at it) before others can fetch this commit."* If the
   gitlink points at a commit that only exists locally, say so — it is the most
   likely way this goes wrong for a collaborator.

## Step 13 — Optional: register in the integration harness

Ask whether to add the module to `integration/` so tests and demos can compose
it. If yes, follow `CLAUDE.md` *Adding a new component* step 6:

- `Module` entry in `integration/src/cltl_integration/modules.py` (key,
  container `"module.path:Class"`, image, requirement with extras, **workdir
  matching the Dockerfile**, HTTP `mounts`);
- section in `integration/config/base.config` (disabled), switched on in a
  topology under `integration/config/topologies/`;
- `integration/requirements.txt`, and a service in `integration/compose/docker-compose.yml`.

If no, list it as a follow-up in the final report.

## Final report

Summarise: gap list and what was generated, base image chosen and why, deps
added to `cltl-requirements`, event-propagation findings (fixed / waived /
open), what was verified versus not, and the explicit not-pushed notice.
