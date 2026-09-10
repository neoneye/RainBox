# Launcher: one small process that starts the rest

**Date:** 2026-09-10

**Status:** phase 1 implemented (`source/main.py` — the launcher IS the entrypoint; the core moved to `source/core.py`, `source/services/`,
`source/webapp/services_api.py`, the `services.*` settings and the /settings
launcher card). Two runtime facts beyond the text below: `RAINBOX_CORE_PORT`
overrides the core's port for both `core.py` and the launcher, so a second
core can run beside the operator's for a smoke test; and the control channel
between launcher and core is a socketpair (`core.py --control-fd N`), not
HTTP — see *Bootstrap and the control channel*.

**Roadmap:** ship the core and static services first, then add dynamic bridge
entries from the [bridge settings design](2026-09-09-bridge-settings-design.md).
Bridges continue to start manually until that second phase is ready.

## Decision and current behavior

A small `source/main.py` — the launcher, and the way rainbox is started — runs the core (`source/core.py`) and enabled services as siblings.
The core continues to own its agents. Operator settings describe which services
should run; the launcher owns processes, credentials, restart backoff, and local
state paths. Restarting a service never requires restarting the core.

```text
main.py  (launcher)
├── core.py                         core and webserver
│   └── python -m agents …          owned by the core
├── voice_tts_kokoro/venv/bin/python server.py
├── voice_stt_whisper/venv/bin/python server.py
└── reranker/venv/bin/python server.py
```

Today the side services start manually, as documented in
`source/notes/voice-and-services.md`. The launcher does not install dependencies,
load models, or guarantee service readiness. A live PID can still be loading a
model or failing requests. The current core shuts down by force-killing its
remaining agents; `TERM_GRACE` applies to its watchdog, not to a graceful drain
of all agent turns. The launcher must not promise to preserve turns when the
operator restarts the core.

## Runtime and launch command

```bash
cd source
venv/bin/python main.py [--state-dir <dir>] [--core-only]
```

- Resolve the source directory from `__file__`, not the working directory.
  Run the core with `sys.executable` and absolute `core.py`, with `cwd=source/`.
- Use the standard library plus a shared, data-only service catalogue. No Flask,
  SQLAlchemy, model stack, or `requests` imports. Measure memory during validation;
  an approximately 15 MB footprint is a target, not a platform-independent fact.
- `--state-dir` defaults to `<repo>/var/services/`; create it with owner-only
  permissions and add `var/services/` to `.gitignore` during implementation.
  Canonicalize it once. Acquire an OS lock on a stable `launcher.lock` before
  any spawn and hold it for the launcher's lifetime. Never unlink the lock file
  to release it. Exit 3 if it is held.
- `--core-only` suppresses every service entry regardless of its DB toggle.
  The status page must show this override rather than suggest a toggle is broken.
