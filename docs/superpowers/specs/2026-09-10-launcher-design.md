# Launcher: one small process that starts the rest

**Date:** 2026-09-10

**Status:** design; nothing implemented yet.

**Roadmap:** this comes first. When the launcher runs the core and the
static services reliably, the chat-bridge settings design
(`2026-09-09-bridge-settings-design.md`) resumes on top of it; until then
the Discord and Telegram bridges keep being started by hand exactly as
their READMEs describe.

## Problem

Starting rainbox today means starting several processes by hand: `main.py`
(the core: agent supervisor plus webserver), and whichever side services
are wanted — Kokoro TTS, Whisper STT, dots.tts, the reranker, the Discord
and Telegram bridges — each from its own directory with its own venv.
`source/notes/voice-and-services.md` says it plainly: "The main app does
not start these services; each is started by hand." Two consequences:

- Turning a service on or off is a terminal task, not a setting.
- Every rainbox process is a Python interpreter named "Python" in Activity
  Monitor, with no common parent, so a process at 100 % CPU is hard to
  attribute.

The core cannot simply become the parent of everything: it is large, it is
multithreaded, and the operator wants to restart it rarely because agents
may be mid-turn. Making it the parent would also mean that restarting or
losing the core takes every service down with it.

## Decision

A small top-level **launcher** (`source/launcher.py`) is the root of the
process tree. Its only job is to start processes, keep the ones that should
be running running, stop the ones that should not, and report what it sees.
It starts the core the same way it starts a service, so the core is a
*sibling* of the services, not their parent.

```text
launcher.py            (stdlib only, single-threaded, ~15 MB)
├── main.py            (core: supervisor thread + webserver)
│   ├── python -m agents …
│   └── python -m agents …
├── voice_tts_kokoro/venv/bin/python server.py
├── voice_stt_whisper/venv/bin/python server.py
└── reranker/venv/bin/python server.py
```

That tree is what Activity Monitor's hierarchical view and
`ps -o pid,ppid,%cpu,command` show, because each child's parent is the
launcher. What the operator turns on and off lives in the core's settings
and is rendered on `/settings` as ordinary toggles; the launcher asks the
core what should be running and reports back what is.

`main.py`'s own supervisor is unchanged: it still spawns and watches agents
over their socketpairs (`python -m agents --socket-fd N`). Agents stay
children of the core because the core is what hands them work.

## Why the launcher is small, and why it must exec

The launcher imports nothing from the application — no Flask, no
SQLAlchemy, no `db`, no `requests` — only the standard library, and a test
asserts its module set. That, not the spawn call, is what keeps its
footprint minimal.

It creates children with `subprocess.Popen(argv, cwd=…, env=…,
start_new_session=True)`, which forks and immediately execs the service's
own interpreter. CPython picks `vfork`/`posix_spawn` or `fork`+`exec`
depending on the arguments; the child's parent pid is the launcher in
every case, so the mechanism does not affect what Activity Monitor shows.
Two things the launcher deliberately does not do:

- **Fork without exec.** A forked child shares the parent's pages
  copy-on-write, so forking a process with an application loaded yields a
  child as large as the parent until it execs; and forking a multithreaded
  Python process on macOS without an immediate exec is unsafe (only the
  calling thread survives; locks held by other threads stay held), which is
  why Python's own `multiprocessing` defaults to spawn on macOS. The
  launcher stays single-threaded — one `select`/`poll` loop — and every
  child is a fresh interpreter.
- **Serve HTTP.** The launcher has no server and no database connection.
  It *pulls* what to run from the core and *pushes* what it sees, both as an
  HTTP client over `127.0.0.1` using `urllib` from the standard library.

`start_new_session=True` matters for Ctrl-C: a terminal delivers `SIGINT`
to the whole foreground process group, which would hit every child at once
and bypass the ordered shutdown below. In its own session a child gets no
terminal signals; the launcher receives the `SIGINT` and stops its children
in order.

## Starting the launcher

```bash
cd source
venv/bin/python launcher.py [--state-dir <dir>] [--core-only]
```

- It is started with the root venv's interpreter and has no requirements of
  its own. `sys.executable` is what it runs the core with, so the core gets
  the venv the operator chose for the launcher.
- `--state-dir` (default `<repo>/var/services/`, gitignored) is where the
  launcher keeps runtime facts: its own lock file and, later, the bridges'
  state files. Where a host keeps runtime state is the launcher's fact, not
  the database's.
- `--core-only` runs the core and nothing else; useful while developing the
  launcher itself.
- One launcher per state dir: it takes an exclusive OS lock on
  `<state-dir>/launcher.lock` for its lifetime and exits with code 3 if the
  lock is held.

This is also the command a future launchd job would run to start rainbox
at login; nothing else in the tree needs to know about launchd.

## Bootstrap and the control loop

