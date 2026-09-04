# Front-End Alternatives for the Chat UI

**Research date:** 2026-09-04
**Question asked:** should the chat UI be replaced with a new front end built on
shadcn/ui Chat + Annotorious, or assistant-ui + Annotorious, in order to add
image upload and region annotation?
**Method:** package tarballs pulled from npm and read directly — build outputs,
type declarations and bundle contents — rather than documentation alone.
Repository activity and release history checked at source.

---

## Summary

**Adopt Annotorious. Do not adopt shadcn/ui or assistant-ui.**

The question has two halves and they get opposite answers. Annotorious is a
117 KB UMD bundle that drops into a `<script>` tag and deletes essentially all
of the bespoke annotation work. shadcn/ui and assistant-ui are React component
systems that would introduce a Node toolchain, a bundler and a lockfile into a
repository that has none, to replace 150 lines of vanilla JavaScript.

The two decisions are independent: Annotorious ships a vanilla build precisely
so that it does not force a framework choice.

---

## 1. Annotorious — adopt

`@annotorious/annotorious@3.8.10`, BSD-3-Clause, published 2026-09-01. Five
runtime dependencies, 1.4 MB unpacked, roughly monthly releases.

> GitHub *Releases* stop at v3.4.0. That is the author publishing to npm without
> cutting GitHub releases, not abandonment — the npm publish history is
> continuous.

**The fact the decision turns on:** `dist/annotorious.js` is a UMD bundle —

```js
typeof define=="function"&&define.amd ? define(["exports"],Z) : (R.Annotorious={})
```

— exposing the global `Annotorious`. **117 KB (37.7 KB gzipped) plus 4.7 KB of
CSS, usable from a plain `<script>` tag with no npm, no bundler and no build
step.**

### What it provides

`createImageAnnotator(image, opts)` takes the `<img>` element itself, so a blob
or object URL works. Then `setDrawingTool('rectangle')`, `getAnnotations()`,
`updateAnnotation()`, `removeAnnotation()`, `clearAnnotations()`,
`undo()`/`redo()` (with `Ctrl/Cmd+Z` wired for free), and events
`createAnnotation`, `updateAnnotation`, `deleteAnnotation`, `selectionChanged`.

That covers drag-to-draw, hit-testing, resize handles, selection, coordinate
mapping, responsive rescaling and history — all of the work we would otherwise
have written and then maintained.

### Why it fits EMISSOR

The native model maps onto EMISSOR one-for-one, in source-image pixels:

| Annotorious | EMISSOR |
|---|---|
| `annotation.target.selector.geometry.bounds` = `{minX, minY, maxX, maxY}` | `MultiIndex(signal_id, (x0, y0, x1, y1))` |
| `annotation.bodies[].value` (`purpose: 'commenting'`) | `Annotation.value.label` |

Coordinates are **definitively source-image pixels**, not CSS pixels: the
overlay's `viewBox` is set to `0 0 naturalWidth naturalHeight` and pointer
offsets are divided back through it, so the numbers are independent of CSS size,
zoom and `devicePixelRatio`. A `ResizeObserver` handles rescaling internally.
They are **floats**, so round before building a `MultiIndex`.

### Why it is safe to vendor

The only absolute URLs in the entire bundle are three `w3.org` namespace *string
constants*, never fetched. No telemetry, no licence key, no server, no cookies,
no `localStorage`, no workers, no WASM. BSD-3-Clause needs only the licence text
shipped alongside.

Not loaded from a CDN: cdnjs does not host it (only jsDelivr and unpkg do), and
vendoring is the point — see §3.

### Five details that bite if missed

1. **`autoSave: true`.** Without it, `createAnnotation` does not fire until the
   user deselects the shape, so a freshly drawn box has nothing to label.
2. **Skip the W3C adapter.** v3.8 rectangles carry a user-facing *rotation*
   handle that `AnnotoriousOpts` gives no way to disable, and a rotated
   rectangle silently serialises to an `SvgSelector` instead of
   `xywh=pixel:…`. Reading `geometry.bounds` sidesteps this entirely — it is
   always the axis-aligned box. Hide the handle in CSS as well:
   `.a9s-rotation-handle-group, .a9s-rotation-handle,
   .a9s-rotation-handle-line-fg, .a9s-rotation-handle-line-bg { display: none }`.
3. **`destroy()` and re-create per upload, inside `img.onload`.** The `viewBox`
   is captured at init and does not follow a later `src` swap; and if
   `naturalWidth` is still 0 at init you get `viewBox="0 0 0 0"` and a
   divide-by-zero.
4. **No delete UI and no label popup.** v3 removed the popup by design; both are
   ours to supply. This is the only residual UI work.
5. **Do not install the wrong package.** Unscoped `annotorious` on npm is a dead
   2018 package, and `@recogito/annotorious` is the v2 line with a different
   API — which is what most online tutorials target.

