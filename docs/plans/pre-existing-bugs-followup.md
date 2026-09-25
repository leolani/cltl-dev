# Pre-existing bugs found while adding the monitoring tab

Three bugs surfaced during the chat UI monitoring work (see
`docs/plans/chat-ui-monitoring-tab.md`). None of them were caused by that
change and none were fixed by it — the feature branch was kept to its own
scope. They are written down here so they can be picked up separately.

They are independent of one another and can be fixed in any order. **Bug 1 is
the only one that affects a running system**; bugs 2 and 3 are cosmetic and
local.

---

## 1. `ContextService` is started twice, so the app opens two scenarios per run

**Severity:** real, and it silently corrupts every run.

### Symptom

Every launch of `app/py-app/app.py` opens **two** scenarios instead of one, and
every shutdown ends with

```
AttributeError: 'NoneType' object has no attribute 'stop'
```

The second symptom is already noted in `CLAUDE.md`, but attributed there to
"shutting the app down before `ContextService.start()` has run — or after a
failed start". That is not what happens: it fires on every clean shutdown, and
both symptoms have the same single cause.

The first symptom was invisible until now. It became visible because
`GET /monitoring/scenarios` lists the scenarios the monitoring component is
holding, and it lists two after a fresh start.

### Cause

`ContextService.start()` is called twice on the same instance.

- `cltl-context/src/cltl_service/context/container.py:47` —
  `ContextComponentsContainer.start()` calls `self.context_service.start()`.
- `app/py-app/app.py:93` — `ApplicationContainer.start()` calls
  `self.context_service.start()` **again**, right after `super().start()` at
  line 92 has already run the container chain that includes the call above.

`ApplicationContainer` overrides the `context_service` property
(`app/py-app/app.py:75-77`) to supply the app's own `ContextService`, and
`@singleton` means both call sites get the *same* object. The override was
presumably meant to *replace* the container's service — the docstring at
`cltl-context/src/cltl_service/context/container.py:17-18` says exactly that:
"An application that needs an application-specific `ContextService` overrides
the `context_service` property; the one provided here is then unused." But
overriding the property does not remove the `start()` call in the base
container, so it is started by both.

`ContextService.start()` (`app/src/eliza_app_service/context/service.py:50-56`)
unconditionally assigns a fresh `TopicWorker` to `self._topic_worker`. The
second call therefore:

- leaks the first worker — it is never stopped, stays subscribed to
  `topic_intention` / `topic_desire`, and keeps processing;
- leaves two live workers handling the `init` intention, so two
  `ScenarioStarted` events are published, and two scenarios exist for the rest
  of the process's life.

### Why the shutdown crash follows from the same cause

`stop()` is symmetrically called twice — `app/py-app/app.py:102` and then
`cltl-context/src/cltl_service/context/container.py:55` by way of
`super().stop()` at `app/py-app/app.py:104`. The first call stops the (second)
worker and sets `self._topic_worker = None`. The second call reaches:

```python
# app/src/eliza_app_service/context/service.py:58-64
def stop(self):
    if not self._topic_worker:
        pass                       # <-- `pass`, not `return`

    self._topic_worker.stop()      # AttributeError on None
```

`pass` is a no-op, so control falls straight through to the dereference. The
guard reads as if it were `return` and does nothing at all.

### The part that is not yet documented anywhere

The `AttributeError` is raised inside `ContextComponentsContainer.stop()`
(line 55), which has **no** `try`/`finally` — so `super().stop()` on line 56
never runs. Everything after `ContextComponentsContainer` in the
`ApplicationContainer` MRO (`app/py-app/app.py:69-73`) is therefore never
stopped:

```
ChatUIContainer, MonitoringContainer, ASRContainer, VADContainer,
EmissorStorageContainer, BackendContainer
```

That is the root cause of the emissor-data flush problem the app config works
around with `[cltl.emissor-data] flush_interval: 0`. `EmissorStorageContainer`
is past the break, so it never gets a clean stop and never flushes. Fixing
this bug should make that workaround unnecessary — though leaving
`flush_interval: 0` in place is still the safer setting for an interrupted run.

### Suggested fix

Two independent changes; do both.

1. **Stop starting it twice.** Preferred: drop `self.context_service.start()`
   and `self.context_service.stop()` from `ApplicationContainer`
   (`app/py-app/app.py:93` and `:102`) and let `ContextComponentsContainer`
   own the lifecycle, since it already starts the overridden instance. Check
   ordering first — `ApplicationContainer.start()` currently starts it *after*
   the whole chain, whereas the container starts it between `keyword_service`
   and `init_intention`. If the app's `ContextService` genuinely needs to be
   last, the alternative is to make `ContextService.start()`/`stop()`
   idempotent.
2. **Make `stop()` actually guard.** Change `pass` to `return` at
   `app/src/eliza_app_service/context/service.py:60`. Worth grepping for the
   same `if not ...: pass` shape elsewhere — it is an easy typo to repeat.

Consider also giving `ContextComponentsContainer.stop()` the `try`/`finally`
nesting that `ApplicationContainer.stop()` already uses, so one failing service
cannot strand the containers behind it.

### How to verify

```bash
cd app && venv/bin/python py-app/app.py
# in another shell, after startup:
curl -s localhost:8000/monitoring/scenarios      # expect exactly one id
```

Then Ctrl-C and confirm the shutdown log has no `AttributeError` and that the
containers after `ContextComponentsContainer` log their stop. Setting
`[cltl.emissor-data] flush_interval: -1` temporarily and checking that the
scenario is still written on a clean stop confirms the flush path is repaired.