1. Read `.env` from the repo root with the launcher's **own parser**, not
   `python-dotenv` (stdlib rule). The parser handles the subset
   `.env.example` uses: `KEY=value`, single and double quotes, `#` comments,
   blank lines. A test feeds `.env.example` and a fixture of edge cases to
   both parsers and asserts identical results, so the launcher and the core
   (which loads the same file through `source/env_file.py`) can never
   disagree about a value. Values from the launcher's own environment win
   over the file, matching `load_dotenv(..., override=False)`.
2. Start the core unconditionally: `sys.executable main.py` with
   `cwd=source/` and the core's full environment (the core is the one child
   that gets everything, because it is the one that needs provider keys and
   `DATABASE_URL`).
3. Poll `GET /services/api/desired` on the core until it answers, then every
   5 seconds. The response lists every service that should be running (see
   *Desired-state contract*). The launcher knows nothing about what a
   service *is*; it runs what it is told.
4. Reconcile:
   - desired and not running → spawn;
   - running and not desired (toggled off) → `SIGTERM`, then `SIGKILL`
     after 10 s (the same grace and escalation `main.py` gives its agents,
     `TERM_GRACE`);
   - `restart_nonce` changed → stop as above, then spawn; this is what a
     *Restart* button does;
   - exited unexpectedly → respawn with exponential backoff (2 s, 4 s, …
     capped at 60 s); five crashes inside two minutes → **failed**, no
     respawn until the operator toggles it off and on or presses *Restart*;
   - exited with a *deterministic* failure code (see *Exit codes*) →
     **failed** immediately, no backoff loop, because respawning cannot fix
     it.
5. After every change, `POST /services/api/status` with the full table
   `{key: {state, pid, since, last_exit, message}}`; also re-post it every
   30 s as a heartbeat, so a core that restarted learns the current picture
   without waiting for a change.
6. On its own `SIGINT`/`SIGTERM`: `SIGTERM` every service first and the core
   last, so a service unwinding an in-flight request still has a live core
   to finish against; wait 10 s; `SIGKILL` the rest; release the lock; exit.
   The core's own shutdown kills its agents as it does today.

