# Verifying the bridge settings work, step by step

**Date:** 2026-09-12. **Branch:** `bridge-settings` (not merged). Design:
[2026-09-09-bridge-settings-design.md](../specs/2026-09-09-bridge-settings-design.md).

This is an operator walkthrough: each step says what to do, what you should
see, and what it proves. Budget about 30 minutes. Stop at any step that does
not match and read *Troubleshooting* at the end.

**The short version**, if you only want the checklist:

1. Add `RAINBOX_CREDENTIAL_KEY` to `.env` (once), start `main.py`, check
   `/settings` says *managed*.
2. `/bridges` → **+ Connector** (Discord, variable name `DISCORD_TOKEN_MAINBOT`).
3. **Set token…** on the pane, paste the bot token.
4. Tick **Enabled** → pane says `running`, terminal shows `[Main Bot]` lines.
5. **+ Binding** (room + channel id), set `allowed_senders` to your user id,
   tick **Enabled** on the binding → terminal says `binding … activated`.
6. Post in Discord → it lands in the room, the reply comes back. Use the
   room's *Bridge troubleshooting* buttons to watch a progress bubble.
7. Flip policies on `/bridges` while it runs; nothing restarts, everything
   applies within a second.

Each phase below is one of those steps, with what to expect and what it proves.

## What you need before starting

- A Discord bot token that has **not** been pasted anywhere (reset it in the
  Developer Portal if in doubt), invited to your server with *View Channels*,
  *Send Messages*, *Read Message History*, *Manage Messages*, and *Message
  Content Intent* enabled — the same setup `discord_service/README.md` step
  1–3 describes.
- The **channel id** of one text channel and **your user id** (Discord →
  Settings → Advanced → Developer Mode, then right-click → Copy ID).
- A **direct** chatroom on `/chat` (the existing `discord` room is fine).
- Your usual Discord bridge process (env mode) must **not** be running for
  this bot while you test. Two processes for one bot would both read the
  channel.

The token is pasted once into a write-only field on `/bridges`; it is
sealed under `RAINBOX_CREDENTIAL_KEY` before it is stored and nothing ever
displays it again.

## Phase A — the sealing key, then the launcher (4 min)

Bot tokens are stored sealed in Postgres under a key that lives only in the
repo-root `.env`. Add it once (skip if `.env` already has a
`RAINBOX_CREDENTIAL_KEY` line):

```bash
cd ~/git/rainbox && printf 'RAINBOX_CREDENTIAL_KEY=%s\n' "$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')" >> .env
```

Keep that line with your backups: a database restored without it holds
tokens nobody can open, and you would paste them again.

Now start the launcher on this branch:

```bash
cd ~/git/rainbox && git checkout bridge-settings && cd source && venv/bin/python main.py
```

Expect, in that terminal:

- `launcher pid N; state dir /Users/…/rainbox/var/services; core-only=False`
- the core's own startup lines (it creates the three new tables on first
  start; no migration step), then the core serving on port 5000.

Open http://127.0.0.1:5000/settings. The **launcher** card at the top must say
*managed; core running*. If it says *unmanaged*, you started `core.py`, not
`main.py`, and nothing will start a bridge — restart with the command above.

**Proves:** the launcher owns the core and can start side processes, and
the core has the key (Phase C would otherwise show a warning).

## Phase B — create a connector (2 min)

Open http://127.0.0.1:5000/bridges. Click **+ Connector**:

- Name: `Main Bot`
- Platform: Discord (Telegram and Zulip are listed but greyed out — their
  bridges have no connector mode yet)
- Credential variable name: `DISCORD_TOKEN_MAINBOT`

Click Create. The connector appears in the left tree and its pane opens.
Expect:

- *Credential variable* `DISCORD_TOKEN_MAINBOT`, and a *Token* row saying `not set`.
- *Desired state*: Enabled unchecked; Launch mode *launcher*.
- *Process*: `unknown` (the launcher has never been asked to run it).
- The **Manual launch command** section shows the launcher's state directory
  and a command — you will not need it today.

