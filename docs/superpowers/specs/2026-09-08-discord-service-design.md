# Discord bridge service + direct-room history window

**Date:** 2026-09-08
**Status:** approved design, pending implementation plan

## Problem / goal

The operator wants to talk to a rainbox **direct room** from Discord, the way
the Telegram bridge (`telegram_service/`) exposes a room on Telegram. Three
requirements shape this beyond a Telegram clone:

1. **Not a REPL bot.** The bot must be free to post several Discord messages
   per turn — progress updates while the model works, a failure notice, the
   reply — and to post with no Discord input at all. Delivery is driven by
   the room, not by a request/response callback.
2. **Rolling history window.** The direct room's model should see only the
   last N messages, and N must be adjustable. Today a direct room sends the
   model its entire history.
3. **Troubleshooting UI.** From the rainbox chatroom sidebar the operator can
   post messages into the room *as the responder*, so the bridge's outbound
   path (message, progress edits, notice) can be exercised and observed on
   Discord without a model or any Discord input.

## Decisions

1. **Transport: Discord REST via `requests`, polled.** No discord.py, no
   gateway WebSocket. Inbound polls `GET /channels/{id}/messages?after=`;
   outbound uses `POST`/`PATCH`/`DELETE` on channel messages. Same shape as
   the Telegram bridge: two worker threads, injected clients, fakes in tests,
   `requests` as the only dependency, own venv. discord.py was rejected
   because its `on_message` callback shape is the REPL bot the operator does
   not want and it drags in asyncio; a raw gateway client would mean
   hand-written heartbeat/resume logic for one channel.
2. **One channel, one room.** The bridge binds one guild text channel
   (`DISCORD_CHANNEL_ID`) to one room found by name (`DISCORD_ROOM_NAME`,
   default `discord`). The README tells the operator to create it as a
   **direct** room; the bridge itself is room-type agnostic (an agents room
   works too, as with Telegram).
