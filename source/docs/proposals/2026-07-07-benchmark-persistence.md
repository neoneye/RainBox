# Persistent benchmark data, KPI classification, and an embedding suite

**Status: proposal.** Store every benchmark run in Postgres so results
survive restarts and never have to be re-run just to be *seen*; make "I
downloaded a new model" a one-row incremental run against a grid whose other
rows hydrate from the database; make "Ollama updated" a visible *staleness*
signal (model digest changed since the last run) with a re-run-stale button;
derive per-model KPI verdicts (structured output, function calling, speed,
accuracy) from the stored runs instead of trusting provider-claimed flags;
and add a fourth suite that benchmarks **embedding models** for typo
robustness and multilingual retrieval.

## Problem

The three benchmark harnesses (`/benchmark_basic`, `/benchmark_editdocument`,
`/benchmark_kanban`) keep their results in the runner's in-memory `_state`
dict. Consequences, in the order they bite:

- **A restart wipes the grid.** The only way to see results again is to
  re-run every model — minutes of GPU time to recompute numbers that already
  existed.
- **A new model means re-running everything or squinting at a partial
  grid.** The per-row Start button already runs one target, but the other
  rows' results only exist if this process happens to have run them.
- **"Ollama updated — which numbers still hold?"** has no answer. A run
  records nothing about the environment it ran under, so there is no way to
  tell a fresh result from one measured three model-updates ago, and no way
  to re-run *only* what changed.
- **Capability flags are claims, not measurements.** Model-group membership
  constraints resolve against `arguments["is_function_calling_model"]` —
  synced from what the provider *reports*, which routinely disagrees with
  what the model actually does. Meanwhile the benchmark suite measures
  exactly this (tool_order, tool_route) and throws the verdict away.
- **Embedding models are not benchmarked at all**, even though retrieval
  quality (the Q&A store, memory claims) hangs on the embedder, and choosing
  one is currently vibes. The two questions that matter for this instance:
  does retrieval survive small typos, and does a query in another language
  (Danish operator, English corpus) still land?

## Design

### Two tables: `benchmark_run` + `benchmark_result`

Repo conventions throughout (`docs/data-model.md`): uuid identity,
timezone-aware stamps, plain-UUID references validated in code.

```
benchmark_run                 -- one (target × suite) execution
  id, uuid,
  target_uuid                 -- ModelConfigOverride.uuid (chat suites) or
                              --   ModelConfig.uuid (embedding suite)
  suite                       -- 'general' | 'kanban' | 'editdocument' | 'embedding'
  provider, model_name,       -- denormalized identity: history survives an
  target_label                --   override being deleted or renamed
  env jsonb                   -- environment fingerprint, see below
  status                      -- 'running' | 'done' | 'error' | 'killed'
  error text, started_at, finished_at

benchmark_result              -- one bench row within a run
  id, uuid, run_uuid,
  bench                       -- 'base64_decode' | 'tool_route' | 'typo_robustness' | …
  trials int, correct int, errors int,
  elapsed_total float,
  detail jsonb                -- per-trial records (correct/elapsed/error/…)
                              --   for drill-down; aggregates above answer
                              --   every grid/KPI query without unpacking it
```

Writes happen where the truth appears: the runner's `_apply_event` already
maps worker NDJSON events onto `_state`; the same handler upserts the run row
and its results, so the child-process protocol, Stop/SIGKILL flow, and the
polling UI are untouched. A killed or crashed run persists as
`'killed'`/`'error'` — honest history, excluded from hydration and KPIs.

**Latest-done-run-wins.** The grid hydrates from each target's most recent
`status='done'` run for that suite. Older runs are kept — that *is* the
re-benchmark story: after an Ollama update you just run again, and the old
numbers remain queryable under the old fingerprint. No retention cap;
at this scale (tens of models × four suites) pruning is a non-problem.

### The environment fingerprint, and "stale"

`env` is captured at run start:

```
{"provider": "ollama", "provider_version": "0.9.6",     -- /api/version
 "model_digest": "sha256:…",                            -- /api/tags
 "model_size": 815319791}
```

A target is **stale** when the provider's *current* digest for its model
differs from the digest in its latest done run (or the provider version
changed, for providers without per-model digests — LM Studio reports no
digest, so its fingerprint degrades to provider_version + size). This gives
the page three precise run modes instead of one blunt "run everything":

| Button | Runs |
|---|---|
| Run missing | targets with no done run for this suite — the new-model case |
| Run stale | targets whose fingerprint no longer matches — the Ollama-updated case |
| Run all | today's behavior, kept as the escape hatch |

plus the existing per-row Start. The row badge shows which state it's in
(`fresh` / `stale — model updated since last run` / `never benchmarked`).

### KPI classification, derived not declared

A pure function over the latest done runs — no new stored state that can
drift from the measurements:

```
kpi_for_target(target_uuid) -> {
  "structured_output": "pass" | "flaky" | "fail" | "untested",
  "function_calling":  "pass" | "flaky" | "fail" | "untested",
  "speed":    {"mean_trial_s": 3.2, "bucket": "fast" | "medium" | "slow"},
  "accuracy": {"ratio": 0.87,       "bucket": "high" | "medium" | "low"},
}
```