**Proves:** the row is stored and the page reads it back; no token involved.

## Phase C — paste the token (1 min)

On the connector pane press **Set token…**, paste the bot token, Save.
Expect the *Token* row to say `set` with a timestamp and the toast
"Token saved". The field is a password input inside a modal and is cleared
when the modal closes.

If the pane shows a warning about `RAINBOX_CREDENTIAL_KEY` instead, the core
did not see the key: check the `.env` line from Phase A and restart the
launcher.

**Proves:** the value went in write-only. Reload the page, open the
connector's JSON at `/bridges/api/connectors/<uuid>` (the id is on the
pane), or open Admin Panel → Bridges → Bridge Credential: none of them
shows the token, only that one is set and when.

## Phase D — enable it and watch it start (2 min)

On the connector pane tick **Enabled**. Within a second:

- The launcher terminal prints lines prefixed `[Main Bot]`, ending with
  something like `discord bot <bot name> (<id>); connector 'Main Bot', 0 binding(s) configured`.
- The pane's *Process* pill turns `running (pid …)` with *credential from
  database* (it refreshes every 10 s; click the connector again to refresh now).

If instead the pill says `credential missing`, no token is stored for this
connector: Phase C was skipped or the key is missing. Save the token; that
alone restarts it.

**Proves:** the desired state, token included, reached the launcher over its
socket, the credential was injected under the named variable, and the bot
authenticated. Also proves that
an enabled connector with no bindings just idles — nothing is posted.

## Phase E — bind the channel to a room (3 min)

1. With `Main Bot` selected, click **+ Binding**. Pick the chatroom, enter
   the channel id, Create. The binding appears as `<room> ↔ channel_id=…`
   in the tree, dimmed because it is disabled.
2. Select `Main Bot` again. In the policy table, **Edit** `allowed_senders`:
   untick *Inherit*, enter your user id (one per line), Save. The row now
   shows your id under *Here*.
3. Select the binding. Its policy table shows `allowed_senders` as
   `<your id> from connector` (inherited, nothing set on the binding). Tick
   **Enabled**.

Launcher terminal, within a second:

```
[Main Bot] … config revision …: connector enabled, 1 binding(s), 1 active
[Main Bot] … binding <uuid> activated: room <uuid> <-> channel_id=… (after …, cursor …)
```

Both cursors start at "newest": nothing from the channel's or the room's
history is replayed.

**Proves:** a config change is delivered by the `bridge_config` event (no
restart, no polling), policy resolves nearest-wins, and activation
establishes the high-water marks.

## Phase F — traffic both ways (5 min)

1. In Discord, post `hello from discord` in the bound channel. Within the
   poll interval (2 s) the launcher prints `[Main Bot] … discord -> room …: 18 chars`
   and the message appears in the room on `/chat` as you; the room's
   responder answers, and the answer appears in Discord.
2. On `/chat`, open the room's **Settings** panel → **Bridge
   troubleshooting**. Type text, press **Post as progress** three times with
   different text, then **Post as reply**. In Discord: one bubble appears
   and is edited twice, then disappears and the reply is posted.
3. From a second Discord account, or after temporarily removing your id
   from `allowed_senders` (Phase G does this), a message is *not* posted;
   the launcher prints one `dropping discord message from user …` warning
   per minute at most.

**Proves:** inbound allowlist, outbound forwarding, progress mirroring, and
that the bot is not a request/response loop (step 2 sends with no Discord
input).

## Phase G — change things while it runs (5 min)

Each change below must take effect within about a second, and the *Process*
pid on the connector pane must **not** change — nothing restarts.

