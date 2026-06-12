# Chat Frontend Coding Rules

The `/chat` page is meant to sit open in a browser tab for hours without
spinning the fan or chattering with the server. **Minimizing idle CPU and
idle network traffic is an explicit goal**, not a nice-to-have. If a change
to `webapp/chat_views.py` or `webapp/chat_api.py` would make an idle tab work
harder, it does not land.

## The architecture in one sentence

A single `EventSource('/chat/stream')` connection delivers Postgres
`LISTEN/NOTIFY` events to the browser; the server kernel-blocks in
`conn.notifies(timeout=15.0)` and the client fetches messages only for the
current room on initial load/room switch, user send, stream reconnect,
visible-tab NOTIFY, or one refocus catch-up.

## Rules

1. **No polling.** No `setInterval`. No "every N seconds, check the server."
   Updates must arrive via the existing SSE stream (server pushes from
   `LISTEN/NOTIFY`). The only acceptable timers are short, one-shot
   `setTimeout`s for local UI affordances (e.g., a 1.2s "Copy" button reset)
   and the SSE reconnect timeout when `EventSource` reports `CLOSED`.

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
   dirty state: which progress rows should be removed on refocus and whether
   unread badges need to be re-rendered. Do not fetch, render, prefetch,
   "warm" pull, or speculatively load while hidden.

6. **Refocus = one current-room catch-up.** When the tab becomes visible,
   apply any deferred progress-row deletions and run one `fetchNew` for the
   current room. Do not also refresh the room list, members, stats, etc.
   Re-render unread badges only if hidden SSE events changed them.

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

9. **Render work is on-demand only.** Markdown (`marked`), sanitization
   (`DOMPurify`), and syntax highlighting (`highlight.js`) run only when a
   message is appended. Do not re-process the whole log on a timer, on
   scroll, or on focus.

10. **Verify before claiming "low CPU."** If you are tuning this path,
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

## Pointers

- Client: `webapp/chat_views.py` — search for `EventSource`, `startStream`,
  `fetchNew`, `visibilitychange`.
- Server: `webapp/chat_api.py` — `chat_stream()` and
  `SSE_HEARTBEAT_SECONDS`.
- Notify channel: `db.CHAT_NOTIFY_CHANNEL`.
