# Telegram bridge service

**Date:** 2026-06-12
**Status:** approved design, pending implementation plan

## Problem / goal

The operator wants to talk to rainbox from Telegram: messages they send on
Telegram appear in a chatroom (where responder agents reply as usual), and the
agents' replies are delivered back to Telegram. The integration must live in a
**separate process** with its own venv — like `kokoro_service/` and
`whisper_service/` — so no Telegram-related dependency enters the core venv.

## Decisions (made with the operator)

1. **Two-way bridge:** Telegram → chatroom AND agent replies → Telegram.
2. **One dedicated room:** all Telegram traffic maps to a single chatroom,
   found by name (`TELEGRAM_ROOM_NAME`, default `telegram`). The operator
   creates it once in the webapp and picks its agent members; the bridge never
   creates rooms.
3. **Raw Bot API via `requests`:** long-poll `getUpdates`, call `sendMessage`.
   No python-telegram-bot, no webhook, no public IP.
4. **Architecture: pure API client (zero core changes).** The bridge uses the
   existing chat JSON API + SSE stream only. The core repo gains no code, no
   routes, no dependencies. The webserver binds 127.0.0.1, so the
   unauthenticated chat API remains local-machine-only — the same trust model
   the browser uses.
5. **Security:** a mandatory allowlist (`TELEGRAM_ALLOWED_USER_IDS`); updates
   from any other Telegram user are dropped and logged. Replies go only to the
   operator's chat id.

## Service layout

```
telegram_service/
├── README.md            # BotFather setup, allowlist, room creation, run instructions
├── requirements.txt     # requests, fully pinned incl. transitives (kokoro precedent)
├── bridge.py            # entrypoint: env config, two worker threads, SIGINT/SIGTERM shutdown
├── telegram_api.py      # thin Bot API client: get_updates(offset, timeout), send_message(chat_id, text)
├── rainbox_api.py       # thin chat-API client: find_room_by_name, post_message,
│                        #   get_messages_after, iter_sse_events
├── test_bridge.py       # bridge logic via injected fake clients (no network)
├── test_telegram_api.py # client against canned HTTP responses
└── test_rainbox_api.py
```

- Own venv (`python3 -m venv venv` inside the dir), own pinned requirements.
- Not a server: no inbound HTTP, no Flask, no /health. Liveness is observable
  from its logs.
- Test basenames are unique repo-wide (the existing
  `kokoro_service/test_server.py` vs `whisper_service/test_server.py`
  collision must not gain a third member).
- `python bridge.py` from inside the dir runs it. Two daemon worker threads
  (inbound, outbound) + a main thread waiting on a `stop_event`; SIGINT/SIGTERM
  set the event and the threads wind down (same shutdown shape as `main.py`).

## Inbound: Telegram → chatroom

1. Long-poll `GET /bot<token>/getUpdates?timeout=50&offset=<n>`.
2. For each update:
   - sender not in `TELEGRAM_ALLOWED_USER_IDS` → drop, log (rate-limited
     logging so a spammer can't flood the log).
   - non-text content (photo, sticker, voice, …) → log, skip (v1 is text-only).
   - text → `POST {RAINBOX_URL}/chat/api/rooms/<room_uuid>/messages` with
     `{"text": ...}` and **no** `sender_uuid`, so the core posts it as the
     seeded human operator and `_maybe_trigger_chat_agents` enqueues the
     room's responder agents — identical to typing in the web UI.
   - record the update's `chat.id` as the operator chat id (persisted; used by
     outbound).
3. **Offset is advanced only after a successful post** (at-least-once
   delivery). A crash between post and offset-persist may duplicate one
   message after restart; accepted and documented in the README.

## Outbound: chatroom → Telegram

1. Subscribe to `GET {RAINBOX_URL}/chat/stream` (SSE) with `requests`
   streaming.
2. For each event for the bridge's room: fetch messages after the cursor via
   `GET …/messages?after=<cursor>`, then forward only rows where
   `kind == "message"` AND the sender is an **agent** user. Human messages are
   never forwarded (prevents echo of Telegram-originated posts; web-typed
   operator messages also stay off Telegram). Debug/thinking/progress rows
   stay in the webapp.
3. Delivery: `POST /bot<token>/sendMessage` as **plain text** (no
   `parse_mode`; avoids MarkdownV2 escaping bugs), split into ≤4096-character
   chunks (Telegram's limit).
4. On SSE disconnect: reconnect with capped backoff; on reconnect, catch up
   from the persisted cursor so replies that landed while disconnected still
   deliver.
5. No operator chat id yet (no inbound message ever received) → outbound
   logs and skips delivery but still advances the cursor; the README documents
   that the bridge starts delivering replies after your first Telegram
   message.

## State

`telegram_service/state.json` (path overridable via `TELEGRAM_STATE_FILE`):

```json
{"telegram_offset": 123456, "operator_chat_id": 987654, "room_cursor": 42}
```

- `room_cursor` initializes to the room's **latest** message id on first run —
  starting the bridge never replays room history to Telegram.
- Written atomically (write temp + rename) after each advance.

## Configuration (env only, like KOKORO_TTS_URL)

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | — | BotFather token |
| `TELEGRAM_ALLOWED_USER_IDS` | yes | — | comma-separated numeric Telegram user ids |
| `RAINBOX_URL` | no | `http://127.0.0.1:5000` | core webapp base URL |
| `TELEGRAM_ROOM_NAME` | no | `telegram` | chatroom the bridge binds to |
| `TELEGRAM_STATE_FILE` | no | `./state.json` | persistence path |

Startup validation: missing token or empty allowlist → exit non-zero with a
clear message. Room not found → exit non-zero telling the operator to create
it in `/chat` and add the agents that should answer.

## Failure behavior

| Failure | Behavior |
|---|---|
| rainbox down (inbound post fails) | capped exponential backoff; Telegram offset not advanced (messages redeliver) |
| Telegram API unreachable | capped exponential backoff on both loops |
| SSE drops | reconnect + catch-up from cursor |
| room deleted at runtime | log, re-discover by name with backoff |
| unauthorized sender | drop + rate-limited log |
| message > 4096 chars | chunked into multiple sendMessage calls |

## Testing

Injected-dependency style (kokoro's `create_app(synthesize_fn=...)`
precedent): the bridge loops take client objects; tests pass in-memory fakes.
Covered without any network: allowlist filtering, text-only filtering,
agent-only/`kind` forwarding filter, chunking at 4096, offset advanced only
after successful post, cursor initialization to latest, catch-up after
simulated disconnect, atomic state writes, clean shutdown on stop_event. The
two thin API clients get tests against canned responses. The service modules
import only `requests` + stdlib, and the root venv already carries `requests`,
so the root `pytest` run collects and runs these tests too.

## Out of scope (v1)

- Media (photos, voice notes, documents) in either direction.
- Telegram commands (`/start`, `/help`), buttons, formatting/MarkdownV2.
- Mirroring web-typed human messages to Telegram.
- Multiple rooms / room-per-chat mapping.
- Core-side changes of any kind.

## Operator runbook (summary for README)

1. Create a bot with @BotFather → token.
2. Get your numeric user id (e.g. message @userinfobot).
3. Create the `telegram` room in `/chat`, add the agents that should reply.
4. `cd telegram_service && python3 -m venv venv && venv/bin/pip install -r requirements.txt`
5. `TELEGRAM_BOT_TOKEN=… TELEGRAM_ALLOWED_USER_IDS=… venv/bin/python bridge.py`
6. Message your bot on Telegram; the message appears in the room; member
   agents reply; replies arrive on Telegram.
