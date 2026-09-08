# Where chat-bridge settings live

**Date:** 2026-09-09
**Status:** design note (no implementation yet)
**Applies to:** `discord_service/`, `telegram_service/`, and any future
bridge (Zulip is the likely next one)

## The question

`discord_service` and `telegram_service` are configured entirely by
environment variables. That is fine for a process you restart freely and
wrong for settings you want to change while the system runs. A `.env` file
does not fix it: the values are still read once at process start, so
changing one means restarting whoever read it.

The core (`main.py`) must not be restarted casually — agents may be
mid-turn. The bridge processes may. **That restart boundary is the whole
design.** Put a setting where the process that must restart to see it is a
process you are willing to restart.

## Three homes, one rule each

| Home | Rule | Bridge examples |
|---|---|---|
| **Environment (deployment facts)** | Needed to reach the DB or identify this process before any config is readable | `DATABASE_URL`, `RAINBOX_URL`, `BRIDGE_STATE_FILE`, `BRIDGE_CONNECTOR` |
| **Environment (secrets)** | Never in Postgres — cleartext there means cleartext in every backup | bot tokens, Zulip API keys |
| **Postgres (operator settings)** | Everything else: read fresh at use, editable in the UI, no restart | which channel maps to which room, allowlists, poll interval, forwarded kinds, enabled flags |

The secret rule is not new. `db/settings.py` already enforces it:
`secret=True` settings are env-backed and refuse to store a value, for the
reason given in `notes/backup.md`. Bridge tokens are the same class of
thing and get the same treatment.

This split lands exactly on the boundary you described. Everything you
change often is in the DB, so the core keeps running and the bridge picks
it up live. The one thing that stays in env is the one thing that almost
never changes, and its only reader is a process you are happy to restart.

## Rows, not settings keys

`app_setting` is a flat key–value table with a code-side registry, and its
own proposal deliberately refused a `scope` column. Bridge configuration
does not fit it: there is no fixed set of keys, there are *rows* — many
connectors, many bindings, created and deleted from a UI. That is
`model_config` / `cron_job` shaped, so it gets real tables.

## Schema sketch

```python
class BridgeConnector(db.Model):
    """One bot identity on one platform: what to talk to and who we are."""
    __tablename__ = "bridge_connector"
    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[UUID] = mapped_column(unique=True, default=uuid4)
    name: Mapped[str] = mapped_column(Text, unique=True)   # "mainbot", referenced by BRIDGE_CONNECTOR
    platform: Mapped[str] = mapped_column(Text)            # "discord" | "telegram" | "zulip"
    # Zulip needs its realm URL; Discord/Telegram use the platform's fixed API base.
    base_url: Mapped[str | None] = mapped_column(default=None)
    # Zulip authenticates as email + API key; the email is not a secret.
    identity: Mapped[str | None] = mapped_column(default=None)
    # The NAME of the env var holding the credential — never the credential.
    token_env: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(default=True)
    policy: Mapped[dict] = mapped_column(JSON, default=dict)   # connector-level defaults


class BridgeBinding(db.Model):
    """One remote conversation <-> one rainbox room."""
    __tablename__ = "bridge_binding"
    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[UUID] = mapped_column(unique=True, default=uuid4)
    connector_uuid: Mapped[UUID] = mapped_column()          # plain col, no FK (house style)
    room_uuid: Mapped[UUID] = mapped_column()               # chatroom.uuid
    # Platform-shaped remote address. NOT a scalar id — see below.
    address: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(default=True)
    policy: Mapped[dict] = mapped_column(JSON, default=dict)  # overrides the connector's
    folder_uuid: Mapped[UUID | None] = mapped_column(default=None)
    position: Mapped[int] = mapped_column(default=0)
```

Plus a `bridge_folder` table mirroring `chatroom_folder` for grouping.

### The address must not be one id column

This is the single decision that keeps the design from being
Discord-shaped. The three platforms address a conversation differently:

```json
{"channel_id": "…"}                          // Discord
{"chat_id": …}                               // Telegram
{"stream": "engineering", "topic": "rainbox"} // Zulip — a PAIR, topic optional
```

A `channel_id` column would force Zulip into a fiction. A JSON `address`
validated per platform by a small code-side registry (same shape as the
`SETTINGS` registry: which keys are required, how to render them) costs
nothing now and is the difference between adding Zulip and rewriting for
it. Discord's guild id, if wanted, is another key in that blob — a server
is not a level, it is metadata.

## Inheritance

Four levels, nearest non-null wins:

```
code defaults (per platform, in the registry)
  └─ connector.policy
       └─ folder chain, root → leaf
            └─ binding.policy
```

