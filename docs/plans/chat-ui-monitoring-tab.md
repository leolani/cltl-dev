# A Monitoring tab in the chat UI

## 1. Context

The chat UI was a two-column page: the conversation on the left, the image
annotator on the right. Nothing in it answered the question an operator actually
has while watching a conversation — *what did the platform perceive?* Answering
it meant reading the event log, or the persisted EMISSOR scenario after the fact.

`cltl-monitoring` exists to answer exactly that, and already served a web page
for it. The task was to surface that page inside the chat UI as a tab.

Two things stood in the way.

**cltl-monitoring could not run at all.** It was extracted from `cltl-leolani` in
commit `20697b6`, which deleted `src/cltl/friends/` from the repository while
`service.py` kept importing it. It also imported `cltl.object_recognition`,
absent from this workspace too — both declared only as optional
`extras_require`, so `import cltl_service.monitoring.service` raised
`ModuleNotFoundError` on any normal install. On top of that it had no DI
container, its shipped `config/default.config` described `[cltl.leolani]` rather
than the `[cltl.monitoring]` section the code read, and `_create_display` called
`Image.ANTIALIAS`, removed in Pillow 10 (the mirror ships 12.3.0).
`docs/integration-testing-design.md` recorded it as blocked for these reasons.

**It assumed one scenario.** `_text_info`, `_image` and `_display` were three
flat slots on the service, and the two HTTP routes had no way to say which
conversation they were showing. A second scenario silently overwrote the first.

### Decisions taken with the user

| | |
|---|---|
| Layout | Tabs in the right-hand column — `Image` \| `Monitoring` |
| Page content | Image only — the annotated JPEG, nothing else |
| Scenario | The tab follows the chat's own scenario. No picker |
| Scope | Full first-class module: container, config, app wiring, Dockerfile, compose service, integration-harness entry, tests |

## 2. The page is embedded, not reimplemented

An iframe onto the page cltl-monitoring serves, rather than a monitoring view
rendered by `chat.js` against monitoring's JSON.

It is what "serve the web page provided by the cltl-monitoring component" asks
for, but it is also the only option that survives the containerised deployment,
where chat-ui and monitoring are separate images on separate ports and therefore
separate origins. It keeps the view's evolution inside the component that owns
it: adding the utterance line back to that page needs no change here.

The cost is one browser-reachable URL of configuration, `[cltl.chat-ui]
monitoring_url`, with the same failure mode as `image_storage_url`: a value that
resolves on the compose network and nowhere else.

## 3. The design

### 3.1 Per-scenario state

Modelled on cltl-emissor-data, which is the only component in the repo that was
already doing this (`cltl/emissordata/file_storage.py:77-122`). The state moved
out of the service into `cltl/monitoring/`, where it is testable with a dict and
a clock:

```
cltl/monitoring/api.py      TextInfo, Box, immutable Snapshot, MonitoringStore ABC
cltl/monitoring/memory.py   MemoryMonitoringStore — dict keyed by scenario_id, RLock
cltl/monitoring/render.py   annotate() and to_jpeg(), pure Pillow
```

`get()` returns an immutable `Snapshot` holding already-rendered JPEG bytes.
That is not decoration: Flask serves `/scenarios/<id>/image.jpg` on a request
thread while the `TopicWorker` thread is drawing on the very `PIL.Image` that
would otherwise be serialised.

**Buckets are created lazily.** `ScenarioStarted` resets a bucket rather than
being the only thing that opens one. The worker runs
`RejectionStrategy.OVERWRITE`, which discards the *oldest* queued event when its
buffer fills (`topic_worker.py:200-217`), so a `ScenarioStarted` can be dropped
under load — and a store that refused to record without one would lose the whole
conversation rather than its first frame. Eviction is therefore capacity-based
(least-recently-updated first) with `ScenarioStopped` as the fast path.

### 3.2 Two traps in the event handling

- Scenario events are published with `Event.for_payload`, **not**
  `for_scenario_payload` (`cltl-context/.../context/service.py:90`), so
  `event.metadata.scenario_id` is `None` on the scenario topic and only there.
  The id is at `event.payload.scenario.id`. Everything else goes through
  `extract_scenario_id(event, on_missing='ignore')`, which also covers a
  publisher that forgot, via `signal.time.container_id`.
- Dispatch is a string compare against `ScenarioStarted.__name__`, never
  `isinstance`: the kombu bus round-trips payloads through (de)serialisation.

### 3.3 Losing two dependencies without losing the feature

Labels are read structurally — `getattr(annotation.value, 'label', None)` — which
reads a hand-drawn `cltl.chatui.api.ImageAnnotation` and a detected
`cltl.object_recognition.api.Object` identically, because the chat UI made them
field-for-field the same shape on purpose. Across a serialising bus both arrive
as an `emissor.representation.util.PickleableDict`, whose `__getattr__` answers
`.label` too. So cltl-monitoring depends on neither producer — and must not:
declaring `cltl.chat-ui` would make the reading side depend on one writer.

`FriendStore` went entirely; face identities render as their raw id. cltl-context
had already dropped it the same way.

### 3.4 Mentions travel inside the ImageSignal