When the core is unreachable the launcher **keeps the services it has
running as they are** and keeps restarting the core. Losing the core is
never a reason to stop a service; the last valid desired state stands until
a new one arrives. (This is the opposite of the rule the chat bridges apply
to their *own* configuration, where stale config pauses traffic; a bridge
protects a third party's channel, the launcher protects uptime.)

Because the core is a child, a core restart is just step 4 for the entry
`core`. Services keep running, notice their connections drop, back off, and
reconnect, which every rainbox side service already does.

## Desired-state contract

```text
GET /services/api/desired
  -> {
       schema_version: 1,
       services: [
         {key: "voice_tts_kokoro", dir: "voice_tts_kokoro",
          argv: ["venv/bin/python", "server.py"],
          env: {"KOKORO_PORT": "5005"},
          restart_nonce: "…"},
         …
       ]
     }
```

- `dir` is relative to `source/`; `argv[0]` is relative to `dir`. A missing
  `argv[0]` (no venv) is reported as **not installed** and not spawned.
- `env` is the complete non-secret environment the child needs beyond the
  baseline. The launcher builds each child's environment **from scratch**:
  `PATH`, `HOME`, `LANG`/`LC_*`, `TMPDIR`, then exactly these keys. Unlike an
  agent, which inherits `dict(os.environ)` from the core, a service sees
  nothing it was not given.
- `token_env` (optional) names one variable whose *value* the launcher must
  supply from `.env` or its own environment. The response carries only the
  name; the core does not have the value and must not. The launcher
  re-reads `.env` at each spawn and prefers the file over its own
  environment, so a rotated credential reaches a service on *Restart*
  without restarting anything else. A name present in neither place is
  reported as **credential missing**, by variable name, and not spawned.
  (No static service needs this today; it exists for the bridges.)
- `restart_nonce` is an opaque string. The core persists it per service as
  an internal setting (`services.<key>.restart_nonce`, `internal=True` so
  `/settings` does not list it); a *Restart* action rewrites it. Persisting
  it is what stops a core restart from looking like "every nonce changed"
  and restarting every service.
- The core builds the list from its **service registry** plus its settings.
  The registry is code, the same shape as `SETTINGS` in
  `source/db/settings.py`: one entry per known service with its key, `dir`,
  `argv`, `env`, and the setting that switches it. The registry also adds
  a bool setting `services.<key>.enabled` (default `false`) per entry, which
  `/settings` renders as a toggle like any other bool. The `core` entry is
  implicit: always desired, never toggleable, but restartable.

Initial registry: `voice_tts_kokoro`, `voice_stt_whisper`,
`voice_tts_dotstts`, `reranker`. Their discovery URLs (`KOKORO_TTS_URL`,
`WHISPER_STT_URL`, `DOTS_TTS_URL`) and default ports (5005, 5006, 5007) are
unchanged; the launcher runs the process that answers there. The bridges
are **not** in the initial registry: their configuration design adds them
as dynamic entries later.

## Status contract

```text
POST /services/api/status
  {launcher: {pid, state_dir, started_at},
   services: {key: {state, pid, since, last_exit, message}}}
```

States: `running`, `starting`, `stopping`, `stopped`, `failed`,
`credential missing`, `not installed`. The core keeps the last table and its
arrival time and renders it beside each toggle on `/settings`, with a
*Restart* button. It shows every service as **unknown** when no status has
arrived for 90 s — no launcher, or a launcher that died — and shows the
`core` entry's own state so the page can say when the core is running
unlaunched. A webapp served by `tools.serve_ui` shows `unknown` always.

Status is ephemeral: it lives in the core's memory, never in Postgres.

## Exit codes

Stdout and stderr are inherited, so every service's log lines land in the
launcher's terminal, prefixed by the service itself — which also means the
launcher never *reads* them. The only thing it learns from a child is its
exit status, so services use a small convention:

| Exit code | Meaning | Launcher reaction |
|---|---|---|
| `0` | clean exit after `SIGTERM` | `stopped` |
| `2` | configuration or credential rejected | `failed`, no respawn; the service's own log line has the detail |
| `3` | a lock the service needs is held by another process | `failed`, "locked by another process; see its log for the pid"; no respawn |
| anything else, or death by signal | crash | respawn with backoff; five in two minutes → `failed` |

Codes 2 and 3 get no backoff loop because respawning cannot fix them. The
existing services and bridges already exit through `SystemExit` for these
conditions and only need the codes assigned.

## Telling the processes apart

Every rainbox process is a Python interpreter, and Activity Monitor names a
process after its executable, so without help they all read "Python". The
hierarchy is the first fix: under the launcher, a process at 100 % CPU is
one hop from a named parent, and `ps -o pid,ppid,%cpu,command` shows each
child's full argv (`server.py`, `-m agents`, `bridge.py`).

A second fix is worth one experiment before relying on it: exec each
service through a symlink named for it
(`bin/rainbox-kokoro -> voice_tts_kokoro/venv/bin/python`), so `argv[0]` —
and, if Activity Monitor uses the exec path rather than the resolved
binary, its Process Name column — reads `rainbox-kokoro`. Whether Activity
Monitor honors the symlink name is not something to assert from memory;
run it once and keep it only if it shows.

## Manual mode and duplicates

Nothing forbids starting a service by hand while the launcher runs. For a
service with a port, the second instance fails to bind and exits nonzero;
the launcher sees a crash and backs off, and the `/settings` row shows
**failed** — the signal that two launchers are configured. Services that
own a state file use exit code 3 for the same purpose. Across hosts nothing
here can help; the launcher is single-host by design.

## Files

```text
source/launcher.py            the launcher; stdlib only
source/test_launcher.py       parser, reconcile, exit-code mapping, env
                              construction, ordered shutdown — with a fake
                              core (a tiny HTTP server in the test) and
                              fake children (sleep / exit-with-code scripts)
source/services/registry.py   the service registry (core side)
source/webapp/services_api.py /services/api/desired and /status
source/db/settings.py         services.<key>.enabled toggles,
                              services.<key>.restart_nonce (internal)
var/services/                 launcher state dir (gitignored)
```

`source/README.md`'s layout table gains `launcher.py`;
`source/notes/voice-and-services.md` is the operator's file and is theirs
to update once this ships.

## Acceptance checks

- The launcher's imported module set is stdlib-only (asserted by a test that
  runs it under `-X importtime` or inspects `sys.modules` after import).
- Every child's parent pid is the launcher's; every child is in its own
  session; `SIGINT` to the launcher's terminal reaches no child directly.
- A child's environment contains the baseline keys plus exactly the
  declared `env` plus the one `token_env` value, and nothing else from the
  launcher's environment.
- The `.env` parser agrees with `python-dotenv` on `.env.example` and on the
  edge-case fixture; launcher-environment values win over file values; a
  rotated file value reaches a child on *Restart* without restarting the
  launcher or the core.
- Toggling a service on `/settings` changes only that child within one poll;
  the core's pid, webserver, and running agents are untouched. *Restart*
  restarts only its service. A core restart does not restart any service
  (persisted nonces).
- Killing the core: services keep running; the launcher restarts the core;
  the new core learns the status table within 30 s.
- Exit code 2 and 3 produce `failed` with no respawn; other exits respawn
  with backoff; five crashes in two minutes produce `failed`. Missing venv
  and missing credential are reported, not spawned, not crash-looped.
- Ordered shutdown: services first, core last, 10 s grace, `SIGKILL` for
  the rest, lock released; a second launcher on the same state dir exits
  with code 3 while the first runs and starts after it exits.
- The core reports `unknown` for every service 90 s after the last status
  post; `tools.serve_ui` reports `unknown` always.

## Out of scope

- Bridge connectors as dynamic entries (the bridge settings design).
- Health checks over HTTP; liveness is "the process is alive".
- Capturing or rotating child logs.
- Multi-host operation and launchd/systemd integration beyond "this is the
  command to run".
- Per-service resource limits or priorities.
