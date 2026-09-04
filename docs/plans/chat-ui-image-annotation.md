# Image upload + region annotation in the chat UI

*The implementation plan for image upload and region annotation in the chat UI,
as executed.* The front-end research it refers to in §2 is written up separately
in [chat-ui-frontend-alternatives.md](../chat-ui-frontend-alternatives.md).

---

## 1. Context

The Eliza chat UI is text-only. `cltl-chat-ui` turns typed text into a
`TextSignal` on `cltl.topic.text_in` and renders replies from
`cltl.topic.text_out`; it has never touched images — an exhaustive grep for
`image|img|upload|blob|base64` across its service, API and JS finds nothing.

The platform already models what we want. EMISSOR's `ImageSignal` carries a
`MultiIndex` ruler — which *is* the bounding box; there is no `BoundingBox`
class — and `Mention`s hang narrower `MultiIndex` segments off it with
`Annotation` values. `cltl-emissor-data` already subscribes to
`cltl.topic.image` and persists both signals and mentions. The only producer of
image signals today is the camera in `cltl-backend`, disabled in this deployment
(`[cltl.backend.image] topic:` is empty).

This puts a human in that loop: a panel beside the chat where someone uploads an
image, drags rectangles over regions, labels each, and submits. The result is a
real `ImageSignal` plus labelled `Mention`s in the EMISSOR scenario next to the
conversation — the same shape
[cltl-object-recognition](https://github.com/leolani/cltl-object-recognition)
produces automatically, so anything that consumes machine annotations consumes
these unchanged.

Precedent worth knowing: `emissor/webapp/` is an Angular 10 EMISSOR annotation
tool that **already does exactly this** — `containers-img` renders draggable,
resizable boxes over an image bound to `MultiIndex.bounds[0..3]`, with
Add Mention / Add Annotation controls. It confirms both the data model and the
coordinate order (left, top, right, bottom). It is also from 2020, on an EOL
Angular, unpackaged by `emissor/setup.py`, and not wired into this app.

### Decisions taken with the user

| | |
|---|---|
| **Submit** | Records to the event bus **and** echoes the image into the chat transcript as a user turn. It must **not** trigger an agent reply. |
| **Per box** | One free-text label. |
| **Scope** | **All component source changes stay inside `cltl-chat-ui`.** No edits to cltl-backend, cltl-emissor-data, emissor or cltl-combot. Config and tests under `integration/` and `app/py-app/config/` are in scope. |
| **Tests** | Unit tests in `cltl-chat-ui/tests/`, a tier-1 integration test, and a manual demo. |

---

## 2. Front-end decision: Annotorious yes, React no

Researched at the user's request. **Adopt Annotorious (vanilla, vendored). Do
not adopt shadcn/ui or assistant-ui.** The two halves of the question are
independent, and they get opposite answers.

### 2.1 Annotorious — adopt

Verified by pulling and inspecting the actual npm package, not just the docs:

- `@annotorious/annotorious@3.8.10`, **BSD-3-Clause**, published 2026-09-01,
  5 runtime deps, 1.4 MB unpacked. Roughly monthly releases. (GitHub *Releases*
  stop at v3.4.0 — the author publishes to npm without cutting them; not
  abandonment.)
- **`dist/annotorious.js` is a UMD bundle** — `typeof define=="function"&&define.amd
  ? … : (R.Annotorious={})` — exposing the global `Annotorious`. **117 KB
  (37.7 KB gzip) + 4.7 KB CSS, usable from a plain `<script>` tag with no npm,
  no bundler, no build step.** This is the fact the whole decision turns on.
- `createImageAnnotator(image: string | HTMLImageElement | HTMLCanvasElement, opts)`
  takes the `<img>` element directly, so a blob/object URL works.
  `setDrawingTool('rectangle')`, `getAnnotations()`, `removeAnnotation()`,
  `undo()`/`redo()` (with `Ctrl/Cmd+Z` wired for free), and
  `on('createAnnotation' | 'updateAnnotation' | 'deleteAnnotation' |
  'selectionChanged', …)`.
- The native model maps **1:1 onto EMISSOR, in source-image pixels**:
  `annotation.target.selector.geometry.bounds = {minX, minY, maxX, maxY}`
  → `MultiIndex(signal_id, (x0, y0, x1, y1))`; the label is
  `annotation.bodies[].value` (`purpose: 'commenting'`), written with
  `anno.updateAnnotation({...a, bodies: [{purpose: 'commenting', value: text}]})`.
- Coordinates are **definitively source-image pixels**, not CSS pixels: the
  overlay's `viewBox` is set to `0 0 naturalWidth naturalHeight` and pointer
  offsets are divided back through it, so the numbers are independent of CSS
  size, zoom and devicePixelRatio. They are **floats** — round before EMISSOR.
  Responsive rescaling is handled internally by a `ResizeObserver`.
- **Clean for a locked-down box**: the only absolute URLs in the entire bundle
  are three `w3.org` namespace *string constants*, never fetched. No telemetry,
  no licence key, no server, no cookies/localStorage/workers/WASM.

This deletes essentially all of the bespoke front-end work: drag-to-draw,
hit-testing, resize handles, selection, coordinate mapping, responsive
rescaling, undo/redo. `annotate.js` shrinks to a file input, a label field, a
delete button, and the Submit mapping.

**Vendor both files** into `static/annotorious/`. `package_data`'s `static/*/*`
packages them, BSD-3 needs only the licence text alongside, and the bundle is
self-contained. Not a CDN: **cdnjs does not host it** (jsDelivr/unpkg only), and
vendoring is the point — see §2.3.

Five operational details that will bite if missed:

1. **`autoSave: true`.** Otherwise `createAnnotation` does not fire until the
   user deselects the shape.
2. **Skip the W3C adapter.** v3.8 rectangles carry a user-facing *rotation*
   handle that `AnnotoriousOpts` gives no way to disable, and a rotated
   rectangle silently serialises to an `SvgSelector` instead of
   `xywh=pixel:…`. Reading `geometry.bounds` sidesteps this entirely — it is
   always the axis-aligned box. Also hide the handle in CSS:
   `.a9s-rotation-handle-group, .a9s-rotation-handle,
   .a9s-rotation-handle-line-fg, .a9s-rotation-handle-line-bg { display: none }`.
3. **`anno.destroy()` and re-create on every new upload, inside `img.onload`.**
   The `viewBox` is captured at init and does *not* follow a later `src` swap;
   and if `naturalWidth` is still 0 at init you get `viewBox="0 0 0 0"` and a
   divide-by-zero.
4. **No delete UI and no label popup** — v3 removed the popup by design. We
   supply both (`anno.removeAnnotation(id)`). This is the residual UI work.
5. **Do not install the wrong package.** Unscoped `annotorious` on npm is a dead
   2018 package; `@recogito/annotorious` is the old v2 line with a different
   API, which is what most online tutorials target.

Rejected within Annotorious: **`@annotorious/react` has no UMD build** — ESM-only,
~25 chunks with unresolved bare `react` imports. It cannot go in a `<script>`
tag, so choosing it means choosing a bundler, which is §2.2.

### 2.2 shadcn/ui and assistant-ui — reject

- The chat UI being replaced is **150 lines total** (23 HTML + 127 JS). This
  would introduce React + Tailwind + Vite + TypeScript + a lockfile where no
  Node toolchain exists, for a component with no dependencies we control.
- **They are not two options; one contains the other.** assistant-ui's own
  install docs tell you to run `npx shadcn@latest add button skeleton dialog …`
  first, so choosing assistant-ui means choosing shadcn too.
- `shadcn-chat` (jakobhoeg), the thing usually meant by "shadcn chat", is
  **abandoned** — ~13 months without a commit, and its README now points
  elsewhere (AI Elements, prompt-kit). shadcn/ui *did* ship first-party chat
  primitives (`MessageScroller`, `Message`, `Bubble`, `Attachment`, `Marker`) in
  June 2026, but they are deliberately unopinionated primitives with no runtime,
  state or transport — three months old, and their own changelog calls them
  "the first phase". The headless escape hatch `@shadcn/react` currently exports
  only two components.
- `Attachment` is a **rendered attachment chip, not a dropzone** — it does not
  do file selection, drag-and-drop or upload. It would not have done our job.
- assistant-ui is **0.x with daily batch releases**, ships `assistant-cloud`
  (the client for a paid hosted service) as a **hard** dependency of the OSS
  package, and models messages as a three-value `role` enum that fights this
  app's multi-speaker model (`chat.js` prefixes `${utt.speaker}> ` because there
  can be more than two participants).
