---
name: soundfile-dyld-library-path
description: cltl-backend tests require DYLD_LIBRARY_PATH=/opt/homebrew/lib to find libsndfile on macOS
metadata:
  type: feedback
---

When running `cltl-backend` tests on this macOS machine, the `soundfile` Python package (v0.10.x) cannot find its bundled `libsndfile.dylib` because the `_soundfile_data/` directory is missing from the venv's site-packages.

**Fix**: Run pytest with `DYLD_LIBRARY_PATH=/opt/homebrew/lib` — Homebrew's libsndfile is installed at `/opt/homebrew/Cellar/libsndfile/1.2.2_1/lib/libsndfile.dylib`.

**Exact invocation**:
```
DYLD_LIBRARY_PATH=/opt/homebrew/lib \
  /Users/thomasbaier/automatic/vu/workspaces/cltl-dev/app/venv/bin/python -m pytest \
  tests/test_cached_storage.py tests/test_storage_client.py --tb=short -v
```

**Why:** The venv's `soundfile` package was installed without the platform-specific bundled dylib. System-level `libsndfile` is present via Homebrew and works fine when `DYLD_LIBRARY_PATH` is set.

**How to apply:** Any time `cltl-backend` tests (or any test importing `cltl.backend.impl.cached_storage`) fail at collection with `OSError: sndfile library not found`, prepend `DYLD_LIBRARY_PATH=/opt/homebrew/lib` to the pytest invocation.