- **structured output** — pass ratio over the structured-output benches
  (kanban `*_struct`; the general suite's format probes). ≥0.9 pass,
  ≥0.5 flaky, else fail.
- **function calling** — same thresholds over `tool_order` + `tool_route` +
  kanban `*_tools`.
- **speed** — mean per-trial wall clock, bucketed **relative to the fleet
  median** (fast < ½×median, slow > 2×median), so buckets stay meaningful as
  hardware changes rather than encoding absolute seconds.
- **accuracy** — correct ratio across the content benches (base64, reverse,
  edit-document score).

Verdicts surface as chips on `/models` and the benchmark grids, explicitly
labeled **measured** next to the provider-**claimed**
`is_function_calling_model` — a disagreement is a finding, not a conflict to
auto-resolve. (Rejected: writing measured verdicts back into
`arguments["is_*_model"]` — that silently changes model-group membership as
a side effect of running a benchmark. If a group should bind to measured
capability, that's a later, explicit opt-in on the group.)

### The embedding suite (`/benchmark_embedding`)

Fourth suite, same shape as the others: same subprocess worker + NDJSON
event protocol + Stop flow, same two tables (`suite='embedding'`), its own
page. Targets are `ModelConfig` rows whose model responds to an embeddings
probe (the sync can tag these once and store `is_embedding_model` in
`arguments`; overrides don't apply — there is nothing to tune).

Everything is computed in-process against a **fixed bilingual corpus**
checked into `benchmarks/embedding.py` (~40 short factual sentences in the
style of the Q&A store, each with: a clean query, 2 typo'd queries, and a
Danish translation of the query). No pgvector, no DB vectors — embed the
corpus once per run, hold the matrix in memory, score with cosine top-1.

| Bench | Measures | Score |
|---|---|---|
| `embed_basic` | does it embed at all: dimension, empty/long input, latency | pass/fail + mean embed seconds |
| `typo_robustness` | top-1 retrieval with 1–2 edit-distance typos in the query | fraction of typo'd queries whose top-1 matches the clean query's top-1 |
| `multilingual` | Danish query against the English corpus | top-1 accuracy vs the clean same-language baseline |

KPIs for embedding targets reuse the same verdict shape: `typo_robustness`
and `multilingual` as pass/flaky/fail ratios, `speed` from `embed_basic`.

The payoff is direct: `EMBED_MODEL_NAME` for the Q&A store is currently
`embeddinggemma:300m` by fiat. With this suite the choice becomes a grid
read, and swapping the winner in is already safe — the `KB_EPOCH` stamp
re-embeds the store automatically on the next sync.

### What deliberately does not change

- The runner/worker/subprocess architecture, the Stop semantics, and the
  polling pages — persistence hooks into `_apply_event`, nothing else moves.
- In-memory `_state` remains the live-progress source while a run is
  executing; the DB is the source between runs. Hydration fills `_state`
  from the DB at page load, so the UI code keeps rendering one dict.
- No scheduler coupling. A "nightly re-run stale benchmarks" cron job
  becomes trivially possible later (the run modes are plain functions), but
  benchmarking stays operator-initiated in this proposal — a benchmark pegs
  the GPU, and the operator knows when that's acceptable.

## Compatibility and edge cases

- **Cold start**: empty tables → every row "never benchmarked"; the pages
  look like today's after a restart, except the state is honest.
- **Override deleted**: its runs keep provider/model_name/target_label, so
  history remains readable; the grid simply no longer shows the row.
- **Same model under two overrides**: two targets, two histories — correct,
  since sampler settings are exactly what overrides vary and results differ.
- **Suite spec changes** (a bench added/renamed): old runs keep old bench
  names; the grid renders the union, missing cells read "not in that run".
  KPI functions only consult benches they know.
- **Concurrent runs**: unchanged — each page owns one runner; per-row runs
  serialize as today.

## Testing

- DB layer: run+results round-trip; latest-done-wins hydration ignores
  error/killed runs; denormalized identity survives override deletion.
- Staleness: fake fingerprints — digest change flags stale, same digest
  stays fresh, missing digest falls back to provider_version.
- KPI classifier: pure-function tests over synthetic run sets covering the
  pass/flaky/fail/untested boundaries and fleet-relative speed buckets.
- Embedding benches: fake `embed_fn` (as in `memory/test_embeddings.py`) —
  typo and multilingual scoring is deterministic given fixed vectors; one
  test per scoring rule, no Ollama.
- Runner integration: feed recorded worker NDJSON through `_apply_event`,
  assert rows land and a SIGKILL mid-run persists `'killed'`.

## See also

- `docs/benchmarks.md` — the three harnesses, worker protocol, Stop flow.
- `docs/llm-providers.md` — provider sync, `/models` test probes,
  `is_function_calling_model`.
- `db/models.py::ModelGroup` — tri-state capability constraints that today
  resolve against claimed flags.
- `docs/qa-system.md` — `KB_EPOCH`; why swapping the embedding model after a
  grid read is already safe.