- The correct adapter *would* be `ExternalStoreRuntime`, not `LocalRuntime` —
  our backend is a shared, polled event log where the agent can speak
  unprompted, which request/response `LocalRuntime` has nowhere to put. That
  adapter is ~80–150 lines and is a faithful port of logic already in `chat.js`;
  React would replace only `groupTurns()` + `toConversationObjects()`, ~35 lines
  of glue that exist solely to satisfy chat-bubble's awkward `{says, reply}` API.
- Cost: ~0.5–1 day vanilla vs ~3–5 days (shadcn) or ~5–8 days (assistant-ui),
  plus a 4–6× JS payload and a permanent Node/npm/Tailwind tax on a team whose
  expertise is Python. `emissor/webapp/` is the cautionary precedent.
- **Revisit if** we add streaming tokens, tool-call rendering, generative UI or
  markdown rendering. That is what assistant-ui is genuinely good at. "A nicer
  bubble" is not.

### 2.3 Correction to a premise, worth acting on

The build is **not** offline today. `setup.py`'s `build_py`/`sdist`/`bdist_wheel`
overrides download chat-bubble from github.com on **every** build — including
inside the Docker build, since `requirements.txt` starts with `.` — and
`chat.html` loads jQuery from `ajax.googleapis.com` at page load. So "an npm
build would break our offline build" was never true: there is no offline build
to break.

