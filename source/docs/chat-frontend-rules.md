# Chat Frontend Coding Rules

The `/chat` page is meant to sit open in a browser tab for hours without
spinning the fan or chattering with the server. **Minimizing idle CPU and
idle network traffic is an explicit goal**, not a nice-to-have. If a change
to `webapp/chat_template.py` or `webapp/chat_api.py` would make an idle tab
work harder, it does not land.

## The architecture in one sentence

A single `EventSource('/chat/stream')` connection delivers Postgres
`LISTEN/NOTIFY` events to the browser; the server kernel-blocks in
`conn.notifies(timeout=15.0)` and the client fetches messages only for the
current room on initial load/room switch, user send, stream reconnect,
visible-tab NOTIFY, or one refocus catch-up — and a *streaming* reply grows
one bubble in place straight from the NOTIFY payload, usually with no fetch
at all.

## Rules

1. **No polling.** No `setInterval`. No "every N seconds, check the server."
   Updates must arrive via the existing SSE stream (server pushes from
   `LISTEN/NOTIFY`). The only acceptable timers are short, one-shot
   `setTimeout`s triggered by user activity — UI affordances (the 1.2s "Copy"
   button reset, the 5s toast), the 300ms tree-autosave debounce — and the
   SSE reconnect timeout when `EventSource` reports `CLOSED`. None of them
   may reschedule themselves while the page is idle.

2. **One stream covers all rooms.** Do not open per-room connections, and do
   not background-fetch rooms the user is not viewing. Other-room activity
   shows up as an unread badge from the same SSE event — no fetch against
   that room.

3. **Idle = silent.** When nothing is happening, the page must run zero JS
   on a recurring schedule. SSE keepalive comments (`: keepalive`) are fine
   because browsers discard them before they reach `onmessage`. A hidden tab
   may record minimal state if the browser still delivers SSE events, but it
   must not fetch, render, or start timer-driven work while idle.

4. **Event-driven on the server too.** The SSE handler must block in the
   kernel (`conn.notifies(timeout=...)`), not spin in a Python loop calling
   the database. The heartbeat interval is encoded as the `notifies()`
   timeout — that is the only acceptable form of "every N seconds" on this
   path.

5. **Hidden tabs do less, not more.** A backgrounded tab should not initiate
   server work. If SSE events arrive while hidden, record only the minimal
   dirty state: which progress rows should be removed on refocus
   (`deferredDeletedMessageIds`) and whether unread badges need re-rendering
   (`deferredUnreadRender`); in-place streaming updates are simply skipped.
   Do not fetch, render, prefetch, "warm" pull, or speculatively load while
   hidden.

6. **Refocus = one current-room catch-up.** When the tab becomes visible,
   apply the deferred progress-row deletions and unread re-render, refetch
   each row still marked `.msg-streaming` by id (its in-place updates were
   skipped while hidden, so it shows stale text), and run one `fetchNew` for
   the current room. Do not also refresh the room list, members, stats, etc.

7. **Respond to user activity immediately; idle pacing should taper.** If
   the user is typing or just posted, surface the next reply with the
   latency the SSE stream already provides — do not add artificial
   debouncing on the receive path. Conversely, do not add "be more eager
   because the user is active" timers; the SSE push already is the eager
   path.

8. **No client-side polling fallback.** If the SSE connection drops, the
   browser retries it natively, and `startStream()` rebuilds it after a
   `CLOSED` state. Do not paper over a flaky stream by polling
   `/chat/api/rooms/.../messages` on a schedule — fix the stream instead.

9. **Streaming rides the same push path — throttled, not chatty.** A
   streaming reply (`StreamingReplyWriter` in `chat/streaming.py`) grows a
   thinking/answer row in place: the server flushes persist+NOTIFY on a
   throttle (~0.15s / 40 chars), never per token, and the NOTIFY inlines the
   row text when it fits under the ~8k Postgres NOTIFY cap so the browser's
   `applyStreamingUpdate` upserts the bubble with **no HTTP request**. The
   browser fetches the row by id only on first sighting or when the text was
   too large to inline. Keep it that way: no per-token writes server-side,
   and no fetch on the client when the payload already carries the text.

10. **Render work is on-demand only.** Markdown (`marked`), sanitization
    (`DOMPurify`), and syntax highlighting (`highlight.js`) run only when a
    message is appended or a streaming bubble is upserted. Do not re-process
    the whole log on a timer, on scroll, or on focus.

11. **Verify before claiming "low CPU."** If you are tuning this path,
    measure: count `fetchNew` invocations over a fixed idle window, watch
    DevTools Network for unexpected requests, and check that the SSE
    connection stays open with only `: keepalive` comments arriving every
    `SSE_HEARTBEAT_SECONDS`. Numbers, not vibes.

## Why this matters

The user keeps this tab open all day. A "small" polling loop — 2s, every
room, JSON parse, DOM diff — is invisible in isolation and miserable in
aggregate: the laptop fan spins, the battery drains, and the server takes
load it does not need. The current design has near-zero idle cost; the rule
above is "do not regress that."

## Editing the template

The whole page is one inline HTML/CSS/JS document, `CHAT_TEMPLATE` in
`webapp/chat_template.py` (`webapp/chat_views.py` is just the route, served
with `Cache-Control: no-store` so a normal reload picks up changes).

`CHAT_TEMPLATE` is a **plain (non-raw) Python string**: Python interprets
backslash escapes *before* the browser ever sees the JS, so a `'\n'` in the
inline script becomes a real newline inside a JS string literal and breaks
the whole script. Write JS escapes double-backslashed (the template does
`'\\u00A0'`) or avoid them. The marker tests (`webapp/test_chat_views.py`)
only assert that named symbols appear in the served page — they will NOT
catch a script broken this way; load `/chat` and check the console.

## Pointers

- Client: `webapp/chat_template.py` — search for `EventSource`,
  `startStream`, `fetchNew`, `applyStreamingUpdate`, `visibilitychange`.
- Server: `webapp/chat_api.py` — `chat_stream()` and `SSE_HEARTBEAT_SECONDS`.
- NOTIFY payload shape: `db/chat.py` — `_chat_event_payload` (room/message
  ids, `deleted_progress_ids`, and the streaming `kind`/`streaming`/`text?`
  extras).
- Streaming writer: `chat/streaming.py` — `StreamingReplyWriter` (flush
  throttle) and `extract_stream_deltas`.
- Notify channel: `db.CHAT_NOTIFY_CHANNEL` (`db/models.py`).
