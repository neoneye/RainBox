# Benchmarks

## Purpose

`rainbox` has three benchmark harnesses, each with its own page,
background runner, and live-updating results grid:

- **`/benchmark_basic`** — coding/format probes (base64 decode/encode, reverse
  string, reverse list, tool ordering, tool routing) across every tuned
  model. Code: `benchmarks/basic.py`, `benchmarks/runner.py`,
  `webapp/benchmark_views.py`.
- **`/benchmark_editdocument`** — runs an edit-document agent (v1–v6) against
  a fixed set of edit tasks and scores the resulting document byte-for-byte.
  Code: `benchmarks/editdocument.py`, `benchmarks/editdocument_runner.py`,
  `webapp/benchmark_editdocument_views.py`.
- **`/benchmark_kanban`** — compares kanban context/action encodings
  (markdown vs JSON context, structured output vs function calling) for the
  agent-facing kanban operation shape. Code: `benchmarks/kanban.py`,
  `webapp/benchmark_kanban_views.py`.

The basic and edit-document harnesses share the same subprocess-runner shape;
this doc covers that shape, and in particular how each row runs in a
**killable child process** so Stop actually stops the model. The kanban harness
uses the same live page/start/stop/state pattern but its own spec set.

## Runner lifecycle

Each page owns one long-lived runner instance (`BenchmarkRunner` /
`BenchmarkEditDocumentRunner`), created in `webapp`. The runner:

- Holds a `_state` dict (targets, per-row status, per-trial tallies) that the
  page polls via `…/state`. The grid re-renders from that dict — the runner
  never pushes to the browser.
- `start(...)` spawns one background thread (`_run`) and returns immediately.
  `target_uuids=None` runs every row; a single uuid runs just that row (the
  per-row **Start** buttons) while other rows keep their cached results.
- `stop()` sets `_stop_event`.

A "target" / "row" is a `ModelConfigOverride` — the unconfigured base
`ModelConfig` rows are skipped, since you only benchmark configs you've
actually dialed in. `/benchmark_editdocument` further restricts to
function-calling overrides.

## Per-row child process

A benchmark trial is a blocking LLM call. Run in the runner thread, a runaway
model can't be aborted — it pegs CPU/GPU until the provider's timeout, and
`_stop_event` is only checked *between* trials. So each row runs in its own
**child process** the runner can SIGKILL — the same idea as the `/models` test
probes (see [LLM Providers → /models test probes](llm-providers.md)) and the
agent supervisor in `main.py`.

- **`benchmarks/subproc.py`** — `stream_target_subprocess(worker, request,
  on_event, stop_event)` spawns the worker, sends `request` as one JSON line on
  stdin, and relays the NDJSON events the worker writes to stdout, calling
  `on_event` for each. It watches `stop_event` through a `selectors` timeout
  rather than a blocking read, so a Stop is observed within ~0.25s even when the
  model has gone silent — then it SIGKILLs and reaps the child. Killing the
  process closes its HTTP socket to the provider, so the provider (e.g. Ollama)
  stops generating. Returns `True` if killed, `False` if the row finished.
- **`benchmarks/worker.py` / `benchmarks/editdocument_worker.py`** — each runs
  ONE target under a lightweight `db.make_app()` app context (no provider sync,
  no admin) and emits progress events on an isolated stdout fd; the benchmark's
  own output and library chatter is redirected to stderr so it can't corrupt the
  event stream.

### Event protocol

The worker writes one JSON object per line. The runner's `_apply_event` maps
each onto the existing `_state` setters, so the polling UI is unchanged.

`/benchmark_basic` worker:

- `{"t":"target_status","status":"warming_up"|"running"|"done"}`
- `{"t":"warmup_elapsed","elapsed":float}` / `{"t":"warmup_failed","error":str}`
- `{"t":"bench_status","bi":int,"status":"running"|"done"|"error","error"?:str}`
- `{"t":"trial","bi":int,"correct":bool,"had_error":bool,"elapsed":float}`

`/benchmark_editdocument` worker:

- `{"t":"target_status","status":"running"|"done"|"error"}`
- `{"t":"trial","test_name":str,"correct":bool,"elapsed":float, …}` plus the
  per-trial detail fields `error`, `applied`, `patches`, `agent_status`,
  `agent_comment`

### Stop flow

The **Stop** button → `…/stop` → `runner.stop()` → sets `_stop_event`.
`stream_target_subprocess` notices within ~0.25s and SIGKILLs the active child;
`_run` breaks its loop and marks the run finished. Rows run sequentially, so
killing the one in-flight child frees the stuck model immediately — no more
pegged GPU or manual restart.

### Trade-off

Each row pays subprocess startup — a fresh Python, the llama-index import, and a
Postgres connection (~2–4s) — before its first trial. Warmup is skipped in the
child when consecutive rows share a model (it's already resident).