Two cheap fixes, in scope because they touch only `cltl-chat-ui`, each removing
a network dependency the new feature would otherwise inherit:

- **Vendor chat-bubble**: commit `static/chat-bubble/`, delete
  `fetch_chat_bubble` and the three `cmdclass` overrides. chat-bubble is itself
  unmaintained, so a pinned committed copy is more honest than re-downloading a
  tag every build. Ship its MIT notice.
- **Vendor or drop jQuery**: `chat.js` uses it for four `$.get`/`$.post` calls
  and one `$(document).ready`. `fetch()` + `DOMContentLoaded` replaces it and
  removes ~30 KB and a CDN dependency. `annotate.js` should use `fetch` from the
  start regardless.

---

## 3. The design

### 3.1 Where the bytes live

Three copies, each with one job:

| Copy | Owner | Purpose | Lifetime |
|---|---|---|---|
| `File` / `createObjectURL` | browser | drives the annotation editor | until reload |
| raw bytes, in-memory | `MemoryImageStore` in chat-ui | serves the chat-echo `<img>` | until eviction or `ScenarioStopped` |
| RGB pixels | cltl-backend's `ImageStorage`, via the **existing** `PUT /storage/image/<id>` | makes `cltl-storage:image/<id>` resolvable so cltl-emissor-data copies the PNG into the scenario | the deployment's |

`cltl-emissor-data`'s `_destination_path` does `url.replace('cltl-storage:', '')`
and joins the remainder onto the scenario dir, so an `http://…` URL yields
`<emissor>/<scenario>/http:/host:8000/chatui/image/<id>.png` and writes
chat-ui's host:port into the persisted `signal.files`. `cltl-storage:image/<id>`
is the only shape that works — hence the best-effort PUT to an existing public
endpoint (an HTTP call, not a code dependency).

Two things that are easy to get wrong there, both verified against
`cltl.backend.api.serialization`:

- **`view` must be a dict, not a list.** `image_hook` tries `Bounds(**view)`
  first and falls back to `Bounds(*view)`, and `Bounds`' *field* order is
  `(x0, x1, y0, y1)` — not diagonal. `{"x0": 0, "x1": w, "y0": 0, "y1": h}`
  takes the first branch and cannot be misread.
- **`depth` must be present and `null`** — `image_hook` indexes it directly.

Config resolution reads `[cltl.chat-ui] image_storage_url` and falls back to
`[cltl.backend] storage_url`, which guarantees chat-ui and cltl-emissor-data
resolve `cltl-storage:` against the same endpoint. Guard the fallback on the
*manager*: `LocalConfig.__contains__` delegates to `ConfigParser.has_option`,
which raises `NoSectionError` when the section itself is missing.

