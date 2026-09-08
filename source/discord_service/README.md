# Discord bridge service

A standalone process that bridges one Discord text channel and one rainbox
chatroom, two-way. Kept separate from the main project (own venv) so no
Discord-related dependency enters the main venv. Talks to the core over HTTP
only (the chat JSON API + SSE stream); the core never imports this code.

It is deliberately **not** a request/response bot: nothing here waits for a
Discord message in order to answer it. The room drives Discord — whenever a
row lands in the room the bridge mirrors it, so one turn can produce a
working bubble that is edited as the agent progresses, a failure notice, and
the reply, and the Settings sidebar can make the bot post with no Discord
input at all (see *Troubleshooting* below).

- **Inbound:** messages allowed users post in the channel are posted into the
  room as you (the human operator); the room's responder replies exactly as
  if you had typed in the web UI. Bots (including this one) are ignored;
  attachment-only messages are skipped.
- **Outbound:** agent `message` and `notice` rows become new Discord
  messages (chunked at 2000 chars, mentions suppressed). Agent `progress`
  rows become one Discord message each, edited in place as they change and
  deleted when the reply lands. `thinking`/debug rows stay in the webapp.

## Setup

1. https://discord.com/developers/applications → New Application → Bot →
   Reset Token → copy it. Under *Privileged Gateway Intents* enable
   **Message Content Intent**.
2. OAuth2 → URL Generator: scope `bot`; permissions *View Channels*, *Send
   Messages*, *Read Message History*, *Manage Messages* (deleting its own
   progress bubbles). Open the URL and add the bot to your server.
3. Discord → Settings → Advanced → **Developer Mode**. Right-click the
   channel → Copy Channel ID; right-click yourself → Copy User ID.
4. In rainbox `/chat`, create a **direct** room named `discord`, pick its
   model in Settings, optionally set its *History window*.
5. Create the venv:

   ```bash
   cd discord_service
   python3 -m venv venv
   venv/bin/pip install -r requirements.txt
   ```

## Run

With `main.py` (the core) already running:

```bash
cd discord_service
DISCORD_BOT_TOKEN=... \
DISCORD_CHANNEL_ID=123456789012345678 \
DISCORD_ALLOWED_USER_IDS=234567890123456789 \
venv/bin/python bridge.py
```

| Env var | Required | Default | Meaning |
|---|---|---|---|
| `DISCORD_BOT_TOKEN` | yes | — | bot token from the developer portal |
| `DISCORD_CHANNEL_ID` | yes | — | the one text channel the bridge binds |
| `DISCORD_ALLOWED_USER_IDS` | yes | — | comma-separated numeric user ids; everyone else is dropped |
| `RAINBOX_URL` | no | `http://127.0.0.1:5000` | core webapp base URL |
| `DISCORD_ROOM_NAME` | no | `discord` | chatroom the bridge binds to |
| `DISCORD_STATE_FILE` | no | `./state.json` | cursor + progress-map persistence |
| `DISCORD_POLL_SECONDS` | no | `2` | inbound poll interval |

Stop with Ctrl-C.

## Troubleshooting

Open the room on `/chat`, choose **Settings** in the right panel, scroll to
**Bridge troubleshooting**. Type something, press *Post as progress* three
times with different text, then *Post as reply*. On Discord one bubble
appears and is edited twice, then it disappears and the reply is posted —
no message was sent from Discord, so the bot is demonstrably not answering
anything. *Post as notice* posts a notice row the same way.

If nothing arrives: the bridge logs every forwarded row (`room -> discord`);
a 403 on send means the bot lacks *Send Messages* in that channel; a 401 at
startup means the token is wrong.

## Behavior notes

- **At-least-once inbound:** the Discord cursor advances only after a
  message is successfully posted into the room; a crash at the wrong instant
  can duplicate one message after restart.
- **No history replay:** on first run both cursors start at "newest": old
  Discord messages are never posted into the room, old room rows never sent
  to Discord.
- **Reconnects reconcile:** after an SSE reconnect the bridge deletes Discord
  bubbles whose progress rows were reaped meanwhile, then catches up on
  replies from its cursor.
- **Rate limits:** a 429 is honored once (sleep `retry_after`), then treated
  as an error with backoff.
- **Token hygiene:** the token is scrubbed from logged errors.

## Tests

From the repo's source root: `venv/bin/python -m pytest -q discord_service/`
(in-memory fakes; no network, no token). The root-wide run must ignore this
directory (`--ignore=discord_service`, like `telegram_service`): both define
a `bridge` module.
