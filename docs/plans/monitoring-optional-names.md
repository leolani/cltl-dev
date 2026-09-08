# Optional name resolution in cltl-monitoring

Status: implemented.

## The problem

The monitoring page draws a labelled box over every annotated region. For a face
the label was the bare vector identity — `face-1` — because resolving it to a
person's name needs `cltl.friends.api.FriendStore`, and that import was removed
when the component was made scenario-aware (see
[chat-ui-monitoring-tab.md](chat-ui-monitoring-tab.md)).

The removal was not incidental. At `20697b6` the component imported
`cltl.friends.api` and `cltl.object_recognition.api` at module level while
declaring both only in `extras_require`, so `import
cltl_service.monitoring.service` raised `ModuleNotFoundError` on any normal
install. The component was unimportable, which is why it was not wired into the
app at all.

We want the name back when a deployment has a source of names, without
reintroducing that.

## What the dependency actually is

Three facts, established before designing anything:

1. **`cltl.friends` is not a package you can install on its own.** It ships
   inside the **`cltl.leolani`** distribution (`github.com/leolani/cltl-leolani`),
   the repo cltl-monitoring was forked out of at `20697b6`. Its `setup.py` there
   pulls `cltl.brain`, `cltl.object-recognition`, `cltl.face-recognition`,
   `cltl.triple_extraction` and `cltl.reply_generation`.
2. **It cannot be installed in this repo.** `cltl-requirements/leolani/` is
   populated only by this repo's own submodules building their sdists
   (`util/make/makefile.py.base.mk:70`), and every venv installs `--no-index`
   against it plus the third-party mirror. There is no `cltl-leolani` submodule
   and adding one would require five more.
3. **The whole dependency is one method.** The old consumer
   (`20697b6^:src/cltl_service/monitoring/service.py:194-205`) called
   `FriendStore.get_friend(face_id) -> (uri, names)` and read `names[0]`,
   discarding the uri.

A capability whose entire surface is "give me a name for this id" does not
justify a five-package dependency chain on every deployment.

## The design

**cltl-monitoring declares the protocol and imports nothing.**

`cltl/monitoring/api.py` gains `NameResolver` — one method, `name_for(identity)
-> Optional[str]` — and a `Person` record. Returning `None` is an ordinary
answer: an unrecognised face and a deployment with no name source are the same
thing from here.

**The adapter lives in a module nothing imports by default.**

`cltl_service/monitoring/friends.py` is the only file that mentions
`cltl.friends`, and it imports it at module top level on purpose. This is the
`cltl-asr` shape: `cltl/asr/whisper_asr.py` imports `whisper` at its top level
and `ASRContainer` defers importing *the module* until configuration asks for it
(`cltl-asr/src/cltl_service/asr/container.py:36-57`). `service.py` and
`container.py` stay clear of it, which is the property that keeps the component
installable.

**`src/main.py` decides what is available.**

`src/main.py` is a top-level module, so
`find_namespace_packages(include=['cltl.*', 'cltl_service.*'], where='src')`
does not package it — only the container image runs it. That makes it the right
place to know that `cltl.friends` might exist, while the installed distribution
knows nothing about it.

It gates twice, in this order:

- `if "cltl.friends" not in self.config_manager` — configuration first, so an
  image that ships with the package can still leave names off. Same
  `implementation:`-means-off convention as `[cltl.asr]`.
- `try: from cltl_service.monitoring.friends import ...` — the import second, so
  an image built without the package logs a line rather than failing to start.

`MonitoringContainer.monitoring_name_resolver` returns `False` by default —
`@singleton` cannot hold `None`, and the property name is prefixed because
`DIContainer._singletons` is keyed by method name process-wide. A deployment
that has a name source overrides that one property.

`app/py-app/app.py` gets **no** override: `cltl.leolani` can never be installed
in this repo's venv, so the code would be unreachable.

### Why not the alternatives

- **`extras_require` + a lazy import in the container.** This is what the
  component did before, and the extra is not what broke it — the unconditional
  module-level import was. A `friends` extra is still declared, but only to
  record the pairing; nothing in the build asks for it and it cannot resolve
  against the offline mirror.
- **Config naming the class, resolved with `importlib`.** More flexible and
  stringly-typed: a typo fails at startup rather than at import, and the
  constructor arguments still have to come from somewhere.
- **An HTTP call to a public endpoint**, the way cltl-chat-ui reaches
  cltl-backend (`chatui/service.py:454-466`). Nothing exposes friends over HTTP.
  If something ever does, it is another `NameResolver` and nothing else moves.

## What the page does with it

`Snapshot` gains `people`, with the same lifecycle as the boxes — cleared by
`set_image`, added to by the identity annotations that follow it. Carrying
people across a frame would present who *was* there as who is.

`GET /scenarios/<id>/people` returns `{"people": [{"identity", "name"}]}`, 200
with an empty list when nobody is in frame (the page polls it; "nobody" is an
answer) and 404 only for a scenario that is not held.

The page renders a block below the image — below, not beside, because the same
page is a standalone tab and a 520px iframe in the chat UI. The block is
**data-driven**: it appears when there is somebody to show. No `/config`
endpoint and no capability flag, so there is no key-to-DOM `if` chain to keep in
step — the thing that makes `panels.js:73-79` awkward to extend. An entry with
no resolved name shows its identity, so the block is informative with no
resolver at all.

`cltl-chat-ui` needed no change: it embeds this page whole.

## Failure behaviour

`NameResolver` asks implementations not to raise, and the service does not
believe them. `MonitoringService._resolve` catches everything and falls back to
the identity, because the implementation is supplied by the deployment and
reaches a knowledge graph over the network — and this runs on the `TopicWorker`
thread, where an escaping exception abandons the rest of the frame's
annotations.

`FriendStoreNameResolver.name_for` catches too, because the two shipped stores
fail differently for the same input: `MemoryFriendsStore.get_friend` subscripts
a dict and raises `KeyError` for a face it has not seen, while
`BrainFriendsStore` answers `(None, None)` and raises only when GraphDB is
unreachable.

`CachingNameResolver` caches **hits only**. A face is routinely seen before
anyone says whose it is, and caching a miss would keep it anonymous for the life
of the process.

## What is not verified

The `FriendStoreNameResolver` branch. It cannot be imported, run or tested in
this repo, and its correctness rests on `get_friend` returning `(uri, names)` as
read from `20697b6^:src/cltl/friends/api.py`. Everything else — the absent path,
the fallbacks, the endpoint, the store — is covered by tests against fakes.
