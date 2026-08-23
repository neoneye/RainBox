# Benchmarks

## Purpose

`rainbox` has four benchmark harnesses, each with its own page,
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
- **`/benchmark_story`** — ten-turn conversations across the
  text/structured × no-tools/tools matrix, three trials each because one trial
  is ten LLM calls. The suite read for *cache* behaviour, which is why it is
  the one with a warm-up toggle. Code: `benchmarks/story.py`,
  `webapp/benchmark_story_views.py`.

Two further benchmarks have no page and no runner.
**`benchmarks/roster_completeness.py`** asks whether an incomplete roster's
caveat actually stops a model inferring absence — a claim about model
behaviour, so it cannot be a unit test. It renders the same list twice, with
and without the caveat, asks about somebody absent from it, and reports whether
each answer names the limit. `ROSTER_BENCH_MODELS` overrides the model list.

**`benchmarks/roster_paraphrase.py`**
is a CLI script measuring whether a derived roster is retrieved by phrasings
nobody authored (see `qa-system.md`, "Derived rosters"). It builds a synthetic
registry in a throwaway pgvector table, embeds it with the real embedding
model, and prints each query's roster rank and score. It refuses to run against
any database but `rainbox_claude`:

```
DATABASE_URL=postgresql+psycopg://localhost/rainbox_claude \
    python -m benchmarks.roster_paraphrase
```

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
- (`BenchmarkRunner` only) stores each cell's result to `benchmark_result` as
  it reaches a terminal state, so the grid outlives the process (see
  **Result history**).

A "target" / "row" is a `ModelConfigOverride` — the unconfigured base
`ModelConfig` rows are skipped, since you only benchmark configs you've
actually dialed in. `/benchmark_editdocument` further restricts to
function-calling overrides.

## Result history

`_state` is in memory, so on its own a restart empties every grid and there is
no earlier number to read a newly-added model against. `benchmark_result`
(`db/benchmark.py`) is what survives, for `/benchmark_basic`,
`/benchmark_story` and `/benchmark_kanban` — the three that share
`BenchmarkRunner`. `/benchmark_editdocument` has its own runner and keeps no
history.

The unit is a **cell**: one `(spec_set, benchmark_name, target)` triple. That
is already the atomic thing the runner executes — `start()` takes
`target_uuids` and `bench_indices`, and the per-cell Start buttons use both —
so a grid is a patchwork of separately started cells rather than one coherent
run, and there is nothing coherent to snapshot at table level.

Each cell keeps the newest **3 complete** results (the run finished *and*
every trial ran) and the newest **3 partial** ones (stopped or errored), in
separate buckets so a run of failures cannot evict the last known-good
baseline. Pruning happens inside the write, which keeps the table bounded
without a scheduled job.

Two details in the schema are load-bearing:

- Rows are keyed on `benchmark_name`, never the benchmark's index into a spec
  list. Indices shift whenever a set is reordered, which would silently
  re-point a cell's history at a different column — the kind of corruption
  nobody notices, because the numbers still look plausible.
- `model_name` / `target_label` are denormalized rather than joined. Targets
  are `ModelConfigOverride` rows, which get deleted and recreated freely; a
  join would make a removed override's history unreadable, while a stored name
  still says what was measured.

### When a result is written

`_set_benchmark_status` stores on `done` and `error`. `_finish` stores the
cells a Stop caught mid-trial, *before* resetting them to pending — that reset
is what would otherwise erase the evidence that they were the interrupted
ones.

A stop before the first trial writes nothing: it measured nothing, and a row
of zeroes would put a fake result into the baseline. An error writes whatever
the trial count, because "this target cannot do this benchmark" is itself the
finding.

Both paths snapshot under `_lock` and write outside it — every open benchmark
page polls `get_state()` on that same lock once a second, and a DB round-trip
inside it would stall all of them. `_run`'s `finally` opens its own app
context, since the one it holds for the run has already exited by then.

