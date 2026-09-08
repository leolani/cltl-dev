---
name: build
description: Build, clean, run, or test the CLTL Leolani platform monorepo or any single component, and diagnose build failures. Use when building, cleaning, running, or testing any part of the project, or when a make target fails, a venv looks broken, or pip cannot resolve packages.
---

Build, clean, run, test and repair skill for the CLTL Leolani platform monorepo.

Usage: `/build [<module> | clean [<module>] | run | test [<module>] | check]`

## Components (build order)

```
emissor  cltl-requirements  cltl-combot  cltl-backend  cltl-context
cltl-eliza  cltl-chat-ui  cltl-emissor-data  cltl-monitoring  cltl-vad
cltl-asr  integration
```

The real order comes from the generated `<component>/makefile.d` files, not from
this list. Regenerate them with `make depend` if the ordering looks wrong.

## Health check — run this first

Read-only. Run it before every branch except `run`.

```bash
cd /workspaces/cltl-dev
for c in emissor cltl-requirements cltl-combot cltl-backend cltl-context \
         cltl-eliza cltl-chat-ui cltl-emissor-data cltl-monitoring \
         cltl-vad cltl-asr integration; do
  if   [ "$c" = cltl-requirements ]; then s="n/a (builds the mirror, not a venv)"
  elif [ ! -d "$c/venv" ];            then s="missing"
  elif [ ! -x "$c/venv/bin/python" ]; then s="DEAD (dangling interpreter)"
  else
    n=$("$c/venv/bin/python" -c 'import os,sysconfig;print(len(os.listdir(sysconfig.get_paths()["purelib"])))' 2>/dev/null)
    # a venv that only has pip/setuptools/wheel has ~11 entries: the install failed
    if [ "${n:-0}" -le 15 ]; then s="SUSPECT ($n pkgs — pip install failed, see T1b)"
    else s="ok ($n pkgs)"; fi
  fi
  printf '%-20s %s\n' "$c" "$s"
done
echo "python:     $(python -V 2>&1)  (needs 3.10, see .python-version)"
for d in . cltl-requirements; do
  [ -f "$d/.python-version" ] && echo "  pin $d: $(cat "$d/.python-version") -> $(cd "$d" && python -V 2>&1)"
done
echo "mirror:     $(ls cltl-requirements/mirror 2>/dev/null | wc -l) files"
echo "leolani:    $(ls cltl-requirements/leolani 2>/dev/null | wc -l) sdists"
echo "lock:       $([ -f cltl-requirements/requirements.lock ] && echo present || echo absent)"
echo "makefile.d: $(ls */makefile.d 2>/dev/null | wc -l)/12"
integration/venv/bin/python -c "import cltl.combot, emissor; print('integration/venv: imports ok')" 2>&1 | tail -1
```

Read the result before trusting any build:

- A venv reported `DEAD` or `missing` is not a working environment, **and
  `make build` will still report success** — make only checks that the `venv/`
  directory is newer than its prerequisites. See T1.
- `mirror: 0 files` with `lock: present` is the stale-lock trap. See T3.
- `python:` anything other than 3.10 will fail the mirror download. See T4.

---

## Dispatch on `$ARGUMENTS`

### No argument — full project build

Run the health check first. If any venv is `DEAD`/`missing`, or `mirror:` is 0,
**stop and report** — a plain `make build` will not fix it and will exit 0
regardless. Go to "Repairing a broken environment" and get the user's agreement
before touching `cltl-requirements`.

Otherwise, `make build` must be run **twice** because of dependency ordering:

```bash
cd /workspaces/cltl-dev
make build && make build
```

Read the output rather than trusting the exit code, then re-run the health check
and report per-component status.

### `check` — diagnose only, change nothing

Run the health check, map each anomaly onto the triage table below, and report
symptom → cause → proposed fix. Make no changes. This is the right branch when
the user says "the build is broken" without more detail.

### `clean [<module>]`

With a module name — safe:

```bash
make -C /workspaces/cltl-dev/<module> clean
```