### Rejected within Annotorious

`@annotorious/react` has **no UMD build**: ESM-only, ~25 chunks with unresolved
bare `react` imports. It cannot go in a `<script>` tag, so choosing it means
choosing a bundler — which is §2.

---

## 2. shadcn/ui and assistant-ui — reject

### They are not two options; one contains the other

assistant-ui's own install instructions tell you to run
`npx shadcn@latest add button skeleton dialog …` first. Choosing assistant-ui
means choosing shadcn as well.

### What is actually being replaced

23 lines of HTML and 127 lines of JavaScript, with no dependencies we control.
Against that, React + Tailwind + Vite + TypeScript + a lockfile, in a repository
with no Node toolchain and a team whose expertise is Python.

### shadcn/ui

- `shadcn-chat` (jakobhoeg), the thing usually meant by "shadcn chat", is
  **abandoned** — roughly 13 months without a commit, and its README now points
  elsewhere (AI Elements, prompt-kit).
- shadcn/ui *did* ship first-party chat primitives — `MessageScroller`,
  `Message`, `Bubble`, `Attachment`, `Marker` — in June 2026. They are
  deliberately unopinionated primitives with no runtime, no state and no
  transport; three months old, and their own changelog calls them "the first
  phase". The headless escape hatch `@shadcn/react` currently exports two
  components.
- `Attachment` is a **rendered attachment chip, not a dropzone.** It does no
  file selection, no drag-and-drop and no upload. It would not have done our job.

### assistant-ui

- 0.x, with daily batch releases.
- Ships `assistant-cloud` — the client for a paid hosted service — as a **hard**
  dependency of the OSS package.
- Models messages as a three-value `role` enum, which fights this app's
  multi-speaker model: `chat.js` prefixes `${utt.speaker}> ` precisely because
  there can be more than two participants.
- The correct adapter *would* be `ExternalStoreRuntime`, not `LocalRuntime`: our
  backend is a shared, polled event log where the agent can speak unprompted,
  which request/response `LocalRuntime` has nowhere to put. That adapter is
  ~80–150 lines and would be a faithful port of logic already in `chat.js`.
  React would replace only `groupTurns()` + `toConversationObjects()` — about 35
  lines of glue that exist solely to satisfy chat-bubble's `{says, reply}` API.

### Cost

| Option | Effort | JS payload | Ongoing |
|---|---|---|---|
| Vanilla + Annotorious | ~0.5–1 day | +117 KB | none |
| shadcn/ui | ~3–5 days | 4–6× | Node, npm, Tailwind |
| assistant-ui | ~5–8 days | 4–6× | Node, npm, Tailwind, a 0.x dependency |

`emissor/webapp/` is the cautionary precedent in this repository: an Angular 10
annotation tool from 2020, on an end-of-life framework, unpackaged by
`emissor/setup.py` and wired into nothing.

### Revisit if

We add streaming tokens, tool-call rendering, generative UI or markdown
rendering. That is what assistant-ui is genuinely good at. "A nicer bubble" is
not.

---

## 3. A premise that turned out to be false

"An npm build would break our offline build" was never true, because there is no
offline build to break:

- `cltl-chat-ui/setup.py` overrode `build_py`, `sdist` and `bdist_wheel` to
  download chat-bubble from github.com on **every** build — including inside the
  Docker build, since `requirements.txt` starts with `.`;
- `chat.html` loaded jQuery from `ajax.googleapis.com` at page load.

Both were removed as part of this work, because the new feature would otherwise
have inherited them:

- **chat-bubble is vendored** under `static/chat-bubble/`, with its MIT notice.
  The download hook and the three `cmdclass` overrides are gone. chat-bubble is
  itself unmaintained, so a pinned committed copy is more honest than
  re-downloading a tag on every build.
- **jQuery is gone.** `chat.js` used it for four `$.get`/`$.post` calls and one
  `$(document).ready`; `fetch()` and `DOMContentLoaded` replace it and remove a
  CDN dependency along with ~30 KB.

The page now references only assets this deployment serves, which
`tests/test_service.py::test_the_page_references_only_local_assets` pins down.

---

## Sources

- `@annotorious/annotorious@3.8.10` — npm tarball: `dist/annotorious.js`,
  `dist/annotorious.css`, `dist/*.d.ts`, `package.json`
- <https://annotorious.dev/react/component-reference/>
- <https://github.com/annotorious/annotorious> — releases, licence
- <https://ui.shadcn.com/docs/components>,
  <https://ui.shadcn.com/docs/components/base/attachment>
- <https://www.assistant-ui.com>,
  <https://www.assistant-ui.com/docs/primitives/attachment>
- `github.com/jakobhoeg/shadcn-chat` — commit history, README
- This repository: `cltl-chat-ui/src/cltl_service/chatui/static/`,
  `emissor/webapp/`
