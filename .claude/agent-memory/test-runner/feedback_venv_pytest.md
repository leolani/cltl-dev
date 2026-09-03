---
name: venv-and-pytest-setup
description: Use app/venv for running cltl-backend tests; pytest must be installed there manually
metadata:
  type: feedback
---

The correct venv for running `cltl-backend` tests is `/Users/thomasbaier/automatic/vu/workspaces/cltl-dev/app/venv`. This venv has `cltl.backend` installed (and all other cltl submodule packages).

The local `cltl-backend/venv` does NOT have `cltl` packages installed and should not be used for testing.

`pytest` is not pre-installed in `app/venv` — it must be installed once with:
```
/Users/thomasbaier/automatic/vu/workspaces/cltl-dev/app/venv/bin/pip install pytest
```

**Why:** The project builds into a single shared venv under `app/venv`. Submodule-local venvs are build artifacts, not runtime test environments.

**How to apply:** Always use `app/venv/bin/python -m pytest` when running any cltl-* submodule tests. Check pytest is available before running. See [[soundfile-dyld-library-path]] for the required env var.