That runs `py-clean` (`rm -rf venv dist build *.egg-info`, drops this
component's timestamped sdists from `cltl-requirements/leolani/`, `pip cache
remove`) plus `base-clean` (`rm -rf makefile.d`). Run `make -C <module> depend`
afterwards if you are not about to build.

Without a module name — **ask first.** Root `make clean` cascades into
`cltl-requirements`' own `clean:`, which deletes `mirror/`, `leolani/` and
`requirements.lock`. Rebuilding the mirror is a multi-GB download that needs
Python 3.10. Offer per-component clean instead.

### `<module-name>` — build a single component

```bash
make -C /workspaces/cltl-dev/<module> build
```

If the argument matches no name in the components list, say so and show the list.

- `integration` deliberately does **not** include `makefile.py.base.mk` and has no
  `setup.py`. That file's `build` chains to `py-install`, which would copy an
  sdist into `cltl-requirements/leolani/` — the registry every component and
  Dockerfile installs from, which a test harness must never publish itself into.
  It defines its own `venv`/`test` recipes instead, chained with `&&` so a failed
  install fails the target. `integration/venv` is the environment that has every
  `cltl.*` module plus pytest, so it is where tests run from.
- `cltl-requirements` has `.DEFAULT_GOAL := build` and builds the **pip mirror**,
  not a Python package. It has no `venv` (it does not include
  `makefile.py.base.mk`). Treat it as ask-first.

### `run` — see the platform running

Root `make run` and `make stop` are **dead targets** — do not use them (T9).
There is no application in this repository: `app/` was removed and `integration/`
is what runs the modules now.

```bash
make -C /workspaces/cltl-dev/integration demos              # what there is to run
make -C /workspaces/cltl-dev/integration demo-text-pipeline # Ctrl-C to stop
```

The launcher prints every URL it discovered, the chat page among them, and opens
a scenario itself — without one `ChatUiService` renders nothing and
`BackendService` records nothing. `--tier compose` runs the real images instead;
a deployment (`demo-csplit`) requires it.

**A startup failure looks like a hang, not a crash.** If a container fails while
wiring services, the exception kills the caller but the `TopicWorker` threads
that already started are non-daemon, so the process stays alive with no HTTP
server ever bound — every endpoint refuses the connection. Read the traceback at
the top of the output, not the fact that the process is still running; Ctrl-C to
exit.

- Chat UI: http://localhost:8000/chatui/static/chat.html
- EMISSOR API: http://localhost:8000/emissor
- Storage: http://localhost:8000/storage
- Backend: http://localhost:8000/host

### `test [<module>]` — run tests

If no module follows `test`, ask which one and show the components list.

**Use `integration/venv`, not the component's own venv.** Component venvs install
only that component's own declared requirements; the sibling `cltl.*` packages
are installed into `integration/venv` from `cltl-requirements/leolani`. `make -C
<module> test` uses the component venv and fails with `ModuleNotFoundError: No
module named 'cltl'`.

```bash
cd /workspaces/cltl-dev/<module>
/workspaces/cltl-dev/integration/venv/bin/python -m unittest discover -s tests
```

`integration/venv` has pytest. For the cross-module tests, prefer the harness's
own targets — `make -C integration test` (tier 1) and `test-compose` (tier 2) —
over invoking pytest by hand.

### `test integration` — the cross-module suite

`integration/` composes the modules and asserts the boundaries hold. Tier 1 runs
in this process and needs no Docker; tier 2 runs the real images. **Ask first for
tier 2** — it starts containers and takes about twenty minutes.

```bash
make -C /workspaces/cltl-dev/integration test           # tier 1, ~4 min
make -C /workspaces/cltl-dev/integration docker-images  # says which images are missing
make -C /workspaces/cltl-dev/integration test-compose   # tier 2, ~20 min
make -C /workspaces/cltl-dev/integration test-manual    # needs a person at the keyboard
```

Add `PYTEST_FLAGS="-v --log-cli-level=DEBUG -k <expr>"` to narrow or to get
verbose output. See `integration/README.md`.

---

## Build error triage

| # | Symptom | Cause | Fix |
|---|---|---|---|
| T1 | `make build` succeeds, then `ModuleNotFoundError: cltl` at runtime or in tests | `venv/` exists but `venv/bin/python` is a dangling symlink (e.g. to a removed pyenv install). Make sees the directory newer than `requirements.txt`/`setup.py`/`VERSION` and skips the rule | `rm -rf <component>/venv` to force recreation — **but fix the mirror first (T3/T4), or recreation fails at T2** |
| T1b | A venv holds only `pip`/`setuptools`/`wheel` (~11 entries) but the build reported no error | The `venv:` recipe in `util/make/makefile.py.base.mk` chains its steps with `;`, not `&&`: `source venv/bin/activate; pip install ...; deactivate`. The recipe's exit status is `deactivate`'s, so **a failed `pip install` never fails the build**, and the following `touch venv` marks the target up to date | Re-run the install by hand to see the real error: `cd <c> && source venv/bin/activate && pip install -r requirements.txt --no-index --find-links=../cltl-requirements/mirror --find-links=../cltl-requirements/leolani`. Fix the cause, `rm -rf <c>/venv`, rebuild |
| T2 | `ERROR: No matching distribution found for <pkg>` during venv creation | All installs use `--no-index --find-links=cltl-requirements/mirror --find-links=cltl-requirements/leolani`. With `--no-index` there is no PyPI fallback, so an empty mirror resolves nothing | Rebuild the mirror (T3), then rebuild the component |
| T2b | `fatal error: portaudio.h: No such file or directory`, `Failed building wheel for pyaudio`, or at runtime `ImportError: libGL.so.1: cannot open shared object file` | Source-only wheels and native runtime libraries. `pyaudio` has no aarch64 wheel and compiles against PortAudio headers; `opencv-python` needs libGL at import time; `soundfile` needs libsndfile | `sudo apt-get install -y portaudio19-dev libsndfile1 libasound2-dev libgl1 libglib2.0-0`, then `rm -rf <c>/venv` and rebuild |
| T2c | The build is green and every venv is populated, but a module dies at import with `ModuleNotFoundError` for a package that *is* in the mirror (e.g. `gtts`) | A component imports an optional dependency at module level while the consumer requests an extra that does not include it — `cltl_service.backend.backend_container` imports `cltl.backend.source.local_tts`, which imports `gtts` unconditionally, but `gTTS` lives in cltl.backend's `local`/`remote` extras while `[impl]` is what gets asked for | Add the package to the explicit transitive-dependency block at the bottom of `integration/requirements.txt` (the existing workaround for this class of gap), then rebuild `integration` |
| T3 | `make -C cltl-requirements build` prints `Nothing to be done for 'build'` while `mirror/` is empty | `requirements.lock` is newer than `requirements.txt` but was written by a **failed** download: the recipe pipes pip through `tee \| grep \| cut > lock` with no `pipefail`, so make only sees `cut`'s exit status | `rm cltl-requirements/requirements.lock`, then rebuild the mirror. **Ask first** — multi-GB download |
| T4 | Mirror download dies at `spacy==3.6.1` with `Installing build dependencies: finished with status 'error'` | Wrong interpreter. `spacy==3.6.1`, `torch==1.13.1`, `torchvision==0.14.1`, `tokenizers==0.11.6`, `scipy==1.8.0`, `numpy==1.24.2` have no cp312/cp313 wheels | Use Python 3.10 (`.python-version` says `3.10`; pyenv provides it). Re-run the mirror build with it |
| T4b | The mirror build uses the wrong Python even though `python -V` is 3.10 at the repo root | pyenv resolves the version from the *nearest* `.python-version` walking up from the recipe's working directory. Per-component files can disagree with the root one — `cltl-requirements/.python-version` pinned `3.9` (not installed), so the shim silently fell back to system 3.12 | Check every one: `find . -maxdepth 2 -name .python-version -exec sh -c 'echo "$1: $(cat "$1")"' _ {} \;`. They are gitignored local artifacts — align them on `3.10`, and set a global fallback with `pyenv global 3.10.16` |
| T5 | Files named `a`, `not`, `tty` appear in `cltl-requirements/` | The mirror recipe does `\| tee $(shell tty)`; with no controlling TTY `tty` prints `not a tty`, which the shell word-splits into three redirect targets | Harmless pip logs — delete them. Avoid by running under a pty: `script -qec "make -C cltl-requirements build" /dev/null` |
| T6 | `No rule to make target '<root>/<c>/makefile.d'`, or components build in the wrong order | `base-clean` did `rm -rf makefile.d`; the root makefile `include`s all 12 | `make depend` at root, or `make -C <c> depend` |
| T7 | Everything rebuilds on every invocation | Not an error. `VERSION: $(sources)` — touching anything under `src/` restamps `VERSION` to `<base>+<epoch>`, and `venv: requirements.txt setup.py VERSION`, so venv → dist → py-install → all dependents re-run. Only versions containing `dev` restamp | Nothing to fix. Do not "fix" it by reverting VERSION mid-build |
| T8 | Edits to the root `Makefile` have no effect | GNU make reads `GNUmakefile` → `makefile` → `Makefile` and stops at the first hit. A byte-identical lowercase `makefile` shadows it | Always edit `/workspaces/cltl-dev/makefile` |
| T9 | `make run` / `make stop` fail with `No rule to make target 'run'` or a missing directory | `makefile.parent.mk` does `$(MAKE) --directory=$(project_name) run` with `project_name` quoted literally, and no component defines `run`/`stop` | Dead targets. Use the `run` branch above |
| T10 | Unsure whether a component is supposed to produce an sdist | Every component that includes `makefile.py.base.mk` does: it declares `build: py-install`, and make unions prerequisites with any local `build:` rule | Expect `cltl.<name>-<version>.tar.gz` in `cltl-requirements/leolani/`. If it is absent, the build did not finish. `integration` is the deliberate exception — it does not include that fragment and must never publish itself |
| T11 | `make install` starts building Docker images | `cltl-requirements` has `install: docker`, which builds `ghcr.io/leolani/cltl-base` and `cltl-base-slim`; the root `install` fans out to every component | **Ask first.** `make build` is almost always what was meant |
| T12 | `make -C <c> clean` errors on its last line | `py-clean` ends with an unprefixed `@pip cache remove $(artifact_name)`, which exits non-zero when there is no cache entry | Cosmetic — the `rm -rf`s already ran. Verify with `ls <c>`. Prefer targeted `rm -rf` when cleaning many components at once |

## Repairing a broken environment

Ordered, each step gated on the previous one. Do not skip ahead.

1. Run the health check and report what is broken.
2. If `python -V` is not 3.10, install it before anything else. All pyenv build
   dependencies are present in this container:
   ```bash
   curl -fsSL https://pyenv.run | bash
   # append to ~/.bashrc: PYENV_ROOT, PATH, eval "$(pyenv init -)"
   MAKE_OPTS=-j10 pyenv install -s 3.10.16
   ```
   Then align every `.python-version` (they are gitignored, per-directory, and
   drift independently) and set a global fallback, or make recipes running in a
   subdirectory will silently pick a different interpreter (T4b):
   ```bash
   cd /workspaces/cltl-dev
   for d in . app cltl-requirements; do [ -f "$d/.python-version" ] && echo 3.10 > "$d/.python-version"; done
   pyenv global 3.10.16
   ```
   Non-interactive zsh reads only `~/.zshenv`, so the shims must be on `PATH`
   there — not just in `~/.zshrc` — for tool calls and make recipes to see them.
3. Clear stale state. Use targeted removal rather than root `make clean`, which
   would trip on T12 partway through:
   ```bash
   cd /workspaces/cltl-dev
   rm -rf */venv */dist */build */src/*.egg-info
   rm -rf cltl-requirements/mirror cltl-requirements/leolani cltl-requirements/requirements.lock
   mkdir -p cltl-requirements/mirror cltl-requirements/leolani
   rm -f cltl-requirements/a cltl-requirements/not cltl-requirements/tty
   ```
4. Get explicit approval, then rebuild the mirror under a pty (avoids T5):
   ```bash
   script -qec "make -C /workspaces/cltl-dev/cltl-requirements build" /dev/null
   ls /workspaces/cltl-dev/cltl-requirements/mirror | wc -l   # must be large, not 0
   ```
   If this fails, **stop and report**. Nothing downstream can succeed.
5. Install the system libraries the source-only wheels need — without them
   `app`, `cltl-backend` and `cltl-eliza` fail silently (T1b + T2b):
   ```bash
   sudo apt-get update -qq && sudo apt-get install -y \
     portaudio19-dev libsndfile1 libasound2-dev libgl1 libglib2.0-0
   ```
6. `make build && make build`, then re-run the health check. Confirm no venv is
   `SUSPECT` and that `integration/venv` imports the stack — a green build is not
   evidence on its own.

## Ask before running

- **`make clean` at repo root** — cascades into `cltl-requirements clean`, deleting `mirror/`, `leolani/` and `requirements.lock`. Recovery is a multi-GB download. Offer `make -C <component> clean` instead.
- **Anything that writes under `cltl-requirements/`** — mirror rebuilds are multi-GB and need Python 3.10.
- **`make install` at root** — builds Docker base images (T11).
- **`make git-update`** — runs `git submodule update --remote`, which moves submodules onto the branch declared in `.gitmodules` (`main`). Most submodules here are checked out on `ref_scenarios`, so this silently abandons their work. Never run it as part of a build.
- **`make -C app test-integration*`** — starts Docker Compose stacks.

## Notes

- Use absolute paths or `make -C <dir>`; the shell working directory is not
  stable between tool calls.
- Artifact exchange is a local file index, not PyPI: each component runs
  `python setup.py sdist` into `<component>/dist/`, then `py-install` copies it
  into `cltl-requirements/leolani/`. External dependencies come from
  `cltl-requirements/mirror/`, populated by `pip download`.
  `artifact_name = $(subst cltl-,cltl.,$(project_name))`, so `cltl-asr` ships as
  `cltl.asr`.
- `integration/venv/` is the only environment with all `cltl.*` packages
  installed, and the only one with pytest. Component venvs are build artifacts,
  not runtime or test environments.
- Never activate two venvs in one shell; `deactivate` first.
- The shared make fragments in `util/make/*.mk` are the `leolani/cltl-build` repo
  vendored as a nested `util` submodule inside every component. Editing them
  affects all components and needs `make update-build` to propagate.
- To commit anything built or changed here, use the `submodule-git` skill — it
  ignores the `VERSION` timestamp churn this build generates.