```python
storage_url = None
if "image_storage_url" in config:                     # config = [cltl.chat-ui]
    storage_url = config.get("image_storage_url")
if not storage_url and "cltl.backend" in config_manager:
    backend = config_manager.get_config("cltl.backend")
    storage_url = backend.get("storage_url") if "storage_url" in backend else None
```

Publish `files=["cltl-storage:image/<id>"]` **unconditionally**, even when the
PUT is disabled: `_store_image_files` swallows the download failure and still
registers the signal, so mentions persist either way and the record keeps its
reference. Only the copied PNG is lost.

chat-ui's own copy is in memory — a UI cache, not a system of record — bounded
(`image_cache` × `image_max_size`, default 4 × 10 MiB), insertion-ordered FIFO
via `collections.OrderedDict` (the transcript is chronological, so the oldest is
the one that scrolled away), cleared on `ScenarioStopped`. Not a temp dir: the
compose `chatui` service mounts the *same* host `${CLTL_STORAGE_DIR}` as every
other service, so a disk store there collides by construction.

### 3.2 Event sequence — one event, mentions embedded

```
POST /chat/<c>/image                 → store bytes, mint image_id.  No event.
POST /chat/<c>/image/<i>/annotations → best-effort PUT to /storage/image/<i>
                                       build ImageSignal(files=["cltl-storage:image/<i>"],
                                                         bounds=(0,0,w,h),
                                                         mentions=[Mention per box])
                                       publish ImageSignalEvent on topic_image
                                       self._chats.append(echo Utterance)   ← no event
```

Not the two-event (signal, then `AnnotationEvent`) shape that
cltl-object-recognition uses, for a reason verified in the source:
`KombuEventBus.subscribe` creates **one `_EventBusConsumer` thread per topic**
(`kombu.py:120-133`), so cross-topic ordering is not guaranteed; and
`_add_mention` resolves `mention.segment[0].container_id` against
*already-registered* signals and drops the mention with only a warning
otherwise. Two topics is a race that fails silently in tier 2 only. One event
needs no argument at all. cltl-object-recognition splits them because a
*different process* annotates a signal the backend already published — here
chat-ui owns both halves, and `TextSignalEvent.for_speaker` is the local
precedent for embedding a mention before publishing.

`signal.time.end` must be truthy or `add_signal` sets `stored_files = []` and
never downloads — submit time is the only moment we honestly have both ends.

Cost, worth noting: `_add_mention` never runs, so `_signal_idx` never learns the
mention/annotation ids and `GET /emissor/<mention_id>/scenario/id` 404s for them.

**No `TextSignalEvent` on `topic_utterance`** — that is what keeps cltl-eliza
silent. The echo is a direct `self._chats.append(...)`, exactly what
`post_utterances` already does *before* it publishes; we just omit the publish.

### 3.3 Bounds

`(0, 0, width, height)` from the source image — **never** from
`cltl.backend.api.camera.Image.bounds`, which goes through
`CameraResolution(shape[:2])` and falls back to `NATIVE = (-1, -1)` for any
resolution not in the enum, i.e. essentially every upload. We do not import that
module at all, which also removes the `Bounds` field-order trap; clipping is
plain arithmetic.

`MultiIndex.get_area_bounding_box` **raises `ValueError`** outside the parent
rather than clipping, so `Region.normalized(width, height)` orients (a
right-to-left drag), rounds Annotorious's floats, clamps, and returns `None` for
a degenerate box — one bad box is dropped, the submission still succeeds.

### 3.4 Decoding: cv2, guarded, declared by nobody

`cv2` is provided by **two differently named distributions**: `opencv-python`
(app and integration venvs; the only one in `cltl-requirements/mirror/`) and
`opencv-python-headless` (in `cltl-base-slim`, which chat-ui's image is built
on). Declaring either breaks the other environment's `--no-index` install. So:
guarded import inside the function, and **neither** name in `setup.py` — the
precedent is `cltl-backend/src/cltl/backend/impl/cached_storage.py`.

**Pillow is not an option**: it is not in `requirements.slim.txt`, and the
Docker build's `--find-links=/leolani` sees only CLTL sdists, so adding it
hard-fails `make -C cltl-chat-ui docker-ghcr-build`.

Decoding is only needed for the best-effort storage PUT — the browser supplies
`naturalWidth`/`naturalHeight`, and the echo serves the original bytes unchanged.