3. **Allowlist.** `DISCORD_ALLOWED_USER_IDS` (numeric Discord user ids) is
   mandatory; other authors are dropped with rate-limited logging. Messages
   from bots (including the bridge's own) are always ignored.
4. **Outbound forwards three kinds.** Agent `message` rows (once finished),
   agent `notice` rows (so a failed turn is visible remotely), and `progress`
   rows (one Discord message per progress row, edited in place, deleted when
   reaped). `thinking` and `debug-*` rows stay in the webapp. Telegram
   forwards only `message`; that service is not changed.
5. **The history window is a core direct-room setting**, not something the
   bridge emulates. Every direct room gains it, adjustable in the /chat
   Settings sidebar and via the settings API. The bridge has no knowledge of
   it.
6. **The troubleshooting UI is core-side.** The bridge has no inbound HTTP
   (same "not a server" stance as Telegram), so the sidebar posts rows
   through a new chat API endpoint and the bridge forwards them like any
   other row.
7. **Security model** is unchanged from Telegram: the webapp binds
   127.0.0.1, the bridge holds the Discord token, and Discord access is
   bounded by the allowlist. Outbound messages set
   `allowed_mentions: {"parse": []}` so model output can never ping
   @everyone or a role.

---

## Part A: core changes

### A1. `Chatroom.history_window`

- Column: `history_window INTEGER` nullable, default null. Null = the model
  sees the whole room (today's behavior). N ≥ 1 = only the last N
  `kind="message"` rows (human and assistant alike) are sent; the system
  message is never counted and is always sent when non-empty.
- Migration: `_add_column_if_missing("chatroom", "history_window",
  "history_window INTEGER")` next to the `request_timeout` line in
  `db/__init__.py`.
- `db.set_chatroom_settings(..., history_window: int | None = _UNSET)`;
  same `_UNSET` sentinel pattern as the other fields.
- `DirectChatAgent.build_messages(system_prompt, history, window=None)`
  filters `kind == "message"` rows as today, then keeps the last `window`
  of them when `window` is set. `handle` passes `room.history_window`.
  The docstring and the model comment on `Chatroom.room_type` stop saying
  "FULL history" and describe the window.
- Settings API `GET/PUT /chat/api/rooms/<uuid>/settings`: new key
  `history_window`. PUT accepts a positive integer or null (bool rejected,
  like `request_timeout`), else 400 `"history_window must be a positive
  integer (messages) or null"`. GET returns it.
- Sidebar (`webapp/chat_template.py`, Settings section of a direct room):
  a `ds-label` "History window (messages)" and a `number` input
  (`min=1`, `step=1`, class `ds-window`, placeholder `all`) directly after
  the request-timeout input; Save sends `history_window` (empty/invalid →
  null). Same toast as today ("applies from the next reply").
- Export (`/chat/api/rooms/<uuid>/export`) is unchanged: it records the
  model, not the window.

### A2. Troubleshooting endpoint

`POST /chat/api/rooms/<room_uuid>/troubleshooting-post`

Body: `{"text": str, "kind": "message" | "progress" | "notice"}`.

- Direct rooms only (400 otherwise); 404 for an unknown room; 400 for empty
  text or an unknown kind.
- Sender is always `DIRECT_CHAT_UUID` (the room's responder). Nothing is
  enqueued: the human-only guard in `_maybe_trigger_direct_chat` is never
  reached because the endpoint calls the db layer directly.
- `kind="progress"` → `db.upsert_progress(room, DIRECT_CHAT_UUID, text)`:
  repeated presses rewrite the one live progress bubble (NOTIFY `update`).
- `kind="message"` → `db.post_chat_message(room, DIRECT_CHAT_UUID, text,
  db.detect_content_type(text), kind="message")`. As with a real reply this
  reaps the responder's progress rows in the same transaction.
- `kind="notice"` → same call with `kind="notice"`; also reaps progress.
- Returns 201 `{"id": int, "uuid": str}`.

Because these are ordinary rows, the web UI shows them too, and the Telegram
bridge forwards the `message` ones. That is the point: the UI proves what
the bridges see.

### A3. Troubleshooting sidebar section

In the direct room's Settings sidebar, below the existing Save button, a
section titled **"Bridge troubleshooting"** with:

- a one-line explanation: "Posts into this room as the responder, without a
  model turn. Bridges (Discord, Telegram) forward these like real replies."
- a textarea (class `ds-trouble-text`, placeholder "Message text"),
- three buttons: **Post as progress**, **Post as reply**, **Post as notice**
  (classes `ds-trouble-progress`, `ds-trouble-reply`, `ds-trouble-notice`).

Each button calls the endpoint with its kind and the textarea's text, shows
`chatToast('Posted as <kind>.')` on success and `alert` on failure, and
leaves the textarea as is so the operator can press progress repeatedly.
Empty text is allowed for progress (it mirrors the room's own empty
"working" bubble) and rejected client-side for reply/notice with a toast
("Reply/notice needs text").

A suggested check, documented in the Discord README: press *Post as
progress* three times with different text, then *Post as reply*. Discord
should show one bubble edited three times, then that bubble deleted and the
reply posted. No Discord input was involved, so the bot is demonstrably not
a request/response loop.

### A4. Core tests

- `db/test_chat_direct.py`: `history_window` round-trips through
  `set_chatroom_settings`; null clears it.
- `agents/test_direct_chat.py`: `build_messages` with `window=2` keeps the
  last two message rows and the system message; `window=None` keeps all;
  `handle` passes the room's window (assert the streamed message count).
- `webapp/test_chat_direct_api.py`: settings GET returns `history_window`
  (null by default); PUT accepts 5 and null, rejects 0, -1, `true`, `"5"`;
  troubleshooting-post: 404 unknown room, 400 agents room, 400 empty
  text, 400 bad kind; progress twice yields one progress row from
  `DIRECT_CHAT_UUID` with the second text; a following reply reaps it and
  leaves a `kind="message"` row; nothing is enqueued for the responder.
- Template marker test (same style as the existing chat template tests):
  the inline JS contains `ds-window`, `history_window`, and the three
  `ds-trouble-*` classes. Remember the inline-JS template is a non-raw
  Python string: no backslash escapes in the added JS.

---

## Part B: `discord_service/`

### Layout

```
discord_service/
├── .gitignore                  # venv/, state.json, state.tmp
├── README.md                   # portal setup, intent, invite, allowlist, room creation, run, troubleshooting check
├── requirements.txt            # requests, fully pinned incl. transitives (copy telegram_service's)
├── bridge.py                   # entrypoint: env config, state, two loops, signals
├── discord_api.py              # thin REST client (see below)
├── rainbox_api.py              # chat-API client: telegram's + get_message(room, id)
├── test_discord_bridge.py      # loop logic via fakes
├── test_discord_api.py         # client against canned HTTP responses
└── test_discord_rainbox_api.py
```

- Test basenames are unique repo-wide (`telegram_service/` already owns
  `test_bridge.py` / `test_rainbox_api.py`). Module names `bridge.py` and
  `rainbox_api.py` are reused deliberately for parity; the root pytest run
  already passes `--ignore=telegram_service` and gains
  `--ignore=discord_service` (documented in the README; `notes/testing.md`
  is the operator's file and is left for them to update). The service's
  tests run from inside its directory:
  `cd discord_service && ../venv/bin/python -m pytest -q`.
- Own venv, pinned requirements, no Flask, no inbound HTTP, no /health.
- `source/README.md` layout table: add `discord_service/` to the standalone
  services row.

### Configuration (env only)

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `DISCORD_BOT_TOKEN` | yes | — | bot token from the developer portal |
| `DISCORD_CHANNEL_ID` | yes | — | the one text channel the bridge binds |
| `DISCORD_ALLOWED_USER_IDS` | yes | — | comma-separated numeric Discord user ids |
| `RAINBOX_URL` | no | `http://127.0.0.1:5000` | core webapp base URL |
| `DISCORD_ROOM_NAME` | no | `discord` | chatroom the bridge binds to |
| `DISCORD_STATE_FILE` | no | `./state.json` | persistence path |
| `DISCORD_POLL_SECONDS` | no | `2` | inbound poll interval |

Startup validation mirrors Telegram: missing token/channel/allowlist or a
non-numeric id → `SystemExit` with a message that says where to find the
value (Discord: enable Developer Mode, right-click → Copy ID). Room not
found → exit telling the operator to create it (as a direct room) on
`/chat`. Snowflakes are handled as strings end to end (they exceed 2^53;
JSON must never carry them as numbers).

### `discord_api.py`

Base `https://discord.com/api/v10`, header `Authorization: Bot <token>`,
`User-Agent: DiscordBot (rainbox discord_service, 1.0)`.

- `get_messages(channel_id, after: str | None, limit=100) -> list[dict]`
  — returned oldest-first (Discord returns newest-first; the client sorts
  by `id` as int). `after=None` fetches the newest page (used only for the
  first-run cursor).
- `send_message(channel_id, text) -> list[str]` — chunks at 2000 chars,
  posts each with `allowed_mentions: {"parse": []}`, returns the Discord
  message ids (a progress row maps to the FIRST id; long progress text is
  truncated to 2000 with a trailing `…` rather than chunked, since it is
  edited in place).
- `edit_message(channel_id, message_id, text)` — `PATCH`, same
  `allowed_mentions`, truncated like above.
- `delete_message(channel_id, message_id)` — 404 is swallowed (already
  gone).
- `get_me() -> dict` — `GET /users/@me`, used at startup to learn the bot's
  own user id (logged; own messages are also caught by the `bot` flag).
- Rate limits: on 429 the client sleeps the response's `retry_after`
  (capped at 30 s) and retries once; a second 429 raises. Every other
  non-2xx raises `requests.HTTPError`.
- `chunk_text(text, limit=2000)` shared helper, tested.

### `rainbox_api.py`

Telegram's client plus `get_message(room_uuid, message_id) -> dict | None`
(`GET /chat/api/rooms/<uuid>/messages/<id>`, None on 404). The rooms list
is still matched by name.

### State

`discord_service/state.json`:

```json
{"discord_after": "1234567890123456789", "room_cursor": 42,
 "progress_messages": {"57": "1234567890123456790"}}
```

- `discord_after`: newest handled Discord message id. First run: the
  channel's newest message id (or `"0"` for an empty channel) — Discord
  history is never replayed into the room.
- `room_cursor`: initialized to the room's latest row id on first run, as
  Telegram does — room history is never replayed to Discord.
- `progress_messages`: room row id → Discord message id for live progress
  bubbles.
- Atomic write (temp + rename) under one lock, shared by both threads.

### Inbound: Discord → room

Loop (thread `inbound`): every `DISCORD_POLL_SECONDS`, fetch messages after
`discord_after`. For each, oldest first:

- `author.bot` true → skip silently.
- `author.id` not in the allowlist → drop with rate-limited warning (one
  per author per 60 s).
- empty `content` (attachment/embed/sticker only) → log, skip.
- otherwise `POST /chat/api/rooms/<uuid>/messages` with `{"text": content}`
  and no `sender_uuid` → posted as the seeded human, which enqueues the
  direct-chat responder (or, in an agents room, the responder agents).
- `discord_after` advances per message only after that message is handled;
  a failed post raises before the advance (at-least-once; a crash between
  post and persist can duplicate one message after restart, documented).

Errors: capped exponential backoff (2^n s, cap 60) with the token redacted
from logged exception text (`Bot <token>` in headers never appears in
`requests` messages, but the regex guard stays for parity and for any
future URL-embedded secret).

### Outbound: room → Discord

Loop (thread `outbound`): run a catch-up, then consume `/chat/stream`; on
each event for the bridge's room:

1. **`deleted_progress_ids`** present → for each id in
   `progress_messages`, delete the Discord message and drop the mapping.
2. **`kind == "progress"`** → text = the event's inline `text` if present,
   else `get_message(room, id)["text"]` (row gone → treat as deleted). Empty
   text renders as `⏳ working…`. If the row id is mapped → `edit_message`;
   else `send_message` and map the first returned id. Persist state.
3. **Catch-up** (also run on connect/reconnect): rows after `room_cursor`
   in id order; stop at the first `streaming: true` row without advancing
   (the finalizing event re-runs the catch-up); forward
   `sender_type == "agent"` rows with `kind in ("message", "notice")` via
   `send_message`; skip every other kind (including progress, which is
   handled by events, not the cursor); advance `room_cursor` per row.
4. **Reconcile on (re)connect**: for each mapped progress row,
   `get_message`; if it is gone, delete the Discord message and unmap. This
   covers reaps that happened while the SSE connection was down.

SSE end without error (server restart) and errors both reconnect with the
same capped backoff as Telegram.

### Failure behavior

| Failure | Behavior |
|---|---|
| rainbox down (inbound post fails) | backoff; `discord_after` not advanced, message redelivered |
| Discord API unreachable | backoff on both loops |
| Discord 429 | sleep `retry_after`, retry once, then treat as an error |
| SSE drops | reconnect, reconcile progress map, catch up from cursor |
| room deleted at runtime | log, re-discover by name with backoff |
| unauthorized author | drop + rate-limited log |
| reply > 2000 chars | chunked into several messages |
| progress text > 2000 chars | truncated with `…` |
| Discord message for a progress row already deleted | 404 swallowed, mapping dropped |

### Tests (`test_discord_bridge.py`, fakes, no network)

Allowlist filtering; bot-author skip; empty-content skip; at-least-once
`discord_after` advance (post failure raises before advance); first-run
cursor init for both sides; catch-up forwards agent `message` + `notice`
only, stops at a streaming row; progress insert → send + map, update →
edit, reap → delete + unmap; reconcile deletes orphaned mappings; chunking
at 2000 and progress truncation; state written atomically; clean stop.
`test_discord_api.py`: URL/headers/body shape (incl. `allowed_mentions`),
newest-first → oldest-first sort, snowflake strings, 429 retry, 404 on
delete swallowed. `test_discord_rainbox_api.py`: `get_message` 200/404.

### README runbook

1. Developer portal → New Application → Bot → copy the token; enable
   **Message Content Intent**.
2. OAuth2 URL generator: scope `bot`, permissions View Channel, Send
   Messages, Read Message History, Manage Messages (for deleting progress
   bubbles); invite to your server; create or pick one text channel.
3. Enable Developer Mode in Discord; copy the channel id and your user id.
4. In rainbox `/chat`, create a **direct** room named `discord`, pick its
   model, optionally set its history window.
5. `cd discord_service && python3 -m venv venv && venv/bin/pip install -r requirements.txt`
6. `DISCORD_BOT_TOKEN=… DISCORD_CHANNEL_ID=… DISCORD_ALLOWED_USER_IDS=… venv/bin/python bridge.py`
7. Post in the channel; the working bubble appears in Discord, then the
   reply. Then use the sidebar's Bridge troubleshooting section as
   described in A3 to watch the bot post without any Discord input.

## Out of scope (v1)

- Streaming the reply into Discord as it generates (edit-as-you-go).
- Attachments, embeds, stickers, voice in either direction.
- Slash commands, buttons, threads, DMs to the bot.
- Adjusting the history window from inside Discord.
- Multiple channels or rooms; mirroring web-typed human messages to Discord.
- Forwarding progress/notice rows from the Telegram bridge.
