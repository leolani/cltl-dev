# Chat UI: the Display tab

Other modules had no way to put a page of their own in front of the person
chatting. The side panel offered the annotator and cltl-monitoring's page, both
wired in by configuration. This adds a third tab whose page is chosen at run time
by an event: any module publishes a url and a name on `cltl.topic.display`, the
tab frames the newest one, and a list keeps every page shown in the conversation.

It is built the way the status bar is: an optional topic, its own `TopicWorker`,
a store without an EventBus dependency, browser polling, and a history that
belongs to the scenario.

## Contract

`cltl.combot.event.display.DisplayEvent`, published with
`Event.for_scenario_payload`:

| Field | Meaning |
|---|---|
| `url` | What the browser loads. Must be reachable *from the browser* — in Compose a published host port, never a service name. The chat UI never rewrites it. |
| `name` | What the person sees: the tab's label and the entry in the list. |

Both must be non-empty strings.

## Behaviour

- Off unless `[cltl.chat-ui.events] topic_display` is set; `/config` reports
  `display_enabled`, and the tab is left out otherwise.
- With the topic set the tab is always there. It reads "Display" and says
  "Nothing to show yet." until the first page, and is then named after the newest.
- The newest page is the one on view. A person who picked an older page from the
  list is moved on by the next event.
- A new page does not move the person to the tab: while the panel is hidden the
  tab shows a "new" badge until it is clicked.
- Showing a url again moves it to the end of the list rather than adding a row.
  `display_history` (default 50) bounds the list.
- **Open in new tab** is always offered. Many sites send `X-Frame-Options` or
  `frame-ancestors` and refuse to be framed, and the browser reports nothing
  usable when they do.
- Events are scenario-bound: those without the running scenario's id are dropped,
  and the history is cleared on `ScenarioStopped`.

## What may be shown

The page is a frame the service fills with whatever arrives on the bus, so the
service decides, in `cltl.chatui.display.DisplayLog.is_allowed`, before the
browser ever hears of a url:

- the scheme is `http` or `https` — no `javascript:`, `data:`, `file:`;
- there are no credentials in the url, which is how `http://localhost@evil.example`
  passes for a local one;
- the host is `localhost`, `127.0.0.1` or `::1`, or is listed in
  `[cltl.chat-ui] display_allowed_hosts` (comma separated, exact host match).

A refused url is logged as a warning and dropped. The frame has no `sandbox`,
like the Monitoring one, so the allow-list is the control: anything it lets
through runs with full privileges, and on the in-process tier a `localhost` page
is same-origin with the chat UI. Anything that can publish on the bus is trusted
to that extent.

## Design notes

- **Whole history, not `?from=`.** `GET /chat/display` returns every entry. Status
  rows are only ever appended, so a poller can ask for the newer ones; here a
  repeated url moves, which a poller holding the old row could not see. The list
  is bounded and `last_sequence` tells the page whether to redraw.
- **Its own worker** (`ChatUiService-display`), because the shared one overwrites
  its oldest event when full and a burst of pages must not push out an utterance.
- **Payload read with `getattr`**, not `isinstance`: the kombu bus delivers a
  `SimpleNamespace`. The chat UI therefore does not import `DisplayEvent`; only
  publishers do.
- **`textContent` only.** Names are written as text; urls go to `src` and `href`.
- `panels.js` needed one branch in `apply()`. Tab discovery, the keyboard
  handling and the single-tab rule are generic. When Display is the only tab
  the strip is hidden, so the panel carries the name in its own header.

## Tests

- `cltl-chat-ui/tests/test_display.py` — the log (allow-list, schemes,
  credentials, bad ports, history cap, repeats, sequence after clear) and the
  service (off without a topic, scenario filtering, namespace payloads, cleared on
  stop).
- `integration/tests/slices/test_chatui_display.py` — `DisplayEvent` through the
  real bus and HTTP app.
- `integration/tests/browser/test_chatui_display.py` — the tab, the badge, the
  list, the frame, names as text, the end of a scenario.
- Not covered: Display as the *only* tab (strip hidden), and a framing refusal by
  a real third-party site.

## Running it

`make -C integration demo-chatui-display` publishes a loop of pages served by the
chat UI itself, and one refused url that should never appear.
