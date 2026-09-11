# Discord bridge service

A standalone process that bridges Discord text channels and rainbox
chatrooms, two-way. Kept separate from the main project (own venv) so no
Discord-related dependency enters the main venv. Talks to the core over HTTP
only (the chat JSON API + SSE stream + the bridge config endpoint); the core
never imports this code.

Two modes, exclusive:

- **Connector mode** (`BRIDGE_CONNECTOR=<uuid>`): the process serves one
  connector row from `/bridges` — any number of bindings (chatroom ↔
  channel), each with its own allowlist and forwarding policy, edited live.
  The launcher (`python main.py` in `source/`) starts one such process per
  enabled connector. This is the normal way to run it; see *Connector mode*.
- **Legacy env mode** (no `BRIDGE_CONNECTOR`): one channel, one room, all
  settings from `DISCORD_*` variables; kept for the transition (*Run (legacy
  env mode)* below, and `import_legacy.py` to move off it).

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

## Connector mode

1. On `/bridges`: **+ Connector** (platform Discord, a credential variable
   NAME such as `DISCORD_TOKEN_MAINBOT`), then **+ Binding** per chatroom ↔
   channel pair (channel id from Discord's *Copy Channel ID*). Set
   `allowed_senders` (numeric user ids) on the connector, a folder, or the
   binding — the nearest level wins; enable the binding and the connector.
2. On the connector pane press **Set token…** and paste the bot token. It
   is sealed (AES-GCM under `RAINBOX_CREDENTIAL_KEY` from the repo-root
   `.env`) before it is stored, no page or API ever returns it, and the
   launcher receives it with the connector's desired entry and injects it
   under that variable name at every spawn. Saving a new value restarts a
   running connector, so rotation is one paste. A manual run instead gets
   the token from its own launch environment (the pane's command says so).

The process fetches `GET /bridge/api/connectors/<uuid>/config` when its
`/chat/stream` connection opens and again on every `bridge_config` event
naming its connector (the core emits one from the transaction that commits
any connector/folder/binding change), never on a timer. While the stream is
down or an event is pending, the snapshot is stale and no message is sent or
posted; delivery resumes from the persisted cursors once a fresh snapshot is
published. Each remote request (every chunk, every 429 retry) re-checks the
snapshot's freshness and the binding's effective enablement and direction.

| Env var (connector mode) | Required | Meaning |
|---|---|---|
| `BRIDGE_CONNECTOR` | yes | the connector's uuid (selects this mode) |
| *the connector's `token_env`* | yes | the bot token, under whatever NAME the row says; read from this process's environment only (the launcher sets it from the sealed value saved on /bridges; a manual run exports it) |
| `RAINBOX_URL` | no (`http://127.0.0.1:5000`) | core webapp base URL |
| `DISCORD_STATE_FILE` | no (`./bridge-<uuid>.json`) | per-connector state (schema 2): bot identity, per-binding cursors and progress maps; the launcher sets `<state-dir>/bridge-<uuid>.json` |

State ownership: an exclusive OS lock on `<state-file>.lock` is held for the
process lifetime, so a duplicate for the same connector exits **3**. Local
validation failures and a confirmed credential rejection (401/403 on
`/users/@me`) exit **2**; the launcher does not respawn either. A state file
written by a different bot id is refused (exit 2) rather than reused.
Removing a binding retires its worker and deletes its recorded progress
bubbles best-effort (30 s total, one attempt each, only while the connector
is enabled and the snapshot fresh), then prunes its state; a connector
disable pauses everything and keeps state. Config 404, timeouts, and bad
responses pause traffic and retry — they never make the process fail or
fall back to the legacy variables.

### Moving a legacy setup over

From `discord_service/` with the legacy `DISCORD_*` environment (the token is
not read):

```bash
venv/bin/python import_legacy.py --name "Main Bot" --state-dir /absolute/path/to/source/var/services
```

It resolves `DISCORD_ROOM_NAME` to exactly one room, creates a disabled
connector and binding with the legacy allowlist and poll interval, and, if
`state.json` exists, writes `bridge-<uuid>.json` in the state dir with the
cursors and progress map under the new binding (the legacy file stays for
rollback). Then stop the legacy process, paste the token on the connector
pane, enable on `/bridges`, and let the launcher start it. Never run both
modes for the same bot.

## Run (legacy env mode)

With rainbox running (`python main.py` from `source/`):

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
(in-memory fakes; no network, no token): `test_discord_bridge.py` (env
mode), `test_connector_bridge.py` (connector mode), `test_import_legacy.py`,
and the two client tests. The root-wide run must ignore this
directory (`--ignore=discord_service`, like `telegram_service`): both define
a `bridge` module.
