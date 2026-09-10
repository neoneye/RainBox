# Where chat-bridge settings live

**Date:** 2026-09-09

**Status:** design note; none of the proposed tables, endpoints, or live reload
behavior is implemented yet.

**Applies to:** `source/discord_service/`, `source/telegram_service/`, and
future bridges for Zulip, Signal, Slack.

## Decision and current behavior

Keep deployment configuration and credentials in the bridge process's
environment. Store operator-editable connectors, bindings, and policies in
Postgres, exposed to bridges through the core's HTTP API. Keep delivery
checkpoints in each bridge's local state file.

The core (`source/main.py`) should not need a restart for ordinary bridge
configuration changes: agents may be mid-turn. Each connector runs in its own
bridge process, which can be restarted independently. By default a small
top-level launcher (`source/launcher.py`) starts the core and every enabled
service, and stops a service when it is disabled (see *Supervised
services*); running a bridge by hand stays possible for a bridge on another
host. Installing the initial
schema and application code still requires the normal deployment procedure;
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
| `bridge_connector` | `name` (unique label), `platform`, nullable `base_url` and `identity`, `token_env`, `enabled` (default false), `policy` (JSON object, default `{}`) |
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
  The handlers catch the integrity error and answer 409 with current blockers;
  the preview query is a courtesy, not the enforcement.
  Reject deletion of nonempty bridge folders/connectors; the UI can offer an
  explicit transactional removal of their contents. A move or deletion
  validates and commits the whole change together. Do not silently repair a
  missing reference by routing to another room.

### Referential integrity and concurrency

The repository uses two mechanisms for this, and the bridge tables reuse both
rather than adding a lock:

- **Real foreign keys where a delete must be refused or cascaded.**
  `model_config` is protected with `ForeignKey(..., ondelete="RESTRICT")`;
  `chatroom_member` and `chat_message` cascade from `chatroom`. Bridge rows
  follow that split: `bridge_binding.room_uuid` and
  `bridge_binding.connector_uuid` are `RESTRICT` foreign keys, and
  `bridge_folder.connector_uuid` likewise, so a bound room, a nonempty
  connector, or a connector with folders cannot be deleted by any path — UI,
  admin, import, or a raw session — and the refusal is race-free because
  Postgres checks it at commit. Handlers translate the integrity error into
  HTTP 409 with blockers.
- **Plain uuid columns with a version token for placement.** `folder_uuid`
  and `parent_uuid` stay plain columns validated app-side, the cron/chat
  house style, and the `/bridges` tree uses the six-endpoint shape in
  `source/notes/ui-tree-persistence.md`: the page hydrates with an opaque
  version token and echoes it on PUT; a stale token is a 409 and the client
  re-hydrates. That is how `/chat` and `/cron` already serialize concurrent
  tree edits, and it is enough here — these edits are rare and human-paced.

What that does not cover is the same thing it does not cover for chat today:
an *unbound* room moved into a folder while that folder's recursive delete is
committing. That is existing behavior of the chat tree, not a bridge concern,
and a bound room is protected regardless.

Config reads and delete previews assemble a coherent response from several
tables. Run them in a read-only REPEATABLE READ transaction so a folder policy
edit committing mid-read cannot yield a snapshot mixing old policy with new
membership: set it on the request's session before the first query
(`db.session.connection(execution_options={"isolation_level": "REPEATABLE
READ"})`), resolve and serialize inside it, and let it close before the HTTP
response is returned. Never hold a database transaction across a bridge
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
With autostart on (the default) the launcher issues this launch itself; the
copyable command below is for the manual mode, a bridge host other than the
core's, or debugging one connector in a terminal while autostart is off.
The `/bridges` tree gives each connector the same **Copy ID** kebab item the
chat and cron trees have. The detail panel also offers a copyable launch command
for the connector's platform, labeled with its required working directory
(`source/discord_service/` or `source/telegram_service/` on the bridge host).
Include `BRIDGE_CONNECTOR` and a distinct state-file path derived from its UUID,
using the platform's existing state-file variable. Two copied commands must not
both use the default `./state.json`. Interpreter, script, and state paths are
relative to that displayed directory; do not infer bridge-host paths from the
core's checkout. Show `RAINBOX_URL` as a separate deployment prerequisite when
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