### 3.5 HTTP API

| Method | Path | Request | Response |
|---|---|---|---|
| `POST` | `/chat/<chat_id>/image?width=<w>&height=<h>` | raw bytes, `Content-Type: image/*` | `201 {"id","url","width","height","content_type"}` |
| `GET` | `/chat/<chat_id>/image/<image_id>` | — | `200` raw bytes, original mimetype; `404` |
| `POST` | `/chat/<chat_id>/image/<image_id>/annotations` | `{"regions":[{"x0","y0","x1","y1","label"},…]}` | `200 {"signal_id","utterance_id","mentions"}` |
| `DELETE` | `/chat/<chat_id>/image/<image_id>` | — | `204` / `404` |

Raw bodies and query params, matching `post_utterances`' `get_data(as_text=True)`
and `?from=` / `?speaker=`. `width`/`height` are **required** — they become the
signal bounds, and a wrong size makes every segment silently wrong, so refuse
rather than guess. `MAX_CONTENT_LENGTH` gives 413 for free. No scenario ⇒
**409, not 500** (the existing text path lets a bare `ValueError` become a 500;
the new routes must not copy that).

### 3.6 New and changed types

`src/cltl/chatui/api.py`: `ImageAnnotation(type, label, confidence)` — shaped
after `cltl.object_recognition.api.Object` so consumers already reading
`annotation.value.label` work unchanged; `Region(x0, y0, x1, y1, label)` with
`normalized(w, h)`; `StoredImage`; `ImageStore` ABC. Plus **one trailing,
defaulted field** on `Utterance`: `content_type: str = "text/plain"` (trailing
because `sequence` has no default; and `MemoryChats` mutates
`utterance.sequence`, so `Utterance` stays a plain mutable dataclass).

`src/cltl/chatui/memory.py`: `MemoryImageStore`.

`src/cltl_service/chatui/schema.py` (new — chat-ui has none today): `image_url`,
`create_image_signal`, `to_mention`, and an `ImageAnnotationEvent` kept for the
two-event shape so tests can build mentions without a signal event.
`STORAGE_SCHEME = "cltl-storage"` is duplicated rather than imported: it is a
wire contract, not an API, and importing it would mean depending on
`cltl.backend`.

`container.py`: an `image_store` singleton returning **`False`** when disabled
(`@singleton` cannot hold `None`; callers test truthiness). No new base class,
so `eliza_chatui` / `text_pipeline` / `csplit_client` are unaffected. Name it
`image_store`, not `image_storage` — `DIContainer._singletons` is keyed by
method name across the whole process, and `image_storage` would collide with
cltl-backend's.

### 3.7 Serialisation across processes