---

## 2. `py-clean` never deletes the sdists it is meant to delete

**Severity:** cosmetic; stale build artefacts accumulate forever.

### Symptom

`cltl-requirements/leolani/` accumulates one sdist per build per component and
never loses one. At the time of writing:

```
cltl_chat_ui-0.0.dev1+1788518010.tar.gz
cltl_chat_ui-0.0.dev1+1788519079.tar.gz
cltl_chat_ui-0.0.dev1+1788519345.tar.gz
cltl_chat_ui-0.0.dev1+1788525666.tar.gz
cltl_chat_ui-0.0.dev1+1788527102.tar.gz
cltl_monitoring-0.0.dev2+1788524442.tar.gz
... and so on
```

### Cause

A name-normalisation mismatch between the makefile and setuptools.

`util/make/makefile.py.base.mk:7` derives the artifact name by turning the
leading `cltl-` into `cltl.`:

```make
artifact_name ?= $(subst cltl-,cltl.,$(project_name))
```

so for `cltl-chat-ui` it is `cltl.chat-ui`, and the cleanup glob at line 17
(and the identical one at line 69) expands to:

```make
@rm -rf $(project_repo)/cltl.chat-ui-{0..9}*+{0..9}*.tar.gz
```

Modern setuptools normalises sdist filenames per PEP 625 — every non-alphanumeric
run becomes a single underscore — so it writes `cltl_chat_ui-0.0.dev1+….tar.gz`.
The glob matches nothing. `rm -rf` on a non-matching glob is silent, so the
target reports success.

This affects every component, not just the two touched by the monitoring work.

### Suggested fix

Match both spellings, since a repo may still hold old-style files:

```make
@rm -rf $(project_repo)/$(artifact_name)-{0..9}*+{0..9}*.tar.gz
@rm -rf $(project_repo)/$(subst .,_,$(subst -,_,$(project_name)))-{0..9}*+{0..9}*.tar.gz
```

`util/make/` is the `leolani/cltl-build` repo, vendored twice as the `util` and
`app/util` submodules, so the fix lands there and propagates via
`make update-build` — not in this repo.

Check `pip cache remove $(artifact_name)` on line 19 while there; pip normalises
names internally so it probably works, but it has not been verified.

### How to verify

```bash
ls cltl-requirements/leolani/ | wc -l
make -C cltl-chat-ui clean
ls cltl-requirements/leolani/ | grep chat_ui     # expect nothing
```

---

## 3. `Bubbles.js` throws on the first keypress if the agent has not spoken yet

**Severity:** cosmetic; one console error, no functional impact.

### Symptom

A `TypeError` in the browser console on submitting the very first message:

```
TypeError: Cannot read properties of undefined (reading 'classList')
    at Bubbles.js:89
```

The message is still sent and the conversation continues normally.

### Cause

`cltl-chat-ui/src/cltl_service/chatui/static/chat-bubble/component/Bubbles.js:87-89`
takes the last `.bubble.say` element without checking that one exists:

```js
var lastBubble = document.querySelectorAll(".bubble.say")
lastBubble = lastBubble[lastBubble.length - 1]      // undefined when the list is empty
lastBubble.classList.contains("reply") && …          // line 89
```

`document.querySelectorAll(".bubble.say")` returns an empty `NodeList` until the
agent's greeting bubble is rendered, which takes roughly 1.5 s after load. Press
Enter before that and the index is `-1`, `lastBubble` is `undefined`, and the
dereference throws.

This is **vendored third-party code** (`chat-bubble`), committed to this repo by
`9fbe767 "Vendor chat-bubble instead of downloading it at build time"`. It is
not code this project wrote, which is why it was left alone.

### Suggested fix

One-line guard, kept minimal so the vendored file stays close to upstream:

```js
lastBubble = lastBubble[lastBubble.length - 1]
lastBubble && lastBubble.classList.contains("reply") &&
```

Since the file is vendored rather than fetched, record the deviation from
upstream — a comment at the edit and a note in whatever tracks the vendoring —
so a future re-vendor does not silently drop it.

### How to verify

Open `http://localhost:8000/chatui/static/chat.html`, type into the input and
press Enter within the first second, before the agent's opening bubble appears.
The console should stay clean.

---

## Follow-up prompt

Paste this into a fresh session:

> The chat UI monitoring tab work turned up three pre-existing bugs that were
> deliberately left unfixed to keep that change in scope. They are written up in
> `docs/plans/pre-existing-bugs-followup.md`, with cause, file:line references, a
> suggested fix and a verification recipe for each.
>
> Please read that file, then fix bug 1 — `ContextService` being started twice,
> which opens two scenarios per run and breaks the container shutdown chain so
> that everything after `ContextComponentsContainer` in the MRO never stops.
> Verify it the way the file describes, and update the "Known bug" note near the
> top of `CLAUDE.md`, which currently describes only the shutdown symptom and
> attributes it to the wrong cause. If the fix makes the
> `[cltl.emissor-data] flush_interval: 0` workaround unnecessary, say so but
> leave the setting in place.
>
> Bugs 2 (`py-clean`'s sdist glob never matching, which needs a change in the
> `cltl-build` submodule under `util/`) and 3 (a missing guard in the vendored
> `chat-bubble` `Bubbles.js`) are independent and cosmetic — ask me before
> starting either, since they touch a shared submodule and vendored third-party
> code respectively.
>
> Do not commit anything until I have reviewed the change.
