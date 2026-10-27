# Chat UI status bar

Modules had no way to tell the person at the chat UI what they were doing
(loading a model, degraded, failed). A bar under the chat window now shows it,
fed by its own event-bus topic.

## Contract

`StatusEvent` (`cltl-combot`, `cltl/combot/event/status.py`):

| Field | Meaning |
|---|---|
| `message`, `source` | What, and which module |
| `level` | `info` \| `warning` \| `error` |
| `progress` | Optional 0..1; drawn as a progress element on the bar |
| `ttl` | Optional seconds, **from receipt** (the emitter's clock is not ours) |
| `key` | Optional identity of a status, e.g. `asr.model-load` |
| `resolved` | Ends the status with this `key`; requires one |

Publish with `Event.for_scenario_payload(scenario_id, StatusEvent(...))` on
`[cltl.chat-ui.events] topic_status`.

## Behaviour

- **Scenario-bound.** Events with a missing or different `scenario_id`, or
  arriving with no scenario running, are dropped (debug log). The timeline is
  cleared on `ScenarioStopped`.
- **Timeline** is a ring buffer (`[cltl.chat-ui] status_history`, 200). Every
  event is a row, including progress updates and resolves.
- **The bar** shows the highest-level entry that is not expired, not resolved
  and not replaced by a newer event with the same `key`; the newest among equals.
  With none it shows a muted "No status". Its height is fixed and the timeline
  opens upward over the chat window, so nothing reflows.
- **Ending a status**: `ttl`, or `resolved=True` with the same `key`. A resolve
  for an unknown key is just a timeline row. A resolve's own message goes in the
  timeline only.

## Design notes

- Polling, like the rest of the page: `GET /chat/status?from=<seq>` is
  side-effect free (it must not reset the chat's inactivity timeout, as
  `/chat/scenario`). The server decides what is current; `status.js` only draws.
- A **separate `TopicWorker`** serves the status topic. The shared one overwrites
  its oldest event when full, and a burst of status must not evict an utterance.
- The store (`cltl/chatui/status.py`, `StatusLog`) has no EventBus dependency and
  takes a clock, so expiry is testable without sleeping. Sequence numbers keep
  counting across a clear, so a poller's `from` never points past unseen rows.
- Messages reach the DOM through `textContent` only.

## Tests

`cltl-chat-ui/tests/test_status.py`; `integration/tests/slices/test_chatui_status.py`
(topology `chatui_status`, one module: the test is the emitter);
`make -C integration demo-chatui-status` for a person.