The bridges do **not** load the repo-root `.env`, and this design does not
add that: under the launcher the credential arrives in the environment, and
in manual mode the operator sets it in the launch shell. `source/env_file.py`
is invoked from `providers/__init__.py`, which these isolated services never
import. For the record, should a bridge-side loader ever be wanted: `env_file.py`
resolves the repo root from its own
location and has one third-party dependency, `python-dotenv`, imported
lazily inside `load_env_file()`. A bridge can import it after adding the absolute
source directory, `str(Path(__file__).resolve().parents[1])`, to its import path;
do not use `".."`, which resolves against the launcher's working directory. After
pinning `python-dotenv` in its own `requirements.txt` beside `requests`,
the bridge can call `load_env_file()` without importing the LLM/provider stack
or using the core venv. Call it explicitly at startup before reading config,
not when importing the bridge module for tests. Launcher environment variables
win over file values; the loader already guarantees that
(`load_dotenv(..., override=False)`). The helper loads all keys in the shared
file, not just this connector's credential. Keep launcher-supplied
credentials as the default when each process should receive only its own
secret; reusing the shared loader does not provide that isolation. Under
the launcher that isolation comes for free: it copies exactly one variable,
the one `token_env` names, into the child's environment (see *Supervised
services*), so the shared `.env` is read by the launcher alone and a bridge
process never sees another bot's token.

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
both, as siblings. Everything between them is plain HTTP over the
loopback TCP socket on `127.0.0.1`, in two forms:

- **Request and response, JSON.** Find the room, post an inbound message,
  fetch a row by id, and fetch the resolved config snapshot from
  `GET /bridge/api/connectors/<uuid>/config`.
- **One long-lived streaming response, Server-Sent Events.** The bridge holds
  `GET /chat/stream` open and reads `data:` lines as the core emits them —
  the same stream the browser uses. Behind it the core listens on a
  Postgres NOTIFY channel and forwards each payload.

No pipe, no RPC framework, no message broker, no shared files, no database
connection: the bridge's state file is private to the bridge, and the bridge
never holds `DATABASE_URL`. The core exposes nothing to a bridge that a
browser on the same machine could not already reach, so a bridge inherits the
existing localhost trust boundary instead of adding a channel.

This is deliberately *not* the mechanism the supervisor uses for its agents.
`main.py` spawns each agent as `python -m agents --socket-fd N` over a
`socketpair()`, writes the agent's config down that socket, and reads
heartbeats and status back up it. Agents need config injected because they
have no other way to receive it; a bridge fetches its config over HTTP and
reports liveness through its logs, so the supervisor's only levers on a
bridge are the ones it has on any child: its exit status, and signals.

## Supervised services

A small top-level **launcher** (`source/launcher.py`) is the root of the
process tree. Its only job is to start processes, keep the ones that should
be running running, stop the ones that should not, and report what it sees.
It starts the core (`main.py`) the same way it starts a bridge, so the core
is a *sibling* of the services, not their parent: restarting or losing the
core does not take a bridge down, and the launcher restarts the core like
anything else.

```text
launcher.py            (stdlib only, single-threaded, ~15 MB)
├── main.py            (core: supervisor thread + webserver)
│   ├── python -m agents …
│   └── python -m agents …
├── discord_service/venv/bin/python bridge.py
├── telegram_service/venv/bin/python bridge.py
└── voice_tts_kokoro/venv/bin/python server.py
```

