# Telegram bridge service

A standalone process that bridges Telegram and one rainbox chatroom, two-way.
Kept separate from the main project (own venv) so no Telegram-related
dependency enters the main venv. Talks to the core over HTTP only (the chat
JSON API + SSE stream); the core never imports this code and was not changed
to support it.

- **Inbound:** messages you send to your Telegram bot are posted into the
  configured chatroom as you (the human operator) — the room's responder
  agents reply exactly as if you had typed in the web UI.
- **Outbound:** agent replies (`kind="message"` rows from agent senders) in
  that room are delivered back to your Telegram chat. Debug rows and your own
  messages are not forwarded.

## Setup

1. Create a bot: talk to @BotFather on Telegram → `/newbot` → copy the token.
2. Find your numeric Telegram user id (e.g. message @userinfobot).
3. In the rainbox webapp, create the room (default name `telegram`) on
   `/chat` and add the agents that should answer. The bridge never creates
   rooms.
4. Create the venv:

   ```bash
   cd telegram_service
   python3 -m venv venv
   venv/bin/pip install -r requirements.txt
   ```

## Run

With `main.py` (the core) already running:

```bash
cd telegram_service
TELEGRAM_BOT_TOKEN=123:abc \
TELEGRAM_ALLOWED_USER_IDS=987654321 \
venv/bin/python bridge.py
```

| Env var | Required | Default | Meaning |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | — | BotFather token |
| `TELEGRAM_ALLOWED_USER_IDS` | yes | — | comma-separated numeric user ids; everyone else is dropped |
| `RAINBOX_URL` | no | `http://127.0.0.1:5000` | core webapp base URL |
| `TELEGRAM_ROOM_NAME` | no | `telegram` | chatroom the bridge binds to |
| `TELEGRAM_STATE_FILE` | no | `./state.json` | offset/cursor persistence |

Stop with Ctrl-C.

## Behavior notes

- **At-least-once inbound:** the Telegram offset advances only after a message
  is successfully posted into the room; a crash at the wrong instant can
  duplicate one message after restart.
- **Replies need a first message:** outbound delivery starts after your first
  Telegram message (that's how the bridge learns your chat id).
- **No history replay:** on first run the room cursor starts at the room's
  latest message; old history is never sent to Telegram.
- **Text only (v1):** photos/voice/stickers are logged and skipped; replies
  are sent as plain text, chunked at Telegram's 4096-char limit.

## Tests

From the repo's source root: `venv/bin/python -m pytest -q telegram_service/`
(the bridge logic is tested with in-memory fakes; no network, no bot token).
