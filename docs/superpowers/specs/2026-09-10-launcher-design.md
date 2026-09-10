# Launcher: one small process that starts the rest

**Date:** 2026-09-10

**Status:** phase 1 implemented (`source/main.py` — the launcher IS the entrypoint; the core moved to `source/core.py`, `source/services/`,
`source/webapp/services_api.py`, the `services.*` settings and the /settings
launcher card). Two runtime facts beyond the text below: `RAINBOX_CORE_PORT`
overrides the core's port for both `core.py` and the launcher, so a second
core can run beside the operator's for a smoke test; and a status post that
fails at the transport level is retried after 2 seconds rather than at the
next heartbeat, so a restarted core learns the table within seconds.

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
  stdin=DEVNULL, close_fds=True)`. Use absolute executable/script paths, resolved
  from the local catalogue. Preserve a venv interpreter's invocation path even
  if it is itself a symlink; resolving it to the base Python can lose the venv.
  Stdout/stderr are inherited. Do not use `preexec_fn` or fork without exec.

Each service gets its own process group/session. Terminal Ctrl-C reaches the
launcher, which performs the shutdown sequence below. Exact fork/spawn selection
is Python's implementation detail; the relevant contract is documented by
[Python's subprocess API](https://docs.python.org/3/library/subprocess.html).

The single-threaded loop polls/reaps children and uses monotonic deadlines,
without long sleeps or blocking waits. HTTP uses stdlib `urllib`, with proxies and redirects disabled
for the fixed `http://127.0.0.1:5000` control endpoint. Bound each request to one
second of elapsed time and each response to 1 MiB; a socket inactivity timeout
alone must not let a trickling response block supervision indefinitely. Check
shutdown and child deadlines before starting a request. Deadline handling may
be delayed by at most one outstanding HTTP request.

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

At **every spawn**, resolve a credential as startup launcher environment value
if its name is present, otherwise the freshly parsed credentials-file value.
An explicitly empty value means missing and does not fall through. Environment
wins consistently; the file never overrides an exported value just on restart.
Show the selected source (environment/file) without its value. File rotation
reaches the next Restart when the file is the selected source; changing an
exported value requires restarting the launcher. Running children keep their
current environment. Invalid file syntax prevents file-backed spawns, not the
core or already-running services. Neither source is copied into DB, argv, status,
or the desired-state API. A child may still deliberately read local files:
environment filtering is not an OS sandbox.

## Bootstrap and core identity

The core is always desired and is not part of the service list. Generate a
fresh launcher UUID and a fresh core-instance UUID for each core spawn. Pass
them as `RAINBOX_LAUNCHER_ID` and `RAINBOX_CORE_INSTANCE_ID` to the core;
`/services/api/desired` echoes them and the core PID.
Do not accept a response from a different instance, even at the right port.
Status POSTs must carry the matching markers. These identify this launcher's
child, not authenticate the existing unauthenticated localhost API.

Before spawning the core, check whether the core could bind its port: attempt
the same bind the core's server makes (the specific loopback address with
`SO_REUSEADDR`, werkzeug's `allow_reuse_address`), not a connect. On macOS,
ControlCenter's AirPlay Receiver listens on `*:5000` and answers loopback
connects, yet the core's `127.0.0.1:5000` bind succeeds beside it and loopback
traffic reaches the more specific socket — a connect probe would refuse to
start on every Mac with AirPlay Receiver on. If the bind fails, stop bootstrap
with a diagnostic that says whether the occupant answers the rainbox control
API (an unmanaged core) or is another application, and do not adopt or signal
it. The check is advisory: a bind race can still occur, and the instance check
prevents reconciling against an unrelated core that won the race.

Start the core before polling desired state. Until the first complete valid
snapshot, start no side services. Thereafter poll every 5 seconds, with at most
one request outstanding. Errors, 404s, wrong instance markers, malformed data,
unknown schema versions, duplicate keys, and truncated/oversized responses leave
the last valid snapshot intact. They never mean “disable everything.”

Keep already-known service desired state during core downtime, including normal
crash recovery for those services. Start no newly discovered services without a
valid snapshot. Bridge processes separately pause message traffic when their
own configuration expires; the launcher keeping a process alive grants no
permission to keep forwarding.

## Desired-state contract