That tree is what Activity Monitor's hierarchical view and `ps -o
pid,ppid,command` show, because each child's parent is the launcher.

### Why the launcher is small, and why it must exec

The launcher imports nothing from the application — no Flask, no
SQLAlchemy, no `db` — only the standard library. That, not the spawn call,
is what keeps its footprint minimal. It creates children with
`subprocess.Popen(argv, cwd=…, env=…)`, which forks and immediately execs
the service's own interpreter (CPython picks `vfork`/`posix_spawn` or
`fork`+`exec` depending on the arguments; the parent pid is the launcher
in every case). Two things it deliberately does not do:

- **Fork without exec.** A forked child shares the parent's pages
  copy-on-write, so forking a process with the application loaded yields a
  child as large as the parent until it execs; and forking a multithreaded
  Python process on macOS without an immediate exec is unsafe (only the
  calling thread survives, locks held by other threads stay held), which is
  why Python's own `multiprocessing` defaults to spawn on macOS. The
  launcher stays single-threaded — a `select`/`poll` loop — and every child
  is a fresh interpreter.
- **Serve HTTP.** The launcher has no server. It *pulls* what to run from
  the core and *pushes* what it sees, both as an HTTP client over
  `127.0.0.1`, the same transport everything else here uses.

`main.py`'s own supervisor is unchanged: it still spawns and watches agents
over their socketpairs. Agents remain children of the core because the core
is what hands them work.

### Bootstrap and the control loop

1. The launcher is started with the root venv's interpreter
   (`venv/bin/python launcher.py` from `source/`): it has no requirements
   of its own, and `sys.executable` is what it runs the core with, so the
   core gets the venv the operator chose for the launcher. It reads `.env`
   with its **own parser**, not `python-dotenv` — the launcher is stdlib-only
   and a test asserts that. The parser handles the subset the repo's
   `.env.example` uses (`KEY=value`, single/double quotes, `#` comments,
   blank lines); a test feeds `.env.example` and a fixture of edge cases
   to both parsers and asserts identical results, so the launcher and the
   core can never disagree about a value. Then it starts the core
   unconditionally.
2. It polls `GET /services/api/desired` on the core until the core answers,
   then every 5 seconds. The response lists every service that should be
   running: `{key, kind, dir, argv, env: {name: value}, token_env,
   restart_nonce}`. The core builds it from the service registry and the
   `bridge_connector` rows; the launcher has no idea what a connector is.
3. It reconciles: desired and not running → spawn; running and not desired
   → `SIGTERM`, `SIGKILL` after 10 s; `restart_nonce` changed → stop, then
   spawn (that is what *Restart* on `/bridges` and `/settings` does);
   exited unexpectedly → respawn with exponential backoff (2 s, 4 s, …
   capped at 60 s); five crashes inside two minutes → **failed**, no
   respawn until the operator toggles it off and on or presses *Restart*.
4. After every change it `POST`s `/services/api/status` with
   `{key: {state, pid, since, last_exit, message}}`; it also re-posts the
   full table every 30 s as a heartbeat, so a core that restarted learns the
   current picture without waiting for a change.
5. Its own `SIGINT`/`SIGTERM`: `SIGTERM` every service first and the core
   last, so a bridge unwinding an in-flight post or state write still has a
   live core to finish against; wait 10 s, `SIGKILL` the rest, exit. The
   core's own shutdown then kills its agents as it does today.

Because the core is a child, a core restart is just step 3 for the entry
`core`; bridges keep running, notice the SSE stream drop, back off, and
reconnect with catch-up, exactly the path they already take when the core
goes away.

### The service registry