| Do this on /bridges | Expect |
|---|---|
| Untick **Enabled** on the binding | log: `config revision …: 1 binding(s), 0 active`; a Discord message is not posted; a room reply is not sent |
| Tick it again | log: `1 active`; a room reply posted while it was off is now sent (the cursor caught up); a Discord message posted while off is **not** posted (it was consumed while disabled: intentional) |
| Edit `allowed_senders` on the connector, remove your id | your next Discord message is dropped with the warning |
| Put it back | your next message is posted |
| Edit `direction` on the binding → `out` | Discord messages are ignored; room replies still go out |
| Set `direction` back to inherit | both ways again |
| Create a folder, drag the binding into it, untick **Enabled** on the folder | binding pane shows *off in effect: a level above is disabled*; traffic stops |
| Tick the folder again | traffic resumes |

**Proves:** the live configuration contract — edits reach the running
process through the event stream, gates combine with AND, and the process
is never restarted for a policy change.

## Phase H — restart and token rotation (3 min)

1. Press **Restart** on the connector pane. The pid changes; the log shows
   the bot authenticating again; traffic resumes from the persisted cursors
   (nothing is replayed).
2. Press **Replace token…**, paste `wrong`, Save. The connector restarts by
   itself (saving a token rewrites its restart nonce). Expect
   `discord rejected the credential in DISCORD_TOKEN_MAINBOT` in the log, the
   pill `failed` with `exit 2`, and **no** respawn loop (exit 2 means "fix
   the config", so the launcher waits for you).
3. **Replace token…** again with the real one. Running again, no Restart
   press needed.

**Proves:** rotation is one paste, and a rejected credential is reported
instead of retried forever.

## Phase I — the deletion guards (2 min)

1. On `/chat`, open the room's kebab → Delete. The dialog says the room is
   bound by `Main Bot` and Delete stays disabled.
2. On `/bridges`, kebab on `Main Bot` → Delete: refused, "still owns …
   binding(s)".
3. Delete the binding (kebab → Delete). Log: `binding … removed: N bubble(s) cleaned up`
   (0 if none were live). Now the room can be deleted and the connector can
   be deleted — do neither if you want to keep the setup.

**Proves:** ownership is enforced in the database, and removal retires the
worker and cleans its bubbles.

## Phase J — stop (1 min)

Ctrl-C in the launcher terminal: it stops `Main Bot` first, then the core,
and exits. Start it again with the Phase A command: the connector comes back
up on its own because it is enabled, and continues from its state file
`var/services/bridge-<uuid>.json`.

## Alternative to phases B and E: import your existing env-mode setup

If you would rather carry over the cursors of the bridge you already run,
stop that process first, then from `source/discord_service/` with the same
`DISCORD_*` environment it used (the token is not read):

```bash
venv/bin/python import_legacy.py --name "Main Bot" --state-dir ~/git/rainbox/var/services
```

It creates the connector and binding **disabled**, copies the allowlist and
poll interval, and wraps `state.json` into the new state file. Then Phase C
(paste the token on the new connector's pane) and Phase D onwards; the
bridge process will read the token as `--token-env`'s name (default
`DISCORD_BOT_TOKEN`).

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| /settings says *unmanaged* | the core was started directly | run `venv/bin/python main.py` from `source/` |
| Process `credential missing` | no token saved for this connector (or the sealing key is missing so it cannot be opened) | Set token… on the pane; check `RAINBOX_CREDENTIAL_KEY` in `.env` |
| Process `not installed` | `discord_service/venv` missing | `cd discord_service && python3 -m venv venv && venv/bin/pip install -r requirements.txt`, then Restart |
| `failed`, exit 2, "rejected the credential" | wrong token | Replace token… on the pane |
| `failed`, exit 3, "state lock … is held" | another process (your old manual run) owns this connector's state file | stop it, Restart |
| binding never says *activated*; log says `cannot activate … 403 Missing Access` | the bot is not in that server or cannot see the channel | invite it / fix channel permissions; the bridge retries by itself with backoff |
| a change on /bridges has no effect | the bridge's stream to the core is down (log: `stream dropped`, `traffic paused`) | it reconnects with backoff and refetches; nothing is delivered on stale config by design |
| Discord messages posted while the binding was disabled never arrive | intentional: disabled bindings consume them | none |
| the pane's *Process* does not update | it refreshes every 10 s | click the connector again |
