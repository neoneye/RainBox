# Where chat-bridge settings live

**Date:** 2026-09-09

**Status:** design note; none of the proposed tables, endpoints, or live reload
behavior is implemented yet.

**Roadmap:** the [launcher](2026-09-10-launcher-design.md) is implemented
(`source/main.py`, merged 2026-09-10) and this design resumes on top of it;
the *Supervised by the launcher* section lists the launcher-side extension
this phase adds. Originally the launcher came first;
this design resumes once it runs the core and the static services reliably.
The process-management mechanism lives there and is only summarized here.

**Applies to:** `source/discord_service/`, `source/telegram_service/`, and
future bridges for Zulip, Signal, Slack.

## Decision and current behavior

Keep deployment configuration and credentials in the bridge process's
environment. Store operator-editable connectors, bindings, and policies in
Postgres, exposed to bridges through the core's HTTP API. Keep delivery
checkpoints in each bridge's local state file.

The core (`source/core.py`) should not need a restart for ordinary bridge
configuration changes: agents may be mid-turn. Each connector runs in its own
bridge process, which can be restarted independently. By default a small
top-level launcher (`source/main.py`) starts the core and every enabled
service assigned to it, and stops that service when disabled (see [Supervised
by the launcher](#supervised-by-the-launcher)). Manual operation remains possible.
Installing the initial schema and application code requires the normal deployment procedure;
live editing is a property of the implemented feature, not a way to hot-load
new core code.

Today both bridges read environment variables at startup and use HTTP only;
neither needs database credentials or imports the core's database models.
Discord binds one configured channel to a room found by name. Telegram binds
one room, but learns its outbound chat ID from an allowed inbound message.
Both have an inbound worker and an independent outbound SSE worker. Updating
only the inbound polling loop would leave the outbound worker using stale
configuration.

## Three homes

| Home | Rule | Examples |
|---|---|---|
| Environment: deployment | Needed before fetching configuration, or specific to the host/process | `RAINBOX_URL`, `BRIDGE_CONNECTOR`, state-file path |
| Environment: credentials | Credential values never enter the bridge tables or config API | Discord/Telegram bot tokens, Zulip API key |
| Postgres: operator settings | Validated edits take effect in the running bridge | Bindings, allowlists, direction, enabled flags, forwarding policy |

`DATABASE_URL` remains a **core** deployment setting. Bridges fetch configuration
through HTTP and do not connect directly to Postgres. `BRIDGE_CONNECTOR` is
proposed below; existing state-file variables remain `DISCORD_STATE_FILE` and
`TELEGRAM_STATE_FILE`. A common `BRIDGE_STATE_FILE` name can be added as an
explicit alias during migration, with precedence documented then.

This follows `source/db/settings.py:set_setting`, which rejects non-null values
for secret-flagged settings. The scope here is bridge credentials, not a claim
that the whole database is secret-free: `source/notes/backup.md` documents
existing model API keys in `model_config`. Backups are encrypted, but any secret
stored in the database is also present in the decrypted dump.

`app_setting` is a registry of fixed keys. User-created connectors and bindings
are collections of rows, like model configurations and cron jobs, so they need
real tables rather than dynamically generated setting keys.

## Data model

All three tables use the repository's integer primary key, unique UUID, and
created/updated timestamps. UUIDs are the persistent API and state identities;
names are editable labels. The following fields describe the contract, not a
complete SQLAlchemy migration.

| Table | Fields in addition to common identity/timestamps |
|---|---|
| `bridge_connector` | `name` (unique label), `platform`, nullable `base_url` and `identity`, `token_env`, `launch_mode` (`launcher` or `manual`, default `launcher`), `restart_nonce` (UUID, default new UUID), `enabled` (default false), `policy` (JSON object, default `{}`) |
| `bridge_folder` | `connector_uuid` (FK `RESTRICT`), `parent_uuid` (nullable, plain uuid), `name`, `position`, `enabled` (default true), `policy` (JSON object, default `{}`) |
| `bridge_binding` | `connector_uuid` (FK `RESTRICT`), `folder_uuid` (nullable, plain uuid), `room_uuid` (FK `RESTRICT` to `chatroom.uuid`), `address` (validated JSON object), `address_key` (canonical text), `enabled` (default false), `policy` (JSON object, default `{}`), `position` |

`bridge_folder` follows the tree shape of `ChatroomFolder`, adding policy and
an enabled gate. Folders belong to exactly one connector. This makes sender IDs
and platform-specific policy unambiguous: a Discord server can be represented
by a folder under its bot, without introducing a server table. Cross-connector
folders and defaults are deferred.

Required invariants:

- `platform` is an installed adapter's registered name. Discord and Telegram
  require null `base_url` and `identity`; Zulip requires a realm URL and bot
  email. Unknown policy keys, unsupported features, malformed addresses, and
  invalid types are rejected on write and on configuration load.
- References must exist; a binding's folder and every ancestor must belong to
  its connector. Reject cycles, missing parents, and cross-connector moves.
  Index connector membership and `(parent_uuid, position)` for tree reads.
- Enforce uniqueness of `(connector_uuid, address_key)` in the database,
  including disabled bindings. The adapter derives `address_key` from the
  normalized routing fields; optional display metadata is excluded. One remote
  conversation maps to one room per connector. Several distinct conversations
  may intentionally share a room, whose agent output reaches each enabled
  outbound binding.
- Connector platform, realm, bot identity, and credential-variable reference
  are fixed after creation. Binding connector, room, and address are also
  fixed. Name, policy, position, and enabled state remain editable. To point
  a remote conversation at a different room, **delete** the old binding and
  create a new one: the uniqueness rule above covers disabled bindings, so a
  disabled replacement with the same address cannot coexist with the old row.
  The core deletes only the configuration row; the bridge retires the old
  binding's local state after observing its removal and draining in-flight
  work. The new UUID never inherits that state and initializes at current
  high-water marks on first activation. Do not reset the connector-wide
  Telegram offset when retiring one binding.
- Deleting a referenced room is rejected until its bindings are removed.
  The room delete dialog already runs on a rollup
  (`source/db/chat.py:chatroom_delete_preview`, served at
  `/chat/api/rooms/<uuid>/delete-preview`); extend that rollup with the
  room's bindings so the refusal is visible before the attempt. The chat
  *folder* delete (`source/db/chat.py:delete_chatroom_folder`) recursively
  deletes every room in the subtree; extend `chatroom_folder_delete_preview`
  at `/chat/api/folders/<uuid>/delete-preview` with the same binding blockers.
  Both previews include disabled bindings, a `can_delete` flag, a total
  `binding_count`, and blocker references (binding, connector, and room UUIDs)
  that the UI can link to. A blocked dialog explains which bindings to remove
  and keeps Delete unavailable even after the confirmation name is entered.
  A preview is informational, not permission to delete: both DELETE handlers
  must recheck current references in the deletion transaction and return HTTP
  409 with current blockers if any exist. Recursive deletion is all-or-nothing;
  never delete unbound rooms first and discover a bound room partway through.
  `delete_chatroom_folder` already commits the whole subtree once at the end,
  and the guard itself is the database's: `bridge_binding.room_uuid` is a real
  foreign key with `ondelete="RESTRICT"` (below), so deleting a bound room
  fails inside that transaction and the whole subtree delete rolls back. A
  binding inserted after any application-side check is caught the same way.
  The handlers roll back the integrity error and answer 409 with current blockers;
  the preview query is a courtesy, not the enforcement.
  Reject deletion of nonempty bridge folders/connectors; the UI can offer an
  explicit transactional removal of their contents. A move or deletion
  validates and commits the whole change together. Do not silently repair a
  missing reference by routing to another room.

### Referential integrity and concurrency

Use database constraints for ownership and a version check under a connector
row lock for bridge-tree edits:

- **Real foreign keys where a delete must be refused or cascaded.**
  `model_config` is protected with `ForeignKey(..., ondelete="RESTRICT")`;
  `chatroom_member` and `chat_message` cascade from `chatroom`. Bridge rows
  follow that split: `bridge_binding.room_uuid` and
  `bridge_binding.connector_uuid` are `RESTRICT` foreign keys, and
  `bridge_folder.connector_uuid` likewise, so a bound room, a nonempty
  connector, or a connector with folders cannot be deleted by any path — UI,
  admin, import, or a raw session — and the database prevents dangling references
  even when application checks race. `RESTRICT` is not a deferred commit-time
  check: the statement can fail
  before commit. Catch failures from flush/execute as well as commit, roll back
  the failed transaction, and query current blockers in a new transaction
  before returning HTTP 409. See [PostgreSQL foreign-key constraints](https://www.postgresql.org/docs/current/ddl-constraints.html#DDL-CONSTRAINTS-FK).
- **Plain uuid columns with a version token for placement.** `folder_uuid`
  and `parent_uuid` stay plain columns validated app-side, the cron/chat
  house style, and the `/bridges` tree uses the six-endpoint shape in
  `source/notes/ui-tree-persistence.md`: the page hydrates with an opaque
  version token and echoes it on PUT; a stale token is a 409 and the client
  re-hydrates. A token comparison alone does not serialize two simultaneous
  saves: both can read the same token before either commits. Every bridge
  mutation of an existing connector first locks its row with
  `SELECT … FOR UPDATE`, then
  re-reads/validates the tree and checks the token before changing anything.
  Folder/binding creation, deletion, moves, and policy edits use this same
  transaction boundary; connector deletion locks that row too. Roll back on
  validation failure. This protects app-validated placement references without
  adding a global lock to unrelated chat or cron operations. Direct SQL changing
  plain placement columns remains outside this application guarantee. If a tree
  PUT spans connectors, lock all affected connector rows in UUID order and
  validate a token covering those rows; never compare a global token while
  locking only one connector. New connector creation relies on DB uniqueness
  and a server-generated UUID, since there is no row to lock yet.

What that does not cover is the same thing it does not cover for chat today:
an *unbound* room moved into a folder while that folder's recursive delete is
committing. That is existing behavior of the chat tree, not a bridge concern,
and a bound room is protected regardless.

Config reads and delete previews assemble a coherent response from several
tables. Run them in a read-only REPEATABLE READ transaction so a folder policy
edit committing mid-read cannot yield a snapshot mixing old policy with new
membership. Set isolation and read-only mode before the first query, using a
fresh transaction if request setup has already queried through `db.session`.
Resolve and serialize inside it, then explicitly end the transaction before
returning the HTTP response. Never hold a database transaction across a bridge
network call.

### Platform addresses

IDs are canonical decimal **strings** in JSON, preserving Discord snowflakes
through JavaScript and signed Telegram chat IDs. Adapters validate their own
ID format and convert to API types at the boundary.

| Platform | Example address | Initial supported scope |
|---|---|---|
| Discord | `{"channel_id":"123456789012345678"}` | One text channel; guild ID may be display metadata |
| Telegram | `{"chat_id":"-1001234567890"}` | One chat; forum-topic routing is deferred |
| Zulip | `{"stream_id":"42","topic":"rainbox"}` | One channel and one explicit, nonempty topic |

A Zulip channel-wide inbound subscription is a different routing feature: it
needs both overlap rules and an outbound-topic decision. Omission of `topic`
must not silently mean “send to the whole stream.” The first adapter requires
an explicit topic in both directions. Zulip's [send-message
API](https://zulip.com/api/send-message) distinguishes the channel destination
and topic; the registry must reflect that rather than treating every platform
as a scalar channel ID.

## Policy resolution and enabled gates

Resolve each policy key independently, in this order:

```text
platform code default
  -> connector.policy
  -> connector's folder chain, root to leaf
  -> binding.policy
```

A missing key or JSON null means inherit. A present non-null value replaces
the previous value. Lists replace lists; there is no concatenation or deep
merge. In particular, `[]` means an empty list and `false` is an explicit value.
The registry defines types, defaults, validation, and adapter capabilities.
The UI shows the effective value and which level supplied it.

| Key | Default and meaning |
|---|---|
| `allowed_senders` | `[]`: deny all inbound. A list of platform user-ID strings, not usernames or rainbox UUIDs. A child can replace its parent's list; the UI must show this clearly. |
| `forward_kinds` | Discord: `["message","notice","progress"]`; Telegram: `["message"]`. Only supported agent row kinds can be forwarded; human, thinking, and debug rows remain excluded. |
| `poll_seconds` | Discord: `2`; finite number in `[0.5, 300]`. Controls channel polling, not config refresh. Reject for adapters that do not use per-binding polling. |
| `mirror_progress` | Discord: `true`; edits one remote bubble per progress row. When false, sends each supported progress update separately. Unsupported by the current Telegram adapter. Ignored when `progress` is excluded. |
| `direction` | `both`; accepted values `both`, `in`, `out`, relative to rainbox. |

Bot/self-authored messages are always excluded inbound, regardless of
`allowed_senders`, to avoid bridge echo loops. No wildcard allowlist is part of
this design. Folders are operator organization, not an access-control boundary;
allowlist overrides intentionally can broaden access.

Enabled state is **not** nearest-value inheritance:

```text
effective_enabled = connector.enabled
                    AND every ancestor folder.enabled
                    AND binding.enabled
```

A child cannot override a disabled ancestor. This matches the enabled gate in
`source/db/cron.py:_cron_job_effective_enabled`, not a general policy merge.
Unlike that helper's permissive missing-parent handling, invalid bridge trees
fail closed. New connectors and bindings start disabled so configuration can
be completed before any traffic flows.

The `/bridges` page follows `source/notes/ui-left-panel-tree.md` and the rename
modal in `source/notes/ui-modal-rename.md`. Show both local and effective enabled
state, inherited values, and validation errors. Moving a binding between
folders changes its effective policy and must be shown as such.

## Credentials and process identity

Run one process per connector, selected by **UUID**, for example
`BRIDGE_CONNECTOR=<connector-uuid>`. A rename cannot break startup or reload.
With autostart on and `launch_mode=launcher` (the defaults), the launcher starts
an enabled connector. Use `launch_mode=manual` for a bridge hosted elsewhere or
for debugging one connector without turning off every other bridge. Switching
ownership stops any launcher-owned instance; wait for it to exit before starting
manually. Switching back requires stopping the manual instance first.
The `/bridges` tree gives each connector the same **Copy ID** kebab item the
chat and cron trees have. The detail panel also offers a copyable launch command
for the connector's platform, labeled with its required working directory
(`source/discord_service/` or `source/telegram_service/` on the bridge host).
Include `BRIDGE_CONNECTOR` and a distinct state-file path derived from its UUID,
using the platform's existing state-file variable. On the launcher's host use
its reported absolute state directory, so manual and supervised runs use the
same `bridge-<uuid>.json` and lock. If the host/path is unknown, require an explicit
local state-directory choice before presenting a runnable command; do not guess
it from the core checkout. Interpreter and script paths are relative to the
shown service directory. Two commands must not both use `./state.json`. Show `RAINBOX_URL` as a separate deployment prerequisite when
the default localhost endpoint is unsuitable.

Name the required credential variable beside the command as “must already be
set in the launch environment”; do not insert an empty or placeholder token
assignment that would overwrite it. Validate `token_env` as an environment
variable name (`[A-Za-z_][A-Za-z0-9_]*`), reject reserved bridge/deployment names
such as `BRIDGE_CONNECTOR`, `RAINBOX_URL`, and state-file variables, and shell-quote
generated argument values. Connector display names never become shell syntax.
`token_env` stores only the name of its credential variable, such as
`DISCORD_TOKEN_MAINBOT`; it never stores a value.

The process reads that variable locally. Empty/missing credentials prevent
activation and produce a redacted error. Rotation under the same variable name
requires restarting only that connector. A replacement token must authenticate
as the same bot; verify and retain the authenticated bot identity in local state
so a different bot cannot inherit the previous bot's checkpoints.

The bridges do **not** load `.env`. Under supervision the launcher supplies the
credential at every spawn from its private `<state-dir>/credentials.env`
first, and only for a name the file does not set, from its own startup
environment — so editing the file and pressing Restart rotates the token
without restarting the launcher. Manual runs receive credentials from their
launch environment. The [launcher design's
environment contract](2026-09-10-launcher-design.md#environment-and-credential-ownership)
is the authority; do not add a second bridge-side dotenv loader.

Only the selected credential is passed to a supervised bridge. This does not
mean the core can never see it: credentials exported to the launcher can reach
the core, and the core's existing `source/env_file.py` loads all keys from the
repo-root `.env`. Use the private launcher credentials file to keep bridge-only
values out of the core's environment. Environment filtering is not an OS sandbox.

Keeping credential values out of JSON does not make configuration harmless.
The core API is currently unauthenticated and bound to localhost; connector
configuration belongs within that same trusted operator boundary. An editable
realm URL determines where credentials are sent. Validate HTTPS realm URLs
without userinfo/query/fragment, reject cross-origin authentication redirects,
and keep realm/token-reference changes outside ordinary live policy edits.
The bridge should receive only its own credential in its launch environment.
Never return credential values, environment dumps, or token-bearing URLs in
config responses or logs. `token_env` names and binding metadata may be returned.

## Transport between bridge and core

The bridge is not a child of the core in any sense: the launcher starts
both, as siblings. On the supervised host they use HTTP at
`http://127.0.0.1:5000`, in two forms:

- **Request and response, JSON.** Find the room, post an inbound message,
  fetch a row by id, and fetch the resolved config snapshot from
  `GET /bridge/api/connectors/<uuid>/config`.
- **One long-lived streaming response, Server-Sent Events.** The bridge holds
  `GET /chat/stream` open and reads `data:` lines as the core emits them —
  the same stream the browser uses. Behind it the core listens on a
  Postgres NOTIFY channel and forwards each payload. The same stream carries
  `{"event": "bridge_config", "connector_uuid": …}` whenever a connector,
  folder, or binding row changes, so a bridge learns that its configuration
  moved without ever asking; browsers ignore that event type.

No pipe, no RPC framework, no message broker, no shared files, no database
connection: the bridge's state file is private to the bridge, and the bridge
never holds `DATABASE_URL`. The core exposes nothing to a bridge that a
browser on the same machine could not already reach, so a bridge inherits the
existing localhost trust boundary instead of adding a channel. A manually
hosted remote bridge needs an operator-provided secure tunnel to that loopback
endpoint (and an appropriate `RAINBOX_URL`); this design does not expose the
unauthenticated core on an external interface or add remote supervision.

This is deliberately *not* the mechanism the supervisor uses for its agents.
`core.py` spawns each agent as `python -m agents --socket-fd N` over a
`socketpair()`, writes the agent's config down that socket, and reads
heartbeats and status back up it. Agents need config injected because they
have no other way to receive it; a bridge fetches its config over HTTP when
the stream tells it to — at connect and on a `bridge_config` event — and
owns its own transport/config recovery. Nothing in the bridge polls for
configuration; like the launcher, it sleeps until something happens. The
launcher observes process liveness and exit status and sends signals; the
core's agent supervisor does not own or control bridge processes. The
launcher reads a bridge's stdout/stderr like any child's and prints each
line prefixed with the entry's label (see below), so several bridges' logs
stay attributable on one terminal.

## Supervised by the launcher

The [launcher design](2026-09-10-launcher-design.md) owns process lifecycle,
credentials, status, and retries. Extend its local catalogue with one kind per
implemented bridge adapter. The core returns one desired entry for **every**
connector, including disabled and manual connectors; deletion alone removes the
key from a complete desired snapshot.

```json
{
  "key": "bridge:<connector-uuid>",
  "kind": "discord_bridge",
  "label": "discord mainbot",
  "enabled": true,
  "restart_nonce": "<connector-restart-nonce>",
  "env": {
    "RAINBOX_URL": "http://127.0.0.1:5000",
    "BRIDGE_CONNECTOR": "<connector-uuid>"
  },
  "token_env": "DISCORD_TOKEN_MAINBOT",
  "state_file": {
    "env": "DISCORD_STATE_FILE",
    "name": "bridge-<connector-uuid>.json"
  }
}
```

The catalogue maps `discord_bridge` to `discord_service/venv/bin/python
bridge.py`, and likewise for Telegram. It defines each adapter's state-file
variable; the payload supplies no executable or directory. The launcher
validates that the key UUID, `BRIDGE_CONNECTOR`, and state basename agree, rejects
path separators/traversal in the basename, and supplies the absolute
`<state-dir>/bridge-<uuid>.json` through the named variable. It never edits bridge
state itself. `env` cannot also supply or override that state-file variable.
`label` is display-only — the connector's name, sanitized to one printable
line — used for the log-line prefix and the status table; the `key` stays the
stable identity, so a rename changes the prefix and nothing else.

### What this adds to the launcher as built

The launcher (`source/main.py`, `source/services/`) handles static services
only. This phase extends it, all under existing seams:

- **Catalogue.** `services/definitions.py` gains one `ServiceKind` per
  adapter (`discord_bridge`, `telegram_bridge`) with `directory`, `argv`, the
  adapter's state-file variable name, and `dynamic=True`; a dynamic kind
  never appears as a static entry and has no `services.<key>.enabled`
  setting of its own.
- **Validation.** `validate_desired` accepts keys of the form
  `bridge:<uuid>` whose kind is dynamic, and for those entries only,
  `token_env`, `state_file`, and `label`; static entries still reject them.
  The rule that every static kind must be present stays; dynamic entries
  are whatever the snapshot lists, and a key absent from a valid snapshot
  is a removal.
- **Spawn.** For a dynamic entry the launcher resolves `token_env` through
  its existing `resolve_credential` (file first, boot environment second)
  and, when neither has it, reports the already-defined `credential missing`
  state with the source it looked in and spawns nothing; it sets the
  state-file variable to `<state-dir>/<name>`; everything else — the
  filtered environment, the process group, exit-code mapping, backoff,
  the crash budget, the two-phase shutdown — is unchanged.
- **Status.** `LauncherStatus.view` currently lists the core and the static
  keys; it must also carry every reported dynamic key so `/bridges` can show
  its connectors. The label rides along for display.
- **Restart.** `POST /services/api/restart/<key>` with a `bridge:<uuid>`
  key rewrites the connector row's nonce rather than a settings key.

### Which change goes down which channel

Two different pushes serve two different processes, and a write must trigger
the right one:

| Change | Reaches | Over |
|---|---|---|
| connector `enabled`, `launch_mode`, `services.bridges.autostart`, a Restart, a launch-environment edit | the launcher | the control socket: `CHANNEL.push_desired()` in the committing request |
| binding, folder, policy, connector name/realm edits that do not change the launch environment | the bridge process | the SSE stream: a `bridge_config` event from the same transaction |

A change of the first kind also reaches the bridge indirectly — the launcher
stops or restarts it. A change of the second kind never touches the process.
The desired snapshot the core pushes must read the connector rows and the
`services.*` settings in one consistent transaction (REPEATABLE READ, as the
config endpoint already does), so an autostart flip cannot pair with a stale
connector row.

The entry's desired `enabled` is:

```text
services.bridges.autostart AND connector.launch_mode == "launcher"
                           AND connector.enabled
```

`services.bridges.autostart` is a registry bool (default true). Folder/binding
enablement never controls whether the process runs: an enabled connector must
keep listening for change events even with no active bindings. `launch_mode`
controls ownership, not forwarding permission; the bridge's effective-enabled
rule is unchanged. `--core-only` suppresses bridge launches too, exactly as it
does static services, and the status row says so.

Persist `restart_nonce` on the connector row, not as dynamic `app_setting` keys.
Restart replaces it; every transition of the desired launch gate from false to
true replaces it too, including autostart and launch-mode changes. Update gate
and affected nonces atomically, locking affected connector rows in UUID order;
the global autostart handler must not commit its setting separately from those
rows. Policy, binding, folder, and name edits do not
change it. Restart does not enable a connector. For manual connectors the UI
instructs the operator to restart their process instead of queuing a launcher
restart that cannot take effect.

A supervised connector disable stops its process; a manual connector disable
pauses its workers while it keeps listening for a change event. Both preserve matching
state for the next activation. Binding disable only pauses that binding in
either mode, including the Telegram inbound-discard exception below. Process
shutdown is not binding deletion: do not delete remote progress bubbles or prune
binding state merely because SIGTERM arrived. A launcher stop has 10 seconds of
grace and may interrupt a 30-second removal cleanup; retain safe checkpoints and
report orphaned progress state on the next start as specified below.

**Exit codes.** Exit 2 for local startup validation failure or a platform's
confirmed credential rejection, and 3 for a held state lock. Config HTTP 404,
timeouts, schema errors, missing rooms, and live policy changes follow the
pause/refresh contract below; they do not become permanent process failures.
Rate limits/network errors are not proof of a rejected credential. Assigning
these codes and graceful worker shutdown are implementation work; existing
`SystemExit("message")` paths exit 1.

**Status and manual ownership.** `/bridges` shows the launcher's process status
and Restart action using the stable `bridge:<uuid>` key, alongside desired
connector enablement and launch mode. `running` means a PID exists; it does not
prove fresh config or successful forwarding. Detailed bridge transport/cleanup
errors remain in bridge logs unless a separate health-reporting API is added.
Manual connectors have externally managed process status, not inferred failure.
A duplicate using the same state path loses its lock and exits 3. If the manual
process loses, the healthy supervised process stays `running`; only a failed
launcher-owned attempt appears as launcher `failed`. Different state paths or
hosts do not share that lock, so ownership changes require an explicit handoff.

## Live configuration contract

```text
GET /bridge/api/connectors/<connector-uuid>/config
  -> {
       schema_version: 1,
       revision: <opaque revision of this complete resolved snapshot>,
       connector: {uuid, name, platform, base_url, identity, token_env, enabled},
       bindings: [{uuid, room_uuid, address, effective_enabled, policy}]
     }
```

The core resolves a coherent database snapshot, including folder policies and
enabled gates, before producing the response. `revision` changes whenever an
effective setting or binding membership changes; it is not merely the
connector's `updated_at`. Include disabled bindings so disable and deletion
remain distinguishable. Use a strict response schema; never serialize ORM rows
or environment values wholesale.

The bridge fetches this snapshot exactly when it can have changed: once when
its SSE stream (re)connects, and again whenever that stream delivers a
`bridge_config` event naming its connector. There is no refresh timer. The
core emits the event from the same transaction that commits any connector,
folder, or binding write (the `NOTIFY` the chat stream already forwards), so
a toggle on `/bridges` reaches the bridge in milliseconds. Bound each config
request to 5 seconds and allow only one in flight; a second event while one
is in flight schedules one more fetch, not one per event. Validate the
entire response before atomically publishing an immutable snapshot to
**both** workers. Workers check its freshness, current binding, enabled
gate, and policy before each delivery request (send/post/edit/delete),
including each chunk of a long message and each retry inside the adapter. Connector-wide inbound polling checks freshness
and connector enablement; each returned update is routed under current binding
policy before posting. Checking once around `send_message()` is insufficient: the current
Discord client sends multiple chunks and retries 429s internally. Removal
cleanup has the narrow exception defined below. Requests already in flight
may complete after a disable. Edits neither cancel agent turns nor restart
the core.

| Event | Required behavior |
|---|---|
| Policy/folder change | Publish a new snapshot; preserve checkpoints; wake affected workers. |
| New binding | Validate destination, initialize at current high-water marks, persist state, then activate; no history replay. |
| Disable | Stop new traffic for that binding when the snapshot is applied. Retain checkpoints and progress mappings for reconciliation; do not send cleanup requests while disabled. |
| Removal | Stop new work under the removed UUID, cancel queued retries, and drain in-flight work. Perform the bounded cleanup below, then prune its local state. Do not transfer its checkpoints or progress map to a replacement. |
| Re-enable | Resume from retained checkpoints under the current policy; catch up retained backlog and reconcile stale progress bubbles. Telegram inbound events consumed while disabled are an explicit exception, described below. |
| Direction/kind/allowlist change | Apply to newly handled events; intentionally filtered events advance the applicable cursor and are not replayed if policy later changes. |
| Missing connector (404) | Immediately pause all traffic; retain state; refetch on the next `bridge_config` event or stream reconnect. Never fall back to legacy env configuration. (Under the launcher the row's removal also stops the process.) |
| Timeout, 5xx, malformed response, or unsupported schema | The snapshot is now known-stale (an event said it changed, or the stream just reconnected): pause new traffic and retry the fetch with capped backoff (2, 4, … 60 s) until a valid response arrives. At startup, no valid snapshot means no traffic. Log the condition without credentials. |
| Restart-required identity mismatch | Pause and report it; do not reuse another connector's state. |

Freshness is a fact, not a clock. A snapshot is fresh while both hold: it
was fetched on the SSE connection that is currently open, and no
`bridge_config` event for this connector has arrived since. A dropped stream
makes it stale at once — the core's `: keepalive` every 15 s and the bridge's
existing 90 s read timeout are what detect a dead connection, and they exist
for the chat events anyway — and the bridge pauses outbound delivery and
inbound posting until it reconnects and refetches. Workers read that state
themselves before every request, so a wedged fetch cannot leave traffic
enabled on stale policy. Never restore freshness from a timestamp in the
state file. This bounds stale allowlist/enablement use to the interval
between a change and the event's delivery, which is the NOTIFY round trip;
it does not promise revocation while the stream is down, because nothing is
delivered then. Readiness/status must distinguish disabled, stale
configuration, invalid configuration, and transport failure. SSE reconnect
still triggers catch-up from persisted cursors.

Delete-and-create may happen between two config fetches: the bridge can observe
the old and replacement UUIDs in consecutive snapshots without ever seeing an
empty address. It must retire the old worker before activating a replacement
for that address. Queued events and retries retain the UUID they were assigned
under; never re-route old work to the replacement by looking up its address.
The config DELETE response confirms the database change, not that the bridge
has stopped. Already submitted requests can still complete. For a cutover that
requires the process to be stopped, stop that connector before changing its
bindings.

### Cleanup when a binding is removed

Removal permits a retired worker to DELETE only the remote progress messages
whose IDs it recorded, using its original address and authenticated connector.
It may not send or edit messages, inspect the replacement's progress map, or
reset shared transport state. This is the sole exception to requiring a binding
to exist and be enabled before making a remote request.

Start cleanup only after a fresh, valid snapshot for the same connector omits
the old UUID and all its in-flight work has drained. Before each deletion,
require that connector still be enabled and the snapshot still be fresh.
A connector 404, invalid response, or identity mismatch never authorizes cleanup.
Disabling a connector stops cleanup too; removing a binding under a disabled
folder may clean up its recorded bubbles because removal is an explicit action.

Cleanup has a 30-second total deadline, with at most 5 seconds per request
(or the remaining budget). Attempt each recorded message once: 2xx and 404
complete it; other failures, including 429, are recorded without automatic
retry. The adapter must bypass its normal retry/sleep behavior for this path.
Abort on deadline, connector disablement, or lost freshness; report unattempted
and failed messages with their original destination and remote IDs, then prune
the retired state. Do not block config refresh or unrelated bindings while
cleaning up. A replacement waits for the old worker to drain and this bounded
cleanup phase to finish; cleanup's deadline does not authorize pruning state
still owned by an in-flight writer.

If the process stops during retirement, its persisted progress map may be
incomplete: a crash after a remote send but before saving its returned ID can
leave an untracked bubble. On restart, state for a UUID absent from a valid
config is reported for manual cleanup and then pruned; this version does not
resume remote cleanup for orphaned state. Preserve the original address in the
state file for that report. Merely having no valid config, or receiving a
connector 404, must not classify every local binding as orphaned or prune it.

## Delivery state and ownership

Keep a versioned state file per connector, with one owning process. Acquire an
exclusive OS lock on a stable sibling `<state-file>.lock` before reading state
and hold it for the process lifetime. Do not lock the JSON file itself: atomic
replacement changes that file's identity. Never replace or unlink the lock file
while running; a stale file without a held OS lock is harmless after a crash.
Workers serialize state mutation and atomic replacement; two processes must not
write the same file. Deployment must also prevent duplicate processes for the
same connector/bot using different state paths: a file lock
alone cannot enforce that across hosts.

State has **connector-wide** and **binding-specific** parts:

```json
{
  "schema_version": 1,
  "connector_uuid": "<connector-uuid>",
  "platform": "telegram",
  "remote_identity": "<authenticated-bot-id>",
  "transport": {"telegram_offset": 123},
  "bindings": {
    "<binding-uuid>": {
      "room_uuid": "<room-uuid>",
      "address": {"chat_id": "-1001234567890"},
      "address_key": "<canonical-address>",
      "room_cursor": 42,
      "progress_messages": {}
    }
  }
}
```

- Discord's `discord_after` is per binding/channel. Room cursors and progress
  maps are always per binding; bindings sharing a room still have independent
  outbound delivery positions.
- Telegram's `getUpdates` offset belongs to the **bot**, not a chat or binding.
  Use one inbound poller to route updates by explicit chat address. Advance the
  shared checkpoint only after the matched active binding's post succeeds, or
  an update is deliberately filtered/unmapped. A failed post blocks advancement
  past that update; head-of-line blocking is accepted initially. Telegram
  confirms earlier updates when the client requests a higher offset, so persist
  the handled checkpoint before making that next request. See the [Telegram
  Bot API](https://core.telegram.org/bots/api#getupdates).
- Disabled Telegram bindings cannot retain their events in that shared stream
  while other bindings continue. Treat their updates as deliberately filtered
  and advance the shared offset; those inbound messages are not replayed on
  re-enable. The UI must state this when disabling a Telegram binding or folder.
  Outbound room cursors remain paused and catch up on re-enable. When the whole
  connector is disabled, stop remote polling; recovery then depends on what
  Telegram still retains. A durable per-binding inbox is deferred rather than
  implying that a shared cursor can provide independent inbound pause queues.
- Zulip queue IDs and event cursors belong to the connector's event
  subscription. Expired queues require re-registration and an adapter-specific
  recovery/catch-up strategy. Its [queue registration
  API](https://docs.zulip.com/api/register-queue) returns both a queue ID and an
  event position; recovery needs more than renaming `discord_after`.

First activation must establish a per-binding remote high-water mark before
accepting inbound events, even when the adapter uses a shared transport cursor;
an existing connector's backlog must not become a new binding's history replay.
The adapter must define this boundary before its multi-binding mode ships.

State is operationally important even though it is outside Postgres backups.
Missing state initializes at newest and skips backlog; corrupt, incompatible,
or identity-mismatched state stops activation with a recovery instruction.
Database restore does not restore delivery positions. Preserve matching state
when migrating; make any reset explicit. Deleted-binding state can be pruned
after workers have quiesced, never while a worker still owns it.

Inbound remains at-least-once: a crash after the core accepts a message but
before the checkpoint is saved may duplicate it. Remote sends have the same
uncertain-success window; neither exactly-once delivery nor unbounded recovery
beyond platform retention is promised. Local state avoids putting frequent
checkpoint writes into configuration tables, but is not disposable cache.

## Migration and implementation boundaries

Legacy env mode and DB mode are **exclusive**; this is not per-field
`DB -> env -> default` resolution. Once `BRIDGE_CONNECTOR` is set, missing or
invalid DB configuration must never reactivate the legacy channel/allowlist.
Unset DB policy fields inherit through the registry and folder chain only.

1. Add tables and transactional validation, policy resolution, the versioned
   endpoint, and `/bridges`. Define/import the models into the core's SQLAlchemy
   metadata before `init_db` calls `create_all()` in `source/db/__init__.py`.
   That creates missing tables with their declared indexes and constraints;
   this initial addition needs no separate table-creation migration. It does
   not upgrade existing tables. Later changes need guarded migration code for
   columns (`_add_column_if_missing`), indexes, constraints, or data backfills
   as applicable. Existing bridges continue unchanged until explicitly opted in.
2. Add Discord DB mode with the event-driven config fetch and shared snapshots.
   With `BRIDGE_CONNECTOR` unset, preserve all existing `DISCORD_*` behavior.
   In DB mode, use room UUIDs rather than recurring name lookups; replace
   channel, room-name, allowlist, and poll settings with resolved DB values.
3. Provide an explicit one-time import: resolve the legacy room name uniquely,
   create a disabled connector/binding, and copy nonsecret settings. Store the
   token variable's name only. After stopping the legacy process, back up its
   state and wrap matching cursors/progress maps under the new identities.
   Verify the remote address and room before reuse, then start DB mode and
   enable it. Keep the original state for rollback; never run both modes for
   the same bot simultaneously. A rollback after new deliveries needs current
   compatible checkpoints or an explicit reset, not blind reuse of an old file.
4. Migrate Telegram separately, preserving env mode until then. DB mode requires
   an explicit `chat_id`; import a learned `operator_chat_id` only after the
   operator verifies the destination. Do not keep “last allowed sender wins”
   as a DB-mode routing rule. Implement shared-offset routing and make the
   disabled-binding discard behavior visible before enabling multiple bindings.
5. Add Zulip only after defining topic validation, queue recovery, initial
   high-water marks, and progress edit/delete behavior in its adapter. Reuse
   policy resolution and core row handling where their semantics match; do not
   promise that transport or reconciliation code carries over unchanged.

History replay, channel-wide Zulip subscriptions, Telegram forum topics,
operator-editable platform defaults, cross-connector folders, multi-host
failover, durable inbound inboxes, exactly-once delivery, and inbound attachments
are outside this first implementation. `replay_history` is deliberately absent
until its direction, bounds, deduplication, and activation semantics have a
separate design.

## Acceptance checks for implementation

- Resolver distinguishes absent/null from `[]`/`false`, replaces lists, rejects
  invalid types/cycles/references, and prevents a child enabling a disabled tree.
- A rename preserves UUID selection and state; concurrent duplicate-address
  creates cannot both commit; destination edits cannot reuse old checkpoints.
- Delete-and-create between config fetches retires old work before activating
  the replacement, never re-routes an old retry, removes the old binding's
  mirrored progress bubbles best-effort, and preserves Telegram's shared
  offset and other bindings' state. Pruning waits for in-flight state writers.
- Cleanup never edits/sends or touches replacement messages; 404 completes it,
  429 does not trigger a hidden retry, and its deadline bounds the cleanup phase.
  Disablement/staleness stops further deletes. After restart, orphaned state is
  reported and pruned only following a valid connector snapshot, not a 404.
- Room/folder delete previews include disabled bindings. A binding added after
  preview causes DELETE to return 409 with blockers and removes no rooms or
  folders. Concurrent subtree moves and binding creation cannot leave dangling
  references; bypassing the preview cannot bypass the deletion guard.
  Exercise both mutation orders with two database sessions: room deletion
  loses if the binding commits first; binding insertion loses if the room was
  deleted first. A direct `db.session.delete(room)` with a binding present
  raises. Two simultaneous tree PUTs with one token cannot both commit; the
  second checks the token after acquiring the connector lock and returns 409.
  Concurrent folder deletion and binding moves leave no dangling placement. A concurrent folder policy edit during config
  serialization yields one complete snapshot, never a mixture of the old
  policy and new membership.
- Manual and supervised commands on the same host use the same chosen absolute
  state path for one connector and distinct paths for different connectors.
  Importing the bridge loads no secrets; launcher/file precedence and rotation
  match the launcher contract. Ownership changes never start two processes.
- Allowlist removal, direction changes, and disablement reach both workers
  during idle SSE and platform backoff, via a `bridge_config` event and never
  a timer; an idle bridge makes no config request at all. A dropped stream
  pauses traffic before the next request, including between message chunks
  and during adapter retries; a change event during an in-flight fetch yields
  exactly one more fetch. Every connector, folder, and binding write emits
  the event in its own transaction. 404 and schema errors never enable legacy
  fallback.
- Two bindings sharing a room have independent outbound cursors. Telegram
  interleaved chats share one offset; a failed post is retried without skipping,
  deliberately filtered/disabled-chat updates advance the offset, and restarting
  does not route one chat's messages to another binding.
- New bindings skip history; re-enabled bindings catch up retained backlog,
  with the explicit Telegram inbound-discard exception; progress maps reconcile
  on reconnect. Lost, corrupt, and mismatched state have the distinct behaviors
  specified above.
- A second owner cannot acquire the state lock after repeated JSON replacements;
  after the first process exits, a new owner can acquire the surviving lock file.
  Crash injection around send/post, state persistence, and transport
  acknowledgement demonstrates the
  documented duplicate window without silent checkpoint advancement.
- Config responses and errors contain no credential values. Secret-variable
  rotation restarts only its bridge; identity mismatch prevents state reuse.
- Existing isolated Discord and Telegram test suites still pass in env mode;
  migration preserves their matching checkpoints. Core tests cover validation,
  serialization and tree edits independently. Initialization checks cover a
  fresh database, an existing database without bridge tables, and repeated
  initialization with saved bridge rows; required constraints/indexes exist
  and existing data survives.
- Under the launcher: desired entries include disabled/manual connectors, use
  stable `bridge:<uuid>` keys and catalogue kinds, and never contain credential
  values. Child environments contain the launcher's baseline, `RAINBOX_URL`,
  `BRIDGE_CONNECTOR`, the validated absolute state path, and the selected token.
  Gate off/on between polls changes the persisted nonce; editing a binding or
  policy does not. Connector disable stops the owned process, binding disable
  does not, and neither triggers removal cleanup. Core downtime leaves the
  process alive but pauses forwarding when bridge config expires. HTTP 404
  remains recoverable; rejected credentials exit 2 and lock contention exits 3.