A code-side registry in the core, the same shape as `SETTINGS` in
`source/db/settings.py`, lists every service the launcher may run: a stable
key, the working directory, the argv relative to it, the interpreter (the
service's *own* venv: `<dir>/venv/bin/python`), an optional port to report,
and the environment it needs. Two kinds of entry:

- **Static services** (`voice_tts_kokoro`, `voice_stt_whisper`,
  `voice_tts_dotstts`, `reranker`): one fixed entry each, switched by a
  registry setting `services.<key>.enabled` (bool, default `false`) that the
  `/settings` page renders as a toggle like any other bool. Their discovery
  URLs (`KOKORO_TTS_URL` and friends) are unchanged; the launcher simply
  runs the process that answers there.
- **Bridge connectors**: one entry per `bridge_connector` row, switched by
  the row's own `enabled` flag on `/bridges`, gated by one registry setting
  `services.bridges.autostart` (bool, default `true`) for the operator who
  runs bridges elsewhere.

The default for static services is off and for bridge autostart is on: a
bridge's whole existence is a DB row the operator created deliberately,
while the voice services are optional heavyweights whose venvs may not even
be installed. A toggle takes effect on the launcher's next poll, and never
restarts the core.

### What the child receives

Unlike an agent, which inherits `dict(os.environ)` from the core, a service
gets a **minimal environment built from scratch**: `PATH`, `HOME`,
`LANG`/`LC_*`, `TMPDIR`, then only what the desired-state entry declares.
For a bridge that is:

| Variable | Value |
|---|---|
| `RAINBOX_URL` | `http://127.0.0.1:5000` |
| `BRIDGE_CONNECTOR` | the connector uuid |
| `<PLATFORM>_STATE_FILE` | `<state dir>/bridge-<connector-uuid>.json`, so two connectors can never share a file; the core sends the file *name*, the launcher owns the directory (`--state-dir`, default `<repo>/var/services/`, gitignored) and prefixes it, since where a host keeps runtime state is the launcher's fact, not the database's |
| the variable `token_env` names | the credential value, and nothing else from the launcher's environment |

The desired-state response carries the *name* in `token_env`, never the
value; the core does not have it and must not. The launcher resolves the
name against `.env`, **re-reading the file at each spawn** and preferring
the file over its own environment, so a rotated token reaches a service on
*Restart* without restarting anything else. A name present in neither place
is reported as **credential missing**, by variable name, and not spawned.
A missing venv is **not installed**, and not spawned. Neither counts as a
crash. Stdout and stderr are inherited, so every service's log lines land in
the launcher's terminal, prefixed by the service itself as today — which
also means the launcher never *reads* them. The only thing it learns from a
child is its exit status, so services use a small exit-code convention and
the launcher maps codes to states instead of parsing logs:

| Exit code | Meaning | Launcher reaction |
|---|---|---|
| `0` | clean exit after `SIGTERM` | `stopped` |
| `2` | configuration or credential rejected (bad connector, token refused by the platform, room missing) | `failed`, no respawn; the bridge's own log line has the detail |
| `3` | state-file lock held by another process | `failed`, message "state file locked by another process; see its log for the pid"; no respawn |
| anything else, or a signal | crash | respawn with backoff; five in two minutes → `failed` |

The two deterministic failures get no backoff loop because respawning
cannot fix them; the bridges already exit through `SystemExit` for exactly
these conditions and only need the codes assigned.

### Status

The core keeps the last table the launcher posted, with the time it
arrived, and the `/bridges` and `/settings` handlers read it. States:
`running`, `stopped`, `starting`, `stopping`, `failed`, `credential
missing`, `not installed`, and `unknown` — the last is what the core shows
for every service once no status has arrived for 90 s (no launcher, or a
launcher that died), and what a webapp served by `tools.serve_ui` shows
always. `/bridges` shows the state beside each connector with a *Restart*
action; `/settings` shows it beside each static toggle; both show the
`core` entry's own state so the page can say when it is running unlaunched.

### Telling the processes apart

Every rainbox process is a Python interpreter, and Activity Monitor names a
process after its executable, so without help they all read "Python". The
hierarchy is the first fix: under the launcher, a process at 100 % CPU is
one hop from a named parent, and `ps -o pid,ppid,%cpu,command` shows each
child's full argv (`bridge.py`, `-m agents`, `server.py`). A second fix is
worth an experiment before relying on it: exec each service through a
symlink named for it (`bin/rainbox-discord -> discord_service/venv/bin/python`),
so `argv[0]` — and, if Activity Monitor uses the exec path rather than the
resolved binary, its Process Name column — reads `rainbox-discord`. Whether
Activity Monitor honors the symlink name is not something to assert from
memory; run it once and keep it only if it shows.

### Manual mode and duplicates

With `services.bridges.autostart` off, the launcher runs the core and the
static services only, and the copyable launch line is the way in for
bridges. With it on, a bridge also started by hand on the same host
collides on the state-file lock and exits with code 3; the launcher then
reports **failed** with the lock message above, which is the signal that two
launchers are configured (the pid is in the bridge's own log line, which the
launcher does not read). Across hosts the lock cannot help, as
the state section already says.

Disabling a connector has two effects, and both are wanted: the launcher
stops the process, and — in manual mode, where the launcher is not running
it — the bridge's own config refresh sees `enabled: false` and pauses, as
the live-configuration contract describes. Neither replaces the other.

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

A dedicated config refresh task runs every 5 seconds, including while all
bindings are disabled. It is independent of platform polling, SSE traffic,
long polling, and retry backoff. Bound each config request to 5 seconds.
Allow only one config request in flight. Validate the entire response before
atomically publishing an immutable snapshot to **both** workers. Workers check
its freshness, current binding, enabled gate, and policy before each delivery
request (send/post/edit/delete), including each chunk of a long message and
each retry inside the adapter. Connector-wide inbound polling checks freshness
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
| Missing connector (404) | Immediately pause all traffic; retain state and keep refreshing. Never fall back to legacy env configuration. |
| Timeout, 5xx, malformed response, or unsupported schema | Keep the last valid snapshot for at most 30 seconds since its last successful validation, then pause new traffic until a valid response arrives. At startup, no valid snapshot means no traffic. Log the condition without credentials. |
| Restart-required identity mismatch | Pause and report it; do not reuse another connector's state. |

Measure the 30-second freshness limit with a monotonic clock, resetting it only
after a successful fetch and validation, even if the revision is unchanged.
Never restore freshness from a timestamp in the state file. Workers enforce
expiry themselves so a stuck refresh task cannot leave traffic enabled. This
bounds stale allowlist/enablement use during an outage; it does not promise
instantaneous revocation. Readiness/status must distinguish disabled, stale
configuration, invalid configuration, and transport
failure. SSE reconnect still triggers catch-up from persisted cursors.

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
2. Add Discord DB mode with the independent refresh task and shared snapshots.
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
  Exercise both mutation orders with two database sessions; the losing order
  fails on the `RESTRICT` foreign key, not on an application check, and a
  direct `db.session.delete(room)` with a binding present raises. A stale
  tree version on PUT is a 409. A concurrent folder policy edit during config
  serialization yields one complete snapshot, never a mixture of the old
  policy and new membership.
- Copied launch commands use distinct state files and preserve already exported
  credentials. If `.env` loading ships, it resolves the same file from the
  service directory, repo root, and an unrelated working directory; importing
  the bridge alone loads no secrets and launcher values retain precedence.
- Allowlist removal, direction changes, and disablement reach both workers
  during idle SSE and platform backoff; stale config pauses within the stated
  limit, including between message chunks and during adapter retries. A stalled
  refresh task and wall-clock jumps cannot extend freshness; an unchanged valid
  revision renews it. 404 and schema errors never enable legacy fallback.
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
- Supervision: toggling a connector or a static service changes only that
  child within one launcher poll; the core's pid, webserver, and running
  agents are untouched. Every service's parent pid is the launcher's. A
  child's environment contains exactly the declared variables plus the one
  credential `token_env` names, and no other `*_TOKEN`/`*_KEY` from the
  launcher's environment; the desired-state response never contains a
  credential value. A rotated `.env` value reaches the child on *Restart*
  without restarting the core or the launcher. Killing the core leaves
  every bridge running and reconnecting; the launcher restarts the core.
  Missing venv and missing credential are reported states, not crash loops;
  five crashes in two minutes stop respawning and show *failed*. Shutting
  down the launcher terminates every child with the same grace and
  escalation. A core that has heard nothing from a launcher for 90 s
  reports every service as `unknown` rather than guessing. The launcher
  imports only the standard library (a test asserts its module set).
