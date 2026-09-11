# Verifying the bridge settings work, step by step

**Date:** 2026-09-12. **Branch:** `bridge-settings` (not merged). Design:
[2026-09-09-bridge-settings-design.md](../specs/2026-09-09-bridge-settings-design.md).

This is an operator walkthrough: each step says what to do, what you should
see, and what it proves. Budget about 30 minutes. Stop at any step that does
not match and read *Troubleshooting* at the end.

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

Nothing here touches the token except your editor and the launcher's
private file; the database only ever stores the variable *name*.

## Phase A — start the launcher on this branch (3 min)

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

**Proves:** the launcher owns the core and can start side processes.

## Phase B — create a connector (2 min)

Open http://127.0.0.1:5000/bridges. Click **+ Connector**:

- Name: `Main Bot`
- Platform: Discord (Telegram and Zulip are listed but greyed out — their
  bridges have no connector mode yet)
- Credential variable name: `DISCORD_TOKEN_MAINBOT`

Click Create. The connector appears in the left tree and its pane opens.
Expect:

- *Credential variable* `DISCORD_TOKEN_MAINBOT` with the note "name only".
- *Desired state*: Enabled unchecked; Launch mode *launcher*.
- *Process*: `unknown` (the launcher has never been asked to run it).
- The **Manual launch command** section shows the launcher's state directory
  and a command — you will not need it today.

**Proves:** the row is stored and the page reads it back; no token involved.

## Phase C — give the launcher the token (2 min)

Create the launcher's private credentials file (the directory already exists
because the launcher is running):

```bash
printf 'DISCORD_TOKEN_MAINBOT=%s\n' 'PASTE-THE-TOKEN-HERE' > ~/git/rainbox/var/services/credentials.env && chmod 600 ~/git/rainbox/var/services/credentials.env
```

One `NAME=value` per line, no `export`, no quotes needed. This file is
git-ignored (`var/services/`) and read only by the launcher, at every spawn.

**Proves nothing yet** — Phase D does.

## Phase D — enable it and watch it start (2 min)

On the connector pane tick **Enabled**. Within a second:

- The launcher terminal prints lines prefixed `[Main Bot]`, ending with
  something like `discord bot <bot name> (<id>); connector 'Main Bot', 0 binding(s) configured`.
- The pane's *Process* pill turns `running (pid …)` with *credential from
  file* (it refreshes every 10 s; click the connector again to refresh now).

If instead the pill says `credential missing`, the message names the
variable and the file path: fix the file, then press **Restart** — the
launcher re-reads the file at every spawn, so no launcher restart is needed.

**Proves:** the desired state reached the launcher over its socket, the
credential was injected by name, and the bot authenticated. Also proves that
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
2. Edit `credentials.env` and change the token to `wrong`, press
   **Restart**. Expect `discord rejected the credential in DISCORD_TOKEN_MAINBOT`
   in the log, the pill `failed` with `exit 2`, and **no** respawn loop
   (exit 2 means "fix the config", so the launcher waits for you).
3. Put the real token back, press **Restart**. Running again.

**Proves:** rotation is edit-file-then-Restart, and a rejected credential is
reported instead of retried forever.

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
poll interval, and wraps `state.json` into the new state file. Continue at
Phase C with `--token-env`'s name (default `DISCORD_BOT_TOKEN`), then Phase D.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| /settings says *unmanaged* | the core was started directly | run `venv/bin/python main.py` from `source/` |
| Process `credential missing` | the variable is not in `credentials.env` (or a typo in the name) | fix the file, press Restart |
| Process `not installed` | `discord_service/venv` missing | `cd discord_service && python3 -m venv venv && venv/bin/pip install -r requirements.txt`, then Restart |
| `failed`, exit 2, "rejected the credential" | wrong token | fix the file, Restart |
| `failed`, exit 3, "state lock … is held" | another process (your old manual run) owns this connector's state file | stop it, Restart |
| binding never says *activated*; log says `cannot activate … 403 Missing Access` | the bot is not in that server or cannot see the channel | invite it / fix channel permissions; the bridge retries by itself with backoff |
| a change on /bridges has no effect | the bridge's stream to the core is down (log: `stream dropped`, `traffic paused`) | it reconnects with backoff and refetches; nothing is delivered on stale config by design |
| Discord messages posted while the binding was disabled never arrive | intentional: disabled bindings consume them | none |
| the pane's *Process* does not update | it refreshes every 10 s | click the connector again |