The chat UI embeds the regions a person drew in `signal.mentions` rather than
publishing them on an annotation topic, and `cltl_service/chatui/schema.py:8-15`
explains why. The old `_update_image` only consumed separate `topic_object` /
`topic_vector_id` events, so in this app every uploaded image would have
rendered with no boxes on it. Both paths are now handled.

### 3.5 The activity gate

Recording used to be gated on someone having polled within `ACTIVE_INTERVAL = 15`
seconds. That exists for a camera-fed deployment, where decoding every frame for
nobody is real work. Here it would mean an image submitted while the tab was not
open is dropped on arrival, with no replay — a Monitoring tab that is empty for
anyone who was not already watching.

It is now `active_interval`, per scenario, and the app sets it to `0`.

### 3.6 HTTP

Scenario-scoped, with cltl-emissor-data's validation and 400/404/500 contract:

| Route | |
|---|---|
| `GET /scenarios` | ids currently held |
| `GET /scenarios/<scenario_id>/image.jpg` | the annotated JPEG, or 404 |
| `GET /scenarios/<scenario_id>/text` | `{"speaker","utterance"}`, or 404 |

Under `/scenarios/` rather than at the root so that nothing collides with
Flask's own `/static/<path:filename>`. No-cache is an `after_request` rather
than a decorator, because Flask 1.1.2 — the version in the mirror — serves
static files with a twelve-hour `max-age` that a decorator would never see.

### 3.7 The page

Rewritten: no jQuery, no CDN, `?scenario=` from `location.search`, a relative
fetch so the mount is not baked in, and a placeholder rather than a broken-image
icon when nothing has been observed yet.

`document.visibilityState` replaces `$(window).blur()`, which was actively wrong
for an embedded page: clicking anywhere in the parent blurs the iframe's window
and would have frozen the view for good. Polling also skips a tick when the
frame has no layout box, which is what an iframe inside a `hidden` tabpanel
looks like from the inside. Both are checks the page makes about itself —
deliberately no `postMessage` protocol, so it stays usable standalone.

### 3.8 The tab strip

`<aside id="side-panel">` holds the strip and two `<section role="tabpanel">`s;
the annotator's markup moved inside one of them unchanged, so every id
`annotate.js` looks up still resolves — including `elements.panel`, captured
since the annotator was written and never read until now.

Three things worth keeping:

- **`#side-panel [hidden] { display: none !important }`.** `#annotator` is
  `display: flex`, and an ID selector beats the user-agent sheet's
  `[hidden] { display: none }`. Without the override the hidden panel renders.
- **The chrome and the height moved to `#side-panel`, not the sections.**
  `#annotate-canvas`'s `flex: 1 1 auto; min-height: 0` only resolves because an
  ancestor has a definite height; that is the 520px matching chat-bubble's fixed
  `.bubble-container`. The sections are `flex: 1 1 auto; min-height: 0`.
- **One panel is not a choice.** With a single enabled panel the strip is
  hidden; with none, the whole column is, and the conversation reflows.

`chat.js` and `annotate.js` are untouched. `panels.js` computes its own REST base
with the same two-segment chop, and polls `GET /chat/scenario` rather than
`/chat/current` — which mints a chat id and resets the inactivity timeout on
every call, so a second poller on it would keep a chat alive for as long as a
browser had the page open.

### 3.9 Config reaches the browser

`GET /chatui/config` → `{"image_upload": bool, "monitoring_url": str|null}`.
This is the first configuration value the chat UI has ever handed to the page.
Until now every panel rendered unconditionally and found out from a 404 that its
feature was off — which is why `_reject_uploads` carried the comment "so the UI
can hide the panel" for a thing the UI could not do.

## 4. Verification

```bash
cd cltl-monitoring && venv/bin/python -m unittest discover -s tests -t .   # 45
cd cltl-chat-ui    && venv/bin/python -m unittest discover -s tests -t .   # 80
make -C integration test                                                   # tier 1
make -C cltl-monitoring docker-ghcr-build
```

End to end, in the app: upload an image in the Image tab, drag two labelled
boxes, Submit, switch to Monitoring — the image appears with both boxes drawn.
`GET /monitoring/scenarios/<other-id>/image.jpg` is a 404, which is the assertion
the single-slot version could never have passed and the one the integration
slice makes.

Static assets install into `app/venv` as **copies**, so every front-end edit
needs `make -C cltl-chat-ui install` (or `-C cltl-monitoring`) before a running
app sees it.

## 5. Found on the way, not fixed

- **The app starts two scenarios per run.** `ContextComponentsContainer.start()`
  calls `self.context_service.start()`, and `ApplicationContainer.start()` calls
  it again on the same instance — a second `TopicWorker` over the same service,
  leaking the first, both handling the `init` intention. Pre-existing and
  independent of this change; visible now because `/monitoring/scenarios` lists
  both. The chat UI reports the later id, so the tab is unaffected.
- **`py-clean` does not remove the sdist it just built.** Its glob is
  `cltl.monitoring-*.tar.gz`, while setuptools now emits
  `cltl_monitoring-*.tar.gz`. The three stale `cltl_chat_ui-*.tar.gz` in
  `cltl-requirements/leolani/` are the same bug.
- **The client/server split does not get the tab.** Monitoring belongs on the
  client side beside chat-ui, but its image fetch would go through
  `[cltl.backend.remote_storage]` there — a different code path, worth its own
  pass.