```json
{
  "schema_version": 1,
  "launcher_id": "<launcher-uuid>",
  "core_instance_id": "<core-instance-uuid>",
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

`GET /services/api/desired` returns a complete, coherent snapshot including
**disabled** entries. Missing static entries invalidate it. A missing dynamic
bridge key in a valid snapshot means removal. Validate the whole response before
reconciling any part of it; unknown kinds invalidate the response and report
that launcher/core versions disagree.

The local data-only catalogue fixes each kind's directory, argv, allowed
nonsecret environment keys, and defaults. The core imports the same catalogue
to build its registry and settings. HTTP cannot provide arbitrary paths, argv,
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
also changes its nonce atomically, so a quick off/on between polls can reset a
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

```json
{
  "schema_version": 1,
  "launcher_id": "<launcher-uuid>",
  "core_instance_id": "<core-instance-uuid>",
  "sequence": 7,
  "launcher": {"pid": 122, "state_dir": "<absolute-dir>", "started_at": "<UTC>"},
  "services": {
    "voice_tts_kokoro": {
      "state": "running", "pid": 124, "since": "<UTC>",
      "last_exit": null, "message": null
    }
  }
}
```

POST the full table to `/services/api/status` after changes (coalesced) and every
30 seconds, including `core` and disabled services. Status failures do not block
reconciliation. The core accepts only its instance markers and increasing
sequence numbers; receipt time is local to the core, not supplied by the client.
Status stays in memory. A restarted core learns it on the next successful post.

States: `starting`, `running`, `stopping`, `stopped`, `backoff`, `failed`,
`credential missing`, `not installed`. Include next-retry time for `backoff` and
credential source for credential-related states, never a credential value.
`/settings` displays the desired toggle separately from observed state, with a
POST Restart action. `running` describes liveness only. With no heartbeat for
90 seconds, show observed state as `unknown`; do not alter the desired toggle.
A directly started core can identify itself as unmanaged from its missing
instance markers. `tools.serve_ui` must not accept launcher status or desired
requests as a supervisor instance; it displays unmanaged/unknown.

Inherited child output may interleave and is not currently guaranteed to have
service prefixes. The launcher logs service key, PID, spawn, exit, and signals
itself; it does not read child output or promise to expose child error text.
Activity Monitor hierarchy and the key-to-PID status table are the supported
way to identify children. Process-name symlinks are an optional later experiment,
not a prerequisite or a reason to risk bypassing a service venv.

A port conflict means an unmanaged instance or another application may be there,
not necessarily a second launcher. Only the process this launcher owns appears
in its status table. A manually started duplicate failing does not change the
healthy supervised child's state; the reverse ownership order may cause the
supervised attempt to fail. State-dir locks cannot prevent duplicate launchers
using different directories from competing for the same fixed core port.

## Idle cost and shortcomings

rainbox is meant to sit idle without waking the CPU or GPU. The launcher is a
polling design, and polling has an idle cost; it was measured on 2026-09-10
(macOS, Apple Silicon, sandbox database, `--core-only`, nothing enqueued),
sampling `top` per process: context switches over 30 s as the wakeup count,
and %CPU averaged over 60 s.

| Process | Context switches / 30 s | CPU idle |
|---|---|---|
| core alone (`core.py`, unmanaged) | 144–194 (varies with its own 5 s ticks) | 0.09–0.14 % |
| launcher, fixed 0.25 s timer (first implementation) | 160 | 0.14 CPU-s in 128 s |
| launcher, deadline-driven loop (current) | 36 | 0.11 CPU-s in 136 s |
| core under the launcher | +77 over core alone | 0.25–0.27 % |

What the numbers say:

- **The launcher itself is now cheap.** Its loop sleeps until its next real
  deadline and is woken early only by signals (`SIGCHLD` for a child exit,
  `SIGTERM`/`SIGINT` for shutdown), so an idle launcher wakes for its
  5-second poll and nothing else. The first implementation used a fixed
  0.25 s `select` timeout — four wakeups a second for no reason — which is
  why that row exists: it is the kind of regression this project is meant
  to avoid, and the deadline-driven loop replaced it.
- **The core's idle got worse, not better.** Every `POLL_INTERVAL` (5 s) the
  launcher makes one HTTP request that the core serves as a full Flask
  request: a session, one `SELECT` over `app_setting`, JSON. Every
  `HEARTBEAT_INTERVAL` (30 s) it posts the status table. That is seven
  requests per 30 s, about 77 extra context switches and 0.1–0.15 %
  CPU on the core — roughly double the core's own idle. Small in absolute
  terms, but it is new work on a process whose idle was the baseline this
  project protects, and it scales with nothing: it is paid whether or not a
  single service is enabled.
- **The 5-second poll exists for responsiveness, not correctness.** A toggle
  on `/settings` takes effect within one poll. Nothing else needs the
  interval; the core could tell the launcher when something changed.

Remedy, not yet built (it changes the control contract, so it belongs in its
own change):

- **Long-poll the desired state and piggyback status on it.** One request:
  `GET /services/api/desired?wait=55&status=<table>` — the core records the
  status from the query body, then holds the response until a service
  setting or nonce changes (a `threading.Event` set by
  `set_service_setting` / `bump_restart_nonce`) or the wait expires. Idle,
  that is one request a minute instead of seven per 30 s, and a toggle
  applies in milliseconds instead of up to 5 s. The launcher's single thread
  cannot block on a 55 s request without losing its 1 s reaping bound, so
  the long-poll runs on one helper thread that hands the parsed snapshot to
  the loop through the existing wakeup fd; the loop's own HTTP client keeps
  its 1 s deadline for the fallback poll when the long-poll is down.
- **Stretch the heartbeat once status rides on the long-poll**: the 90 s
  staleness rule becomes "three missed long-polls", and the separate POST
  disappears.

Other shortcomings worth knowing:

- Every idle wakeup in the core is its own (the 5 s `IDLE_TICK_TIMEOUT`
  Postgres poll and the 5 s cron tick); the launcher neither fixes nor
  worsens those.
- `running` means the process is alive, not that the service answers; a
  service loading a model for a minute is `running` the whole time.
- The launcher does not install venvs or models; `not installed` is a
  report, not an action.
- Log lines of every child interleave on the launcher's terminal, unprefixed
  unless the service prefixes its own.

## Implementation and acceptance

Files: `source/main.py` (launcher), `source/core.py` (core), `source/test_main.py`, `source/test_core.py`, data-only
`source/services/definitions.py`, core-side `source/services/registry.py`,
`source/webapp/services_api.py`, and settings registration in
`source/db/settings.py`. Import the new view module in `source/webapp/__init__.py`.
Update `.gitignore`, `.env.example`'s guidance about launcher-only credentials,
`source/README.md`, and `source/notes/voice-and-services.md` when this ships.
No service toggle should appear until its endpoint and registry are wired up.

Verify with fake HTTP peers/children, temporary files, and a controllable clock;
real models, platform tokens, and network services are unnecessary:

- Import/spawn catalogue code under an isolated interpreter and prove no
  application/dependency imports. Verify child parent/group IDs and venv identity.
- Validate supported credential grammar, duplicate/malformed lines, explicit
  empty values, consistent environment precedence, file rotation, source labels,
  and the absence of unrelated credentials in each service environment.
- Wrong-instance, malformed, duplicate, truncated, and oversized desired payloads
  cause no partial reconcile. Slow/unavailable HTTP cannot prevent reaping,
  freshness/status updates, or shutdown deadlines beyond one request bound.
- Toggle/restart affects only the selected process. An off/on between polls
  changes its nonce; a restart during stopping coalesces. Core restart consumes
  its nonce once, preserves services, and restores their status within 30 seconds
  of reachable HTTP. Core failure exhaustion leaves services running and a local
  recovery diagnostic.
- Expected signal exits do not count as crashes; unexpected exit 0 does. Check
  rolling crash windows, backoff reset, deterministic exits, spawn errors, and
  disabled services cancelling scheduled retries.
- A fake child with a grandchild verifies group cleanup after leader death and
  the two shutdown phases; the core remains alive throughout service grace.
  Sidecar locks survive stale files and reject a concurrent owner. Abrupt launcher
  death is tested for the documented survivor/conflict behavior, not adoption.
- Status rejects old instance/sequence reports, expires after 90 seconds, and
  distinguishes unmanaged mode and `--core-only` suppression from failure.
- Check static service bind addresses against entrypoints and exercise the
  implemented exit conventions in each isolated service suite.
- A malformed replacement credential or missing executable leaves an existing
  child running with a blocked-restart message; a new nonce retries after repair.

Out of scope: bridge implementation in phase 1, health checks/model readiness,
log capture/rotation, automatic dependency installation, orphan adoption,
multi-host supervision, resource limits, and launchd/systemd integration beyond
running the same launcher command.