- Spawn with `Popen(argv, cwd=…, env=…, start_new_session=True, shell=False,
  stdin=DEVNULL, stdout=PIPE, stderr=STDOUT, close_fds=True)` (plus
  `pass_fds` for the core's control fd). Use absolute executable/script paths,
  resolved from the local catalogue. Preserve a venv interpreter's invocation
  path even if it is itself a symlink; resolving it to the base Python can
  lose the venv. Do not use `preexec_fn` or fork without exec.
- **Child output is piped, not inherited.** Each child's stdout and stderr
  are one non-blocking pipe the loop selects on; every line is printed to
  the launcher's stdout prefixed `[<key>] `, so a traceback from Kokoro and
  one from Whisper can never interleave anonymously. A pipe stays open until
  EOF, so a grandchild that inherited it is attributed to its parent's key.
  Idle cost is nil: the loop wakes only when a child writes.
- **POSIX only.** `select()` on a pipe, `SIGCHLD`, process groups, `fcntl`
  locks — none of it exists on Windows, and neither does the core's
  `posix_spawn`/socketpair agent protocol. rainbox is a macOS/Linux program;
  the launcher says so at import and refuses to run elsewhere rather than
  failing obscurely in the loop.

Each service gets its own process group/session. Terminal Ctrl-C reaches the
launcher, which performs the shutdown sequence below. Exact fork/spawn selection
is Python's implementation detail; the relevant contract is documented by
[Python's subprocess API](https://docs.python.org/3/library/subprocess.html).

The single-threaded loop sleeps in `select()` on the control socket to the
core, the signal wakeup pipe, and every child's output pipe — with a timeout
equal to its earliest own deadline (a stop escalation, a backoff retry, a
group drain, a shutdown phase, the next inactivity log line). It wakes for a
line from the core, for a child writing output, for `SIGCHLD` (a child
exited: reap it now), for `SIGTERM`/`SIGINT`, or for that deadline, and for
nothing else. There is no timer tick and no HTTP: the launcher is not an HTTP
client at all. Sends to the core never block: a status line the socket cannot
take yet stays pending, the socket joins the write set, and a newer table
replaces a pending one whose bytes have not yet gone out. Backpressure never
closes the channel (an inherited socketpair cannot be reopened); only a peer
that is gone does.

## Environment and credential ownership

The launcher does **not** parse repo-root `.env`. The core already loads that
file through `source/env_file.py` and keeps its existing environment precedence.
This avoids maintaining a second, subtly different dotenv parser.

Core environment: inherit the launcher's startup environment, plus the instance
marker described below. This is not secret isolation from the core: anything
exported to the launcher can reach the core and its agents, and provider loading
reads the whole repo-root `.env`. Do not put bridge-only credentials there if
they must stay out of the core.

Service environment: build from an explicit baseline (`PATH`, `HOME`, `LANG`,
`LC_*`, `TMPDIR` when present), catalogue-approved nonsecret values, and, for a
bridge, exactly its named credential. Never forward `PYTHONPATH`, `PYTHONHOME`,
loader-injection variables, database URLs, or arbitrary parent variables through
this service path. Additional model-cache/proxy settings require catalogue
entries; the UI must not accept arbitrary environment maps.

For supervised bridges, use `<state-dir>/credentials.env`, an owner-readable
file read only by the launcher. It is optional for the static-services phase;
a missing file matters only when a requested credential is unavailable.
The launcher parses a deliberately limited format:

- One `NAME=value` per line; names match `[A-Za-z_][A-Za-z0-9_]*`.
- Blank lines and lines whose first non-whitespace character is `#` are ignored.
- Strip whitespace around the name and value; one matching pair of single or
  double quotes may surround the value. Characters inside quotes are literal.
- No interpolation, escapes, multiline values, `export`, or inline comments.
  An unquoted `#` is part of the value. Reject malformed quoting and duplicate
  names with a line-number error, never echo the line's contents.

At **every spawn**, resolve a credential from the freshly parsed credentials
file first, and only if the file does not set that name, from the launcher's
startup environment. The file is the operator's source of truth for service
credentials, so editing it and pressing Restart takes effect even when the
same name was exported when the launcher booted — silently ignoring a file
edit because of a variable set days ago would be the astonishing behavior.
An empty value is unset at either level. Show the selected source
(file/environment) without its value. Changing an exported value that the
file does not override requires restarting the launcher. Running children keep their
current environment. Invalid file syntax prevents file-backed spawns, not the
core or already-running services. Neither source is copied into DB, argv, status,
or the desired-state API. A child may still deliberately read local files:
environment filtering is not an OS sandbox.

## Bootstrap and the control channel

The core is always desired and is not part of the service list. Before
spawning it the launcher creates a `socketpair()`, marks the child end
inheritable, and passes it as `core.py --control-fd N` — the same mechanism
the core uses to hand each agent its socket. The launcher keeps the other end.
Both directions carry JSON lines and both are **pushes**:

- core → launcher: `{"type": "desired", …}` once when the core has finished
  `init_db` and again whenever a service setting or restart nonce changes;
- launcher → core: `{"type": "status", …}` whenever a process changes state.

The socket is identity and liveness at once: only the child that inherited
this fd can speak on it, so there are no instance markers to check, and EOF on
either end means the peer is gone. When the core dies the launcher sees EOF
(and `SIGCHLD`); when the launcher dies the core's reader sees EOF and the core
is unmanaged from that moment. A core started without `--control-fd` (by hand,
`tools.serve_ui`) is unmanaged and `/settings` says so.

There is no port probe before spawning the core: any check the launcher
made would be a time-of-check/time-of-use race against the core's own bind a
moment later, and a false refusal is worse than a clean failure (an earlier
connect-based probe refused to start on every Mac with AirPlay Receiver on,
which answers loopback connects on `*:5000`). The core binds first thing in
`main()`, before its supervisor thread or control channel exist, and exits
with code 3 — the "held by another process" convention — when the address is
in use, with a log line naming the port. The launcher reports that as
`failed` without a respawn loop, exactly like a held lock.

Until the first complete valid snapshot from the current core, start no side
services. Malformed lines, unknown schema versions, unknown kinds, duplicate
keys, and oversized lines leave the last valid snapshot intact; they never mean
"disable everything". Keep already-known service desired state during core
downtime, including normal crash recovery for those services. Bridge processes
separately pause message traffic when their own configuration expires; the
launcher keeping a process alive grants no permission to keep forwarding.

## Desired-state contract

One JSON line from the core, pushed at startup and on every change:

```json
{
  "type": "desired",
  "schema_version": 1,
  "core_pid": 123,
  "core_restart_nonce": "<persisted-nonce>",
  "services": [
    {
      "key": "voice_tts_kokoro",
      "kind": "voice_tts_kokoro",
      "enabled": true,
      "restart_nonce": "<persisted-nonce>",
      "env": {}
    }
  ]
}
```

A snapshot is complete and coherent — it comes from one statement over the
settings table — and includes **disabled** entries. Missing static entries
invalidate it. A missing dynamic bridge key in a valid snapshot means removal.
Validate the whole line before reconciling any part of it; unknown kinds
invalidate it and report that launcher/core versions disagree.

The local data-only catalogue fixes each kind's directory, argv, allowed
nonsecret environment keys, and defaults. The core imports the same catalogue
to build its registry and settings. The channel cannot provide arbitrary paths, argv,
or new executable kinds. A service key identifies one process; static keys
match their kind, and future bridge keys are `bridge:<connector-uuid>`.

The later bridge extension permits `token_env` and `state_file: {env, name}`
only for registered bridge kinds. Validate the credential name, the catalogue's
state-variable name, and the UUID-derived basename, then join it under the
canonical state directory. Reject path traversal and collisions with explicit
`env` values. Static services reject these extra fields. The bridge spec defines
the exact entry; both readers use this same envelope and validation rules.

`source/db/settings.py` registers `services.<key>.enabled` (default false) and
internal persisted `services.<key>.restart_nonce` for static services, plus
internal `services.core.restart_nonce`. Restart is a POST action that writes a
new nonce; it never executes a process inside the core. Consume a new nonce in
launcher memory **before** starting its stop/restart sequence. On the first
valid snapshot after launcher startup, adopt the existing core nonce as the
baseline; the just-started core must not restart merely because it has a nonce.
An ordinary core restart preserves the remembered nonces for every service.
Repeated snapshots therefore cannot trigger repeated restarts. Restart does not
implicitly enable a disabled service. Every transition from disabled to enabled
also changes its nonce atomically, so a quick off/on between two snapshots can reset a
failed service. Changes received while stopping coalesce to the newest desired
entry; finish stopping before starting at most one replacement.

A nonsecret launch-environment change is a restart-requiring edit: the core
updates that entry and its nonce in one transaction, and the UI labels it as a
restart. Ordinary bridge policies and bindings stay out of this environment and
do not change the process nonce. New executables/catalogue changes require a
launcher deployment; they are not live settings.

### Initial catalogue

| Kind | Directory under `source/` | Current bind | Supported configuration inputs |
|---|---|---|---|
| `voice_tts_kokoro` | `voice_tts_kokoro` | `127.0.0.1:5005` | None initially |
| `voice_stt_whisper` | `voice_stt_whisper` | `127.0.0.1:5006` | `WHISPER_MODEL`, `WHISPER_COMPUTE_TYPE`, `WHISPER_CPU_THREADS` |
| `voice_tts_dotstts` | `voice_tts_dotstts` | `127.0.0.1:5007` | None initially |
| `reranker` | `reranker` | `127.0.0.1:5008` | `RERANKER_MAX_LENGTH`, `RERANKER_BATCH_SIZE`, `RERANKER_DEVICE` |

All currently run `venv/bin/python server.py`. Ports are hard-coded in their
entrypoints: `KOKORO_PORT` is not an existing setting. Preserve current defaults;
validate any supported override in the registry. Discovery URLs on the core
(`KOKORO_TTS_URL`, `WHISPER_STT_URL`, `DOTS_TTS_URL`) are independent: starting a
local service does not rewrite a core URL that points elsewhere. The UI should
show the local bind address and explain this distinction. Bridges are added in
a later catalogue extension.

## Reconciliation, failures, and shutdown

Each process has one state record, its owned PID/process group, observed nonce,
last exit, crash timestamps, and optional next-retry/stop deadline. A successful
spawn means `running` (PID alive), not “HTTP ready.” Missing interpreter/script
is `not installed`; permission/exec-format errors are `failed` with an actionable
launcher diagnostic. No venv creation or package installation happens here.
Before a restart, validate executable and credential prerequisites; if invalid,
keep a currently running child and report the blocked restart. A disable/removal
still stops it. After a valid replacement is prepared, stop the old child before
spawning; never run two copies to test the replacement.
For a blocked restart, retain state `running` and its PID, put the reason in
`message`, and consume that nonce as attempted. A fresh Restart retries the
prerequisites; repeated snapshots do not keep reading a broken credential file.

| Observation | Action |
|---|---|
| Enabled, stopped, prerequisites available | Spawn once. |
| Disabled, removed, or launcher shutting down | Cancel pending retries and stop the owned process; no replacement. |
| New nonce while enabled | Clear failure/backoff, preflight prerequisites, then stop if running and start with latest configuration and credentials. |
| Exit while an intentional stop is pending | Complete that stop regardless of numeric code; do not count it as a crash. |
| Exit 2 or 3 without a pending stop | `failed`, no automatic respawn. |
| Any other unexpected exit, including 0 or a signal | `backoff`, retry after 2, 4, 8, … seconds up to 60; the fifth unexpected exit in a rolling 120 seconds latches `failed`. |
| Missing credential or installation | Report the blocked state; retry prerequisites on a new nonce or disabled-to-enabled transition. |

Exit 0 is not evidence that SIGTERM caused the exit. A desired daemon that exits
0 spontaneously still needs crash recovery. Reset the backoff exponent after
120 seconds of continuous running; intentional restarts clear the crash window.
Use exit codes 2 (deterministic configuration/credential failure) and 3 (held
ownership lock) only where services explicitly implement that convention.
Existing `SystemExit("message")` exits 1; current services do not uniformly
implement these codes. Converting entrypoints and testing those paths is
implementation work, not an already-provided property.

The **core** uses the same crash budget. If it fails deterministically or
exhausts its budget, keep other services at last-known desired state and log a
prominent local diagnostic; recovery is restarting the launcher after fixing
the cause, since the core's Restart button is unavailable. Do not claim the core
restarts forever while also specifying a finite crash budget.

For ordinary service stops, send SIGTERM to its owned process group, allow
10 seconds, then SIGKILL remaining group members. Reap the direct child; ensure
its old group is gone before replacement, including when the leader crashed
but descendants remain. Signals target only groups created by this live
launcher, never PIDs loaded from stale files. Use [process-group
signals](https://docs.python.org/3/library/os.html#os.killpg) for service trees.
For a core stop, initially signal its PID so its supervisor can handle agents;
if descendants survive or its grace expires, kill the owned core group. Current
agents inherit that group; detached grandchildren are outside this guarantee.

Launcher shutdown is **two phases**, with respawns disabled from the start:

1. Stop all side-service groups together; keep the core alive while they drain.
   At 10 seconds escalate remaining services and reap their direct children.
2. Only after phase 1, signal the core, give it a separate 10-second grace, then
   kill any remaining core group and reap it. Release the lock and exit.

A second termination signal requests immediate escalation. This ordering keeps
the HTTP core available during the service grace; merely sending it SIGTERM a
few milliseconds after the services would not. Abrupt launcher death (SIGKILL,
crash, power loss) cannot run this sequence. Children in separate sessions may
survive; do not adopt or kill them using persisted PID numbers on the next run.
Report port/lock conflicts and require the operator to stop known survivors
before restarting. Automatic orphan adoption is out of scope.

## Status and operator controls

One JSON line from the launcher, pushed after every change and after the first
snapshot from a freshly started core (so a new core learns the whole table at
once). Never on a timer: an idle system sends nothing.

```json
{
  "type": "status",
  "schema_version": 1,
  "sequence": 7,
  "launcher": {"pid": 122, "state_dir": "<absolute-dir>", "started_at": "<UTC>", "core_only": false},
  "services": {
    "voice_tts_kokoro": {
      "state": "running", "pid": 124, "since": "<UTC>",
      "last_exit": null, "message": null
    }
  }
}
```

The table includes `core` and disabled services. The core accepts only
increasing sequence numbers; receipt time is its own. Status stays in memory.
A slow core costs the launcher one buffered line, never the channel: the
line waits until the socket is writable, a newer table replaces a pending
line none of whose bytes have gone out, and a line partly on the wire is
finished before the next. Only `EPIPE`/`ECONNRESET` — the peer is gone —
closes the channel; the next core learns the table after its first snapshot.

States: `starting`, `running`, `stopping`, `stopped`, `backoff`, `failed`,
`credential missing`, `not installed`. Include next-retry time for `backoff` and
credential source for credential-related states, never a credential value.
`/settings` displays the desired toggle separately from observed state, with a
POST Restart action. `running` describes liveness only. When the channel is
closed (EOF, or a failed send) the core is unmanaged and shows every observed
state as `unknown`; it never alters the desired toggle. A core started without
`--control-fd` — by hand, or `tools.serve_ui` — is unmanaged from the start
and displays exactly that.

Child output is read by the launcher and printed with a `[<key>] ` prefix
per line (see *Runtime*); the launcher additionally logs service key, PID,
spawn, exit, and signals itself. Activity Monitor hierarchy and the
key-to-PID status table identify children by process; the prefix identifies
them by log line. Process-name symlinks are an optional later experiment,
not a prerequisite or a reason to risk bypassing a service venv.

A port conflict means an unmanaged instance or another application may be there,
not necessarily a second launcher. Only the process this launcher owns appears
in its status table. A manually started duplicate failing does not change the
healthy supervised child's state; the reverse ownership order may cause the
supervised attempt to fail. State-dir locks cannot prevent duplicate launchers
using different directories from competing for the same fixed core port.

## Idle cost and shortcomings

rainbox is meant to sit idle without waking the CPU or GPU, so the launcher's
idle cost was measured (macOS, Apple Silicon, sandbox database, `--core-only`,
nothing enqueued), sampling `top` per process: context switches over 30 s as
the wakeup count, %CPU averaged over 60 s. Three implementations were
measured on 2026-09-10; the third is the current one.

| Process | Context switches / 30 s | CPU idle |
|---|---|---|
| core alone (`core.py`, unmanaged) | 144–194 (its own 5 s ticks) | 0.09–0.14 % |
| launcher, fixed 0.25 s timer + HTTP polling | 160 | 0.14 CPU-s in 128 s |
| launcher, deadline loop + HTTP polling every 5 s | 36 | 0.11 CPU-s in 136 s |
| **launcher, socket channel (current)** | **8** | **0.07 CPU-s in 153 s** |
| core under HTTP polling | +77 over core alone | 0.25–0.27 % |
| **core under the socket channel** | **167 — inside the core-alone range** | **0.02 %** |

What the numbers say:

- **The first two designs were the kind of regression this project exists to
  avoid.** A quarter-second timer woke the launcher four times a second for
  nothing, and 5-second HTTP polling put seven Flask requests per 30 s on the
  core — a session, a `SELECT`, JSON each — roughly doubling the core's idle
  CPU, paid whether or not any service was enabled.
- **The socket channel removed both.** Nothing polls in either direction: the
  core pushes a snapshot when a setting changes, the launcher pushes status
  when a process changes, `SIGCHLD` delivers child exits, and the loop sleeps
  in `select()` until one of those happens. The launcher's remaining 8
  wakeups per 30 s are the inactivity log and the sampler itself; the core's
  idle is back to what it was without a launcher. A toggle on `/settings`
  now applies in milliseconds instead of within 5 s.
- **Liveness is free.** Two timed signals a heartbeat used to buy — "is the
  launcher alive?", "is the core alive?" — come from EOF on the socket and
  from `SIGCHLD`, at zero idle cost.

The one timed wakeup left is deliberate: the **inactivity log**. After 1
minute with nothing to do the launcher logs `no activity for 1 minutes`, then
again at 10 minutes, then at 60, then every 60 minutes — enough to see in the
log that it is alive, sparse enough never to flood it. Any activity (a line
from the core, a child exit, a signal) resets the ladder to 1 minute.

Shortcomings that remain:

- Every idle wakeup in the core is its own: the 5 s `IDLE_TICK_TIMEOUT`
  Postgres poll and the 5 s cron tick. The launcher neither fixes nor worsens
  them; they are the next thing to look at if idle cost matters further.
- `running` means the process is alive, not that the service answers; a
  service loading a model for a minute is `running` the whole time.
- The launcher does not install venvs or models; `not installed` is a
  report, not an action.
- **A launcher crash mid-transition aborts the transition.** The launcher
  is stateless by design: it consumes a new restart nonce in memory before
  it stops the old process, and on startup it adopts whatever nonce the
  core's first snapshot carries as the baseline. If the launcher itself is
  killed between consuming a nonce and spawning the replacement, the next
  launcher sees the same nonce, treats it as already applied, and does not
  finish the restart; the service stays as the crash left it (usually
  stopped, which a snapshot with `enabled: true` starts again on the next
  reconcile). Pressing Restart again completes it. Persisting consumed
  nonces would close this window at the cost of launcher state on disk; not
  worth it for a process that is not expected to crash.
- **POSIX only**, as stated under *Runtime*.
- **The credentials-file grammar is the launcher's own**, deliberately
  smaller than dotenv's: one `NAME=value` per line, the value split at the
  first `=` (so `API_KEY=foo=bar` is `foo=bar`), one optional pair of
  matching quotes whose contents are literal (spaces inside are kept), no
  escapes, no interpolation, no multiline values, no `export`. Anything
  else is refused with a line number. Tests pin those cases.

## Implementation and acceptance

Files: `source/main.py` (launcher), `source/core.py` (core), `source/test_main.py`, `source/test_core.py`, data-only
`source/services/definitions.py`, core-side `source/services/registry.py`,
`source/webapp/services_api.py`, and settings registration in
`source/db/settings.py`. Import the new view module in `source/webapp/__init__.py`.
Update `.gitignore`, `.env.example`'s guidance about launcher-only credentials,
`source/README.md`, and `source/notes/voice-and-services.md` when this ships.
No service toggle should appear until its endpoint and registry are wired up.

Verify with a fake core on a socketpair, fake children, temporary files, and a controllable clock;
real models, platform tokens, and network services are unnecessary:

- Import/spawn catalogue code under an isolated interpreter and prove no
  application/dependency imports. Verify child parent/group IDs and venv identity.
- Validate supported credential grammar, duplicate/malformed lines, explicit
  empty values, consistent environment precedence, file rotation, source labels,
  and the absence of unrelated credentials in each service environment.
- Malformed, duplicate, unknown-kind, and oversized desired lines cause no
  partial reconcile. A closed or wedged core channel cannot prevent reaping or
  shutdown deadlines; a send is bounded by its 2 s timeout.
- Toggle/restart affects only the selected process. An off/on between two
  snapshots changes its nonce; a restart during stopping coalesces. Core
  restart consumes its nonce once, preserves services, and the new core
  receives the full status table right after its first snapshot. Core failure
  exhaustion leaves services running and a local recovery diagnostic.
- Expected signal exits do not count as crashes; unexpected exit 0 does. Check
  rolling crash windows, backoff reset, deterministic exits, spawn errors, and
  disabled services cancelling scheduled retries.
- A fake child with a grandchild verifies group cleanup after leader death and
  the two shutdown phases; the core remains alive throughout service grace.
  Sidecar locks survive stale files and reject a concurrent owner. Abrupt launcher
  death is tested for the documented survivor/conflict behavior, not adoption.
- Status rejects stale sequence numbers; EOF on the channel makes the core
  unmanaged at once; unmanaged mode and `--core-only` suppression are
  distinguished from failure. Idle, the launcher sends nothing and its only
  timed wakeups are the inactivity log lines at 1, 10, 60 minutes, then hourly.
- Check static service bind addresses against entrypoints and exercise the
  implemented exit conventions in each isolated service suite.
- A malformed replacement credential or missing executable leaves an existing
  child running with a blocked-restart message; a new nonce retries after repair.

Out of scope: bridge implementation in phase 1, health checks/model readiness,
log capture/rotation, automatic dependency installation, orphan adoption,
multi-host supervision, resource limits, and launchd/systemd integration beyond
running the same launcher command.