`_persist_cell` logs and swallows. A run costs real model time; losing one to
a telemetry bug is far worse than losing the telemetry (the same posture
`llm/activity.py` takes for its own recording).

### How the page reads it

`_state` stays purely live, so a stored result can never be mistaken for a
running one. Each page serves its history from `…/history` instead —
deliberately **not** a field on `…/state`, which is polled once a second and
would then carry the whole table's past on the wire every second for as long
as the page is open. Story artifacts are held off the polled state for exactly
the same reason.

The page fetches history before its first paint, and again whenever `running`
goes true → false so a cell that just completed shows its new entry without a
reload. A cell with no live result renders its newest stored one, greyed and
italic behind an hourglass so a stale number is never read as fresh; hovering
any cell with history shows the retained runs, complete then partial. A cell
with no history shows no card at all — an empty box on hover is worse than no
box.

Historic cells count toward the row score. Without that, a grid restored after
a restart would rank every row 0.0000, which is less useful than showing
nothing.

Each row stores a fingerprint of the target's resolved model kwargs and of the
benchmark's spec params. An entry whose fingerprint differs from the newest is
**flagged in the hover card, never hidden or deleted**: seeing what changed
when you retuned a model is the reason to keep the earlier number at all.

Rows also appear read-only under Admin → Feedback. Editing one there would be
discarded by the next write's pruning pass, so the view does not offer it.

## Warm up

Before a target's first trial the child sends one throwaway `complete("hi")`,
so a cold model's load time doesn't land inside the first benchmark's average.
It is skipped automatically when the previous target used the same model, which
is already in memory.

`/benchmark_story` carries a **Warm up LLM** checkbox, **off by default**, kept
in `localStorage` under `pp-benchmark-warmup` and sent to `…/start` as
`?warmup=0|1`. The story suite is the one read for *cache* behaviour, and there
a model warmed before the first trial is the thing under observation, not noise
to remove. The other pages are read for timings and always warm up; giving one
of them the toggle is `show_warmup_toggle=True` plus passing `warmup` through
that page's start endpoint.

The setting lives in the browser rather than in the runner because it describes
how the operator wants to read a run, not a property of the run — it has to
survive a page load and must not be reset by whatever the previous run used. A
start request with no `warmup` parameter warms up, so anything predating the
toggle behaves as before.

## One suite at a time

There are four runner instances — general, kanban, story, edit-document — and
each used to guard only its own re-entry, so two pages could be started minutes
apart and drive two models at once. The models run locally on one consumer
machine: two suites in flight means two models resident and competing for the
same GPU, which is slower than running them in turn and makes every timing the
benchmarks record meaningless.

So the runners share one slot (`benchmarks/exclusion.py`). `start()` takes it or
returns `False`; `_finish()` (the worker thread's `finally`, reached whether the
run ended, was stopped, or raised) hands it back. An in-process lock suffices
even though trials execute in child processes, because the runner thread that
spawned a child blocks until that child is done — the slot covers the whole row.

`…/state` carries `blocked_by`: the label of the suite currently holding the
slot, or `null`. It is keyed on the holder rather than on `running`, since
`start()` takes the slot a moment before it sets `running` and the holder must
never report itself as blocked. A blocked page disables every Start control,
says which suite is running, and keeps polling so it re-enables itself when that
run ends.

This covers the benchmark pages only. The supervisor in `main.py` spawns
assistant agent processes on its own schedule and does not consult the slot, so
an assistant turn can still overlap a benchmark run.

## Per-row child process

A benchmark trial is a blocking LLM call. Run in the runner thread, a runaway
model can't be aborted — it pegs CPU/GPU until the provider's timeout, and
`_stop_event` is only checked *between* trials. So each row runs in its own
**child process** the runner can SIGKILL — the same idea as the `/model` test
probes (see [LLM Providers → /model test probes](llm-providers.md)) and the
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
