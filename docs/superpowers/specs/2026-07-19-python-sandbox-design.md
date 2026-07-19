# Python sandbox — `python_run` assistant capability

Give the assistant a way to write and execute small Python programs — big-number
math, string manipulation (reversal, regex search), and similar pure
computation — inside a Pyodide (WebAssembly) sandbox with packages, network,
and the host filesystem blocked, and hard resource limits enforced from
outside the sandbox.

## Decision: one capability, not two

The request imagines two tools (math programs, string programs). Both are "run
a Python program"; splitting them would duplicate the entire execution path and
give the model two identical actions to confuse. One action `python_run` whose
prompt description names both intended uses covers the request.

## Architecture

```
assistant (agents/assistant.py)
  └─ _action_python_run                      Capability: PYTHON_RUN
       └─ tools/python_sandbox/sandbox.py    run_python(code) -> SandboxResult
            └─ node runner.mjs               fresh subprocess per job
                 └─ Pyodide (WASM)           sanitized jsglobals, hardened
```

- **`source/tools/python_sandbox/runner.mjs`** — Node script. Loads Pyodide
  with a **sanitized `jsglobals`** object (only harmless JS builtins: Object,
  Array, Promise, TextEncoder, setTimeout, …). Python's `js` module therefore
  has **no `process`, no `fetch`, no `eval`, no `require`** — verified by
  probe: `js.process` → AttributeError, `pyodide.http.pyfetch` → NameError.
  After load it nulls out `pyodide_js.loadPackage` / `loadPackagesFromImports` /
  `mountNodeFS` / `FS` so Python code cannot install packages or mount the real
  filesystem. Reads one JSON job (`{"code": ...}`) from stdin, runs it through
  an embedded Python harness, prints one `RESULT {json}` line to stdout.
- **Python harness (inside runner.mjs)** — redirects `sys.stdout`/`sys.stderr`
  to StringIO, `exec`s the user code, and if the last statement is an
  expression, `eval`s it separately so the job returns a proper `repr`.
  Exceptions come back as a clean traceback (user frames only).
- **`source/tools/python_sandbox/sandbox.py`** — the Python-side API:
  `run_python(code, limits) -> SandboxResult(ok, stdout, stderr, result_repr,
  error, duration_seconds)`. Spawns `node runner.mjs` fresh per job (clean
  state, ~1.2 s startup measured), then enforces limits from the parent.
- **`source/tools/python_sandbox/package.json` + lock** — pins the `pyodide`
  npm package (~13 MB, bundles the wasm runtime + Python stdlib, so execution
  is fully offline). `node_modules/` is gitignored; `sandbox.py` returns a
  clear "run npm install in tools/python_sandbox" error when it is missing,
  and a "node not found" error when Node.js is absent.

## Sandbox properties (probed, then locked in by tests)

| Vector | Status |
| --- | --- |
| Host filesystem | Not mounted in Node — Pyodide sees only in-memory MEMFS (`/tmp`, `/home`, `/dev`, `/proc`, `/lib`); `/etc/passwd`, `/Users` don't exist |
| Network | `js.fetch` removed via jsglobals; `pyfetch` broken (no fetch/AbortController); WASM has no real sockets (`socket.connect` no-ops) |
| Package install | micropip never loaded; `pyodide_js.loadPackage` nulled; network blocked anyway |
| Host JS (`js.process`, env secrets) | Removed via sanitized jsglobals |
| Subprocess env | Runner gets a minimal env (PATH + LANG only — no `DATABASE_URL`, no secrets), cwd is an empty scratch dir |

Pyodide-in-Node is not a cryptographic security boundary against a determined
human attacker; the threat model is an LLM emitting buggy or runaway code. The
layers above (sanitized globals + hardening + minimal env + fresh process +
the kill limits below) contain that, and the fixed env means even a full JS
escape would find no secrets and die within the same limits.

## Resource limits (enforced by the parent, outside the sandbox)

- **CPU: 30 s** — `RLIMIT_CPU` set on the node child via `preexec_fn`; the
  kernel kills the process at 30 s of actual CPU time (this is exactly
  "100% CPU for longer than 30 s"). Works on macOS and Linux.
- **Memory: 100 MB** — Pyodide's baseline is ~220 MB RSS just to load, so the
  budget is **100 MB above baseline**: `sandbox.py` samples the child's RSS
  (`ps -o rss=`, no new dependency) when the runner prints `READY`, then polls
  every 0.2 s and SIGKILLs the process group when RSS exceeds baseline+100 MB.
- **Wall clock: 60 s** — covers load (~1.2 s) + run; guards sleep/deadlock
  that RLIMIT_CPU can't see. SIGKILL to the whole process group
  (`start_new_session=True`).
- Output: stdout/stderr each truncated in `sandbox.py`; the capability's
  `output_cap_chars` (8000) caps the final observation.

Each violated limit yields a distinct, model-readable error ("killed: exceeded
30s CPU", "killed: exceeded memory budget", "killed: exceeded 60s wall clock").

## Capability registration (agents/assistant.py)

- Enum: `PYTHON_RUN = "python_run"` (new `python` family, contiguous per the
  family convention).
- Registry record: `family="python"`, `read=False`, `write=False` (pure
  compute — touches no operator data), `network=False`,
  `required_args=("code",)`, `output_cap_chars=8000`, `timeout_seconds=60`
  (metadata; the runner enforces).
- Description tells the model: run a small self-contained Python program for
  math (e.g. multiplying big numbers) and string manipulation (reversal, regex
  search); stdlib only, no packages/network/files; print results or end with
  an expression; ~30 s CPU / 100 MB budget.
- `_action_python_run` maps `SandboxResult` to an `AssistantObservation`
  mirroring `_action_workspace_read_command`'s shape (ok flag + formatted
  text), including "blocked: …" texts for missing node / missing npm install.

## Testing

`source/tools/python_sandbox/test_python_sandbox.py` (pytest, skipped with a
clear message when node or node_modules is absent):

1. Math: big-int multiply returns exact repr.
2. String: reversal + `re.findall` work; stdout captured; last-expression repr.
3. Error: exception surfaces as a traceback, `ok=False`.
4. CPU kill: `while True: pass` dies with the CPU message (test overrides the
   limit down to ~2 s so the suite stays fast).
5. Memory kill: allocating far past the budget dies with the memory message
   (budget overridden down for speed).
6. Escapes blocked: `js.process`, `js.fetch`, `pyfetch`, `open('/etc/passwd')`,
   `os.listdir('/Users')`, `pyodide_js.loadPackage`, `import micropip`,
   `pyodide.code.run_js` — every one must fail.
7. Fresh state: a variable defined in one job is undefined in the next.

Plus an assistant-side test that `python_run` validates args and dispatches
(matching the existing capability-test patterns in `agents/`).
