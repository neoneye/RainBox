# Benchmark result history

## Problem

`/benchmark_basic`, `/benchmark_story` and `/benchmark_kanban` keep their
results in `BenchmarkRunner._state`, a plain dict on a module-level instance.
Nothing is persisted. Restarting the app empties every table, so there is no
baseline to compare a newly-added model against — the numbers you would want
to compare it to are gone.

The three pages are three instances of one `BenchmarkRunner`
(`webapp/core.py`), rendered by one `render_benchmark_page`, so one mechanism
serves all three. `/benchmark_editdocument` has its own separate runner and
views and is out of scope.

## Goal

Persist each cell's result, show the most recent one when the page loads, and
expose the last few on hover so a re-run can be read against what came before.

## The unit is a cell

A cell is one `(spec_set, benchmark_name, target)` triple. That is already the
atomic thing the runner executes: `start()` takes `target_uuids` and
`bench_indices`, the page has a per-cell Start button, and the method's own
docstring notes the table "shows accumulated results across multiple
per-target Start clicks". The table as a whole is a patchwork of separately
started cells, not a coherent run, so there is nothing coherent to snapshot at
table level.

Per cell, retain:

- the newest **3 complete** results — every trial ran
- the newest **3 partial** results — stopped or errored

Both, because a model that times out or refuses is itself a finding, and
keeping partials in their own bucket stops a run of failures from evicting the
last good baseline.

## Storage

One table, `benchmark_result`, one row per cell execution that reached a
terminal state. `db.create_all()` picks up a new table on startup, so no
migration step is needed.

| column | purpose |
|---|---|
| `uuid` | identity |
| `spec_set` | `general` / `kanban` / `story` — which page |
| `benchmark_name` | `base64_decode`, `kanban_md_struct`, … |
| `target_uuid` | the `model_config_override` row benchmarked |
| `target_label`, `model_name`, `provider` | denormalized |
| `completed` | true = every trial ran |
| `status` | `done` / `error` / `stopped` |
| `trials_done`, `trials_total` | |
| `correct`, `mistakes`, `failures` | |
| `total_elapsed` | seconds across the trials that ran |
| `reasoning_chars`, `content_chars` | |
| `error` | the message, when there was one |
| `config_fingerprint` | hash of the target's resolved model kwargs |
| `spec_fingerprint` | hash of the benchmark's spec params |
| `started_at`, `ended_at` | |

**Keyed on `benchmark_name`, not the benchmark's index.** Indices shift
whenever `BENCHMARK_SPECS` is reordered or an entry is inserted, which would
silently re-attach a cell's history to a different column — the kind of
corruption nobody notices because the numbers still look plausible.

**The label columns are denormalized on purpose.** Targets are
`ModelConfigOverride` rows, which the operator deletes and recreates freely. A
join would make a deleted override's history unreadable; carrying the name
means an old row still says what it measured.

### Fingerprints

`config_fingerprint` hashes `db.resolved_model_kwargs(target_uuid)`;
`spec_fingerprint` hashes the benchmark's params from the spec table
(`{"num_trials": 5, "string_length": 6}`). Both are canonical-JSON blake2b.

They exist to answer "is this old number comparable?", and the answer is
**shown, never enforced**. A history entry whose fingerprint differs from the
current one is flagged in the hover card; it is not hidden and not deleted.
Seeing what changed when you raised the temperature is the whole point of a
benchmark page — a design that quietly drops the before-value defeats it.

### Retention

On each write, prune that cell to the newest 3 complete and newest 3 partial.
Pruning at write time keeps the table bounded with no scheduled job, and the
write is already in a transaction.

## Write path

`BenchmarkRunner` gains one persistence call, at the two places a cell reaches
a terminal state:

1. `_set_benchmark_status(...)` with status `done` or `error`.
   `completed = (status == "done" and trials_done == trials_total)`.
2. `_finish(aborted=True)`, which today resets in-progress cells to `pending`.
   Before that reset, any cell with `trials_done > 0` is persisted as
   `stopped`, partial. A cell killed with zero trials done is not recorded —
   it measured nothing.

Two constraints on the implementation:

- **`_finish` has no app context.** In `_run`, `with app.app_context()` is
  *inside* the `try`, while `_finish` is called from the `finally` — so the
  context has already exited by then. `_run`'s finally wraps the `_finish`
  call in its own `app.app_context()`.
- **The DB write must not hold `self._lock`.** The page polls `get_state()`
  once a second and that takes the same lock. Both hooks snapshot the entry
  under the lock, release, then write.

A failing write must never break a run. The persistence call logs and swallows,
the same posture `llm/activity.py` takes for its own recording: a telemetry bug
cannot be allowed to kill the thing it is observing.

## Read path

**`_state` stays purely live.** It continues to mean "this session", and the
runner is not taught to hydrate itself from storage — a stored result must
never be mistaken for a running one.

A new `/benchmark_<page>/history` endpoint returns, per cell, the latest result
plus the retained lists. The page fetches it once on load and again whenever
`running` goes true → false.

**It is deliberately not on `/state`.** That endpoint is polled once a second;
putting 3+6 entries per cell on it would put the entire history on the wire
every second for as long as the page is open. This follows the precedent
already set for story artifacts, which are held off the polled state for the
same reason and fetched on demand.

Merging in the page's `render()`: a cell whose live status is `pending` renders
its stored latest instead, styled as historic. Once the runner has a real entry
for that cell, the live value wins.

## Hover card

Rendered into the cell and revealed by CSS on `td.bench:hover` — `td.bench` is
already `position:relative`, so no JS positioning is needed.

```
┌─ base64_decode · gemma4:e4b ────────────────────┐
│  2026-08-23 00:41   5/5   ✓5 ✗0 !0    2.1s/tr  │
│  2026-08-21 20:35   5/5   ✓4 ✗1 !0    2.4s/tr  │
│  2026-08-18 02:44   5/5   ✓5 ✗0 !0    2.0s/tr  │
│ ── partial ─────────────────────────────────── │
│  2026-08-22 02:40   2/5   ✓2 ✗0 !0    stopped  │
│  2026-08-17 21:03   0/5               error    │
│                                                 │
│  ⚠ model arguments changed since 2026-08-21    │
└─────────────────────────────────────────────────┘
```

A cell with no stored history shows no card at all, rather than an empty box.

## Out of scope

- `/benchmark_editdocument`, which has its own runner and views.
- Cross-model comparison — a "best so far" column, or ranking a new model
  against a previous champion. A genuinely new model has a new override uuid
  and therefore no history of its own; what this design gives is that every
  *other* row keeps its numbers across restarts, so the new model lands in a
  table that still shows what everything else scored. Ranking across rows is a
  separate feature.
- Storing per-trial artifacts (story markdown, transcripts). Those are large,
  already held off the polled state, and are not what a baseline comparison
  reads.
- Editing or hand-annotating stored results.

## Testing

- **Retention**: writing 5 complete results for one cell leaves the newest 3;
  interleaved partials do not evict completes, and vice versa.
- **Keyed by name**: reordering a spec set does not move a cell's history.
- **Terminal states**: `done` with all trials → complete; `error` → partial;
  abort mid-cell with trials done → partial `stopped`; abort with zero trials
  → nothing written.
- **Isolation**: a raising persistence layer does not abort a run.
- **Deleted target**: history for a removed override still renders its label.
- **Read path**: `/history` returns the retained lists; `/state` does not carry
  them.
- **Merge**: a pending cell with stored history renders the stored numbers
  marked historic; a live result outranks a stored one.