`Annotation.value` is the registered TypeVar `T`, so it goes through emissor's
`GenericField`, tagged `_py_type: "cltl.chatui.api-ImageAnnotation"`. In tier 2,
`cltl-emissor-data`'s image installs `cltl.emissor-data[service,client]` and
**not** `cltl.chat-ui`, so that lookup fails and the value degrades to a
`PickleableDict`. `annotation.value.label` still works by attribute access and
the record still persists; `Annotation.type` is a plain `str` and survives, so
`class_type(ImageAnnotation)` stays the reliable discriminator. The one-line fix
(adding `cltl.chat-ui` to cltl-emissor-data's requirements) is out of scope —
flagged, not done.

### 3.8 Config

`app/py-app/config/default.config` and `integration/config/base.config`:

```ini
[cltl.chat-ui]
image_upload: True
image_cache: 4
image_max_size: 10485760
image_types: image/png, image/jpeg, image/gif, image/webp
image_storage_url:            # empty ⇒ fall back to [cltl.backend] storage_url

[cltl.chat-ui.events]
topic_image: cltl.topic.image  # already in [cltl.emissor-data.event] topics
```

`cltl-chat-ui/config/default.config` is currently missing `timeout` and
`topic_scenario`, so `python src/main.py` crashes before serving anything — fix
that while adding the new keys, and keep `image_upload: False` there, because
its `external_input: False` makes `get_utterances` default to the agent's name
and **filters the echo out of the transcript**.

New `integration/config/topologies/chatui_image.config` carries
`flush_interval: 0` and the storage URL; the backend is in that topology
precisely so the test can tell "pixels lost" apart from "annotations lost".

### 3.9 Tests

`make -C cltl-chat-ui test` runs **`python -m unittest`**, which discovers only
`TestCase` subclasses — bare pytest functions are collected by nothing. And
`tests/test_service.py` is **already red** (obsolete `ChatUiService(...)`
signature), so step 0 is repairing it, or new failures are indistinguishable
from the standing ones.

- `tests/test_schema.py` — bounds, `files`, both time ends truthy; segment
  `container_id == signal.id`; reversed drag normalised; overhanging box clamped
  with no `ValueError`; outside/zero-area dropped while others survive;
  `annotation.type == class_type(ImageAnnotation)`; and an
  `unmarshal(marshal(signal))` round trip, which is literally the first thing
  `add_signal` does.
- `tests/test_image_store.py` — FIFO eviction, `remove`, `clear`.
- `tests/test_image_service.py` — byte-identical `GET`; 415/400/413; exactly one
  event on `topic_image`; **nothing on `topic_utterance`** (the Eliza-silence
  regression); echo present with `content_type == "text/html"` and the literal
  `<img src=`; a `"><script>` label escaped; 409 not 500 with no scenario; the
  PUT body asserted field by field (`view` a dict, `depth` present and `None`,
  `shape == [h, w, 3]`); `ScenarioStopped` empties the store.
- `integration/`: new `CHATUI_IMAGE` topology (`chatui`, `emissor`, `backend`) —
  which also gives `make -C integration demo-chatui-image` for free, since the
  launcher is generic over the registry; `ChatClient.upload_image/fetch_image/
  annotate`; a `drivers/image.py` PNG helper (no image fixtures exist in the
  repo — the audio tests synthesise theirs too; and `images.py` at package root
  is the docker-image checker, so don't collide with that name);
  `tests/slices/test_chatui_image.py` asserting the signal, the mentions, the
  silence on `cltl.topic.text_in`, and — **separately** — that
  `<emissor>/<scenario>/image/<id>.png` exists, which is the only thing that
  proves the whole PUT → storage → download chain; and a manual test that
  cross-checks what the person left behind so a distracted "y" cannot pass it.

---

## 4. File-by-file, in order

**`cltl-chat-ui/` — the only component source that changes**

| # | Path | Action |
|---|---|---|
| 1 | `tests/test_service.py` | **Repair or delete** — red before we start |
| 2 | `src/cltl/chatui/api.py` | ADD `ImageAnnotation`, `Region`, `StoredImage`, `ImageStore`; ADD `Utterance.content_type` |
| 3 | `src/cltl/chatui/memory.py` | ADD `MemoryImageStore` |
| 4 | `src/cltl_service/chatui/schema.py` | **NEW** |
| 5 | `tests/test_schema.py`, `tests/test_image_store.py` | **NEW** — pass before any service code exists |
| 6 | `src/cltl_service/chatui/service.py` | ADD 4 routes + `_echo_utterance`, `_upload_to_storage`, `_decode_rgb`; `MAX_CONTENT_LENGTH`; clear store on `ScenarioStopped` |
| 7 | `src/cltl_service/chatui/container.py` | ADD `image_store` |
| 8 | `tests/test_image_service.py` | **NEW** |
| 9 | `setup.py`, `requirements.txt` | ADD `requests` only |
| 10 | `config/default.config` | ADD image keys; FIX missing `timeout` / `topic_scenario` |
| 11 | `static/annotorious/` | **NEW (vendored)** — `annotorious.js` (UMD) + `annotorious.css` from `@annotorious/annotorious@3.8.10`, plus its BSD-3 `LICENSE`. Never under `static/chat-bubble/` |
| 12 | `static/annotate.js` | **NEW** — file input → `POST …/image`; inside `img.onload`, `destroy()` any previous annotator then `createImageAnnotator(img, {autoSave: true, drawingMode: 'drag', userSelectAction: 'EDIT'})` + `setDrawingTool('rectangle')`; label field writes `bodies: [{purpose:'commenting', value}]`; delete button calls `removeAnnotation(id)`; Submit maps `getAnnotations()` → `regions` (rounding `geometry.bounds`) and `POST …/annotations`. `fetch`, not jQuery |
| 13 | `static/annotate.css` | **NEW** — two-column layout; `.bubble-container{margin:0}` to undo `setup.css`'s `margin: 0 auto`; hide the four `.a9s-rotation-handle*` classes; `.cltl-annotated`/`.cltl-box` echo styles |
| 14 | `static/chat.html` | MODIFY — `#workspace` wrapper + `#annotator` markup; link `annotorious/annotorious.css`, `annotate.css`; script `annotorious/annotorious.js`, `annotate.js` |
| 15 | `static/chat.js` | MODIFY — publish `window.cltlChat = {restPath, chatId, agentId}`; route `content_type === "text/html"` utterances to the `says` array |

**Front-end hygiene (same submodule; removes network dependencies the feature would inherit)**

| # | Path | Action |
|---|---|---|
| H1 | `static/chat-bubble/` + `.gitignore` + `setup.py` | Commit the vendored copy; delete `fetch_chat_bubble` and the three `cmdclass` overrides; ship the MIT notice |
| H2 | `static/chat.html` / `chat.js` | Vendor jQuery locally, or drop it — four `$.get`/`$.post` calls and one `$(document).ready` |

**App and harness (no component source)**

| # | Path | Action |
|---|---|---|
| 16 | `app/py-app/config/default.config` | ADD the image keys + `topic_image` |
| 17 | `app/py-app/clean_storage.sh` | ADD `mkdir -p storage/image` — `CachedImageStorage` creates the *parent* of its storage path, so the first PUT 500s on a fresh checkout |
| 18 | `integration/config/base.config`, `tier-compose.config`, `topologies/chatui_image.config` | ADD keys / **NEW** overlay |
| 19 | `integration/src/cltl_integration/topology.py`, `drivers/chat.py`, `drivers/image.py` | ADD `CHATUI_IMAGE`, client methods, PNG helper |
| 20 | `integration/tests/slices/test_chatui_image.py`, `tests/manual/test_chat_ui.py` | **NEW** / ADD |
| 21 | `docs/plans/chat-ui-image-annotation.md` | **NEW** — this plan (`docs/plans/` must be created) |
| 22 | `docs/chat-ui-frontend-alternatives.md` | **NEW** — §2, in the format of `docs/framework-alternatives-research.md` |

---

## 5. Verification

```bash
make -C cltl-chat-ui build && make -C cltl-chat-ui test
make -C integration build && make -C integration test -- -k chatui_image

make build && make build          # twice: first builds deps, second links
cd app && source venv/bin/activate && cd py-app && python app.py
# → http://localhost:8000/chatui/static/chat.html
#   upload, drag two boxes, label, Submit
ls app/py-app/storage/emissor/*/image.json app/py-app/storage/emissor/*/image/

make -C integration demo-chatui-image
```

`make build` exiting 0 is **not** evidence the build worked — the `venv:` recipe
in `util/make/makefile.py.base.mk` chains with `;` not `&&`, so a failed
`pip install` still touches the target. Use the `build` skill. And installs into
`app/venv` are **copies, not editable**: every static-asset edit needs
`make -C cltl-chat-ui install` before it reaches a running app.

---

## 6. Traps, ranked

1. `view` as a list in the storage PUT — `Bounds(*view)`'s field order is `(x0, x1, y0, y1)`, not diagonal. Send a dict.
2. Rich HTML through `chat.js`'s `reply` path — `Bubbles.js` interpolates user text into an `onClick` attribute. The `says` path is the only safe one, and `innerHTML = say` means every label must be escaped server-side.
3. Cross-topic ordering under Kombu — one consumer thread per topic; `_add_mention` drops silently. Never split a signal from its mentions across two topics.
4. `app/py-app/storage/image` does not exist ⇒ the first PUT 500s; survivable only because `store` fills the LRU before `_write`.
5. `Image.bounds` for uploads is `(0, 0, -1, -1)`.
6. Pillow breaks the Docker build; neither opencv name can be declared safely.
7. `get_area_bounding_box` raises, it does not clip.
8. `external_input: False` filters the echo out of `GET /chat/<id>`.
9. `make test` runs `python -m unittest` — use `TestCase`.
10. `"key" in config` raises `NoSectionError` for a missing *section*.
11. `static/chat-bubble/` is deleted and re-downloaded on every build.
12. `restPath` chops exactly two path segments — scripts must sit directly in `static/`.
13. Tier 2 loses `_py_type` on the annotation value; `Annotation.type` survives as the discriminator.
14. Annotorious captures the `viewBox` at init — `destroy()` and re-create per upload, inside `img.onload`.
15. Annotorious's rectangle rotation handle cannot be disabled by option; read `geometry.bounds` (never the W3C `FragmentSelector`) and hide the handle in CSS.
16. Annotorious emits **floats** — round before building the `MultiIndex`.
17. Without `autoSave: true`, `createAnnotation` does not fire until deselect.

---

## 7. What changed during implementation

The plan above is the plan as approved. Nine things turned out differently once
it met the code; this section is the reconciliation.

1. **`make test` in cltl-chat-ui was running zero tests.** `tests/` had no
   `__init__.py`, and a bare `python -m unittest` silently skips a directory
   that is not an importable package — so the "already red" `test_service.py`
   in §3.9 had in fact never been run by anything. Adding `tests/__init__.py`
   is what makes the suite exist; repairing `test_service.py` came second.
   `tests/support.py` holds the shared harness (a started service, a
   synthesised PNG, an event listener, and the waits that keep a test off the
   `TopicWorker`'s thread).

2. **The topology overlay does not set a storage URL.** §3.8 proposed putting
   one in `config/topologies/chatui_image.config`. Leaving
   `[cltl.chat-ui] image_storage_url` empty is better: it falls back to
   `[cltl.backend] storage_url`, which is the value cltl-emissor-data resolves
   `cltl-storage:` against and which each tier config already sets correctly.
   One value to keep right instead of two.

3. **`[cltl.emissor-data] flush_interval: 0` added to the app config.** Not in
   the plan, and not optional: the library default of -1 keeps every signal in
   memory until a clean scenario stop, so in the app a submitted annotation
   reached `storage/emissor/<scenario>/image/<id>.png` but never appeared in
   `image.json`. The integration harness already sets this, for the same reason
   its own comment gives.

4. **`[app.context]` renamed to `[eliza.context]` in the app config.**
   `ContextService.from_config` reads `eliza.context`; the section was named
   `app.context`, so `python app.py` raised `NoSectionError` before serving
   anything. This is the config half of the mismatch CLAUDE.md recorded as a
   pending fix, and without it none of this feature can be run in the app. The
   *other* half — `ContextService.stop()` calling `stop()` on a `None` topic
   worker, which aborts the container shutdown chain — is still open and is
   still recorded in CLAUDE.md.

5. **jQuery was dropped, not vendored.** H2 allowed either. `chat.js` now uses
   `fetch` and `DOMContentLoaded`, so the page references only assets this
   deployment serves — pinned by
   `tests/test_service.py::test_the_page_references_only_local_assets`.

6. **`annotorious.js` ships with its `sourceMappingURL` comment stripped.** The
   `.map` file is not vendored, and the comment makes every browser with
   devtools open request a file that is not there.

7. **`ImageAnnotationEvent` exists but nothing publishes it.** As designed in
   §3.6 — it is the two-event shape, kept so that a future consumer that does
   need mentions on their own topic has the payload to hand, and so a test can
   build mentions without a signal event.

8. **`clean_storage.sh` creates all four storage directories**, not only
   `image/`. `CachedAudioStorage` has the same "creates the parent" behaviour as
   `CachedImageStorage`, so the audio directory is the same trap one component
   over.

9. **The tier-1 slice covers the upload lifecycle too** — that the bytes come
   back unchanged, that uploading publishes nothing at all, and that an upload
   can be discarded — not only the submit path §3.9 described.

### Verified

| | |
|---|---|
| `make -C cltl-chat-ui test` | 74 tests, green (2 skip without cv2, which is optional there) |
| `make -C integration test` | tier 1 green, including 9 new `test_chatui_image` tests |
| `make -C cltl-chat-ui docker-ghcr-build` | image builds; every vendored asset present inside it; `cv2` (headless) and `requests` import |
| `python app.py` | upload → annotate → `image.json` with two mentions, the third region correctly dropped, the PNG copied into the scenario folder, and the pixels in `storage/image/` |
| `make -C integration demo-chatui-image` | topology comes up and serves the page |

Not run here: tier 2 (`make -C integration test-compose`, ~18 min) and the
manual tests, which need a person at the keyboard.