Null means inherit; that is already the repo's idiom
(`Chatroom.request_timeout` null = the model config's, `history_window`
null = the whole room, `chat.default_model` under a room's own model).
Folder-chain inheritance has direct precedent too:
`_cron_job_effective_enabled` walks every ancestor folder before deciding a
job is live.

Letting the **folder tree** carry inheritance is what makes the hierarchy
generic. You arrange folders however the situation demands — one per
Discord server, one per platform, one per purpose — and no platform
concept is baked into the schema. It also inherits the UI conventions: a
`/bridges` page follows `notes/ui-left-panel-tree.md` (mirror `/cron`'s CSS
and JS) and renames go through the modal in `notes/ui-modal-rename.md`.

Policy keys worth having, all resolvable at any level:

| Key | Meaning |
|---|---|
| `allowed_senders` | remote user ids permitted to post inbound |
| `forward_kinds` | which row kinds go out, e.g. `["message","notice","progress"]` |
| `poll_seconds` | inbound poll interval |
| `mirror_progress` | edit one remote message in place vs post each update |
| `replay_history` | whether a fresh binding backfills |
| `direction` | `both` / `in` / `out` |

## Secrets with more than one bot

`token_env` per connector is what makes multiple bots work without putting
any credential in Postgres:

| Connector | `token_env` | Where the value lives |
|---|---|---|
| `mainbot` | `DISCORD_TOKEN_MAINBOT` | repo-root `.env`, or the shell |
| `testbot` | `DISCORD_TOKEN_TESTBOT` | same |
| `zulip-work` | `ZULIP_KEY_WORK` | same |

`.env` is a fine transport for these — `env_file.py` already loads the
repo-root file, and it is gitignored. The point is not that env vars are
better; it is that the value never reaches Postgres, never reaches a
backup, and never crosses the core's unauthenticated local HTTP API.

Run **one process per connector**, selected by `BRIDGE_CONNECTOR=mainbot`.
It reads only its own token, and restarting one bot leaves the others
alone. One process serving several connectors is possible but buys
nothing here.

## How the bridge sees changes without a restart

The bridge already polls. Have it re-fetch its config on the same cycle:

```
GET /bridge/api/connectors/<name>/config
  -> {connector: {...}, bindings: [{uuid, room_uuid, address, policy resolved}]}
```

Secrets are absent by construction, so this endpoint exposes nothing the
`/chat` API does not already. A changed policy applies on the next cycle;
an added or removed binding is a rebind, not a restart. Only two things
still need a process restart: the credential itself, and which connector
this process is.

So in practice you stop restarting the bridge too, even though you were
willing to.

## Cursors stay in the service's state file

Per-binding bookkeeping (`discord_after`, `room_cursor`, the progress
message map) is high-write process state, not operator configuration. In
Postgres it would churn every backup for no gain. Keep it in the state
file, but key it **by binding uuid** rather than flat, so adding or
removing a binding cannot disturb another's position:

```json
{"bindings": {"<binding-uuid>": {"remote_after": "…", "room_cursor": 42,
                                 "progress_messages": {}}}}
```

## Getting there from today

Additive, with env as the fallback layer — the same precedence
`get_setting()` already uses (DB → env → default):

1. Build the tables, the resolver, the config endpoint, and the `/bridges`
   page. Nothing else changes.
2. `discord_service` prefers a connector named by `BRIDGE_CONNECTOR`; with
   none set it falls back to today's `DISCORD_*` variables, so an existing
   command line keeps working.
3. `telegram_service` gets the same fallback when it is next touched. Its
   env vars keep working until then.
4. `DISCORD_CHANNEL_ID`, `DISCORD_ALLOWED_USER_IDS`, `DISCORD_ROOM_NAME`
   and `DISCORD_POLL_SECONDS` become DB-backed. `RAINBOX_URL` and the state
   file path stay env: they are deployment facts.

## Does it actually hold for Zulip?

Worth checking before committing, since Zulip is the reason for the
generality:

- **Realm URL** — `base_url` on the connector. Discord and Telegram leave
  it null.
- **Email + API key** — `identity` plus `token_env`. Discord and Telegram
  leave `identity` null.
- **Stream + topic** — the JSON `address`, with `topic` optional meaning
  the whole stream.
- **Long-polling `/events` instead of REST polling** — an implementation
  detail of that platform's client, invisible to the schema.
- **Message edits and reactions** — Zulip supports both, so the progress
  bubble mirroring maps cleanly.

Nothing about the model resists Zulip, and nothing in it is Discord-only.
If Zulip replaces Discord entirely, connectors and bindings are re-pointed;
the tables, resolver, UI and endpoint are untouched.

## Not building yet

- Operator-editable per-platform defaults. The code registry covers it; if
  a real need appears it is one more level in the resolver, or a folder.
- DB-held cursors, one process for many connectors, per-binding rate
  limits, inbound attachments.
- Anything multi-tenant. This stays single-operator; the folder tree is
  organization, not access control.

## The one open choice

Whether Discord stays. If Zulip wins on being open source, this design
absorbs that as a new `platform` value plus a client module, and the work
already done on the Discord bridge (the row-kind mapping, progress
mirroring, at-least-once inbound, the reconcile-on-reconnect logic) carries
over unchanged, because none of it is about Discord's API.
