# /activity — LLM activity dashboard

An operator-facing page at `/activity` that answers "is the prompt cache
working, and where is it not?" for every LLM call rainbox makes. Named for
the general case: cache is the first KPI, not the only one the page will
ever hold.

## Why this needs new plumbing

rainbox records token counts only for assistant steps (`assistant_step.
input_tokens` / `output_tokens` / `duration_ms`). Chat, cron, kanban,
benchmark and eval calls leave no per-call record at all, and **nothing
anywhere records cache behaviour** — `cache_read`, `cached_tokens`,
`prompt_cache` and `prompt_eval_count` appear nowhere in the tree.

So the dashboard cannot be a query over existing data. Capture comes first.

## What the local providers actually report

Measured against the operator's own Ollama, `llama3.2:3b`, a ~4k-token
shared prefix with a varying suffix:

| call | `prompt_eval_count` | prefill |
| --- | --- | --- |
| prefix A, cold | 4032 | 2093 ms |
| prefix A, warm | 4032 | 49 ms |
| prefix B, cold | 3632 | 3453 ms |
| prefix B, warm | 3632 | 49 ms |
| prefix A again, after B | 4032 | 48 ms |

Two findings drive the whole design:

1. **The KV prompt cache is real and large** — roughly 45× faster prefill on
   a hit — and Ollama holds several prefixes at once (A survived an
   intervening B call).
2. **Ollama reports no cache fields.** `prompt_eval_count` is identical on
   hit and miss; the OpenAI-compat `usage` carries only prompt/completion/
   total. The DeepSeek-style `prompt_cache_hit_tokens` /
   `prompt_tokens_details.cached_tokens` fields that a remote API returns
   simply do not exist here.

The only local observable is therefore prefill *duration*, not a token
count. Confirmed reachable through instrumentation: the native
`llama-index-llms-ollama` wrapper puts the full native response on
`event.response.raw`, including `prompt_eval_count` and
`prompt_eval_duration` (cold 2734 ms vs warm 47 ms for an identical 3031
tokens).

## Metrics

### A — measured cached tokens (timing-derived)

Not a binary hit/miss. Partial hits are real: cache half the prefix and
prefill takes half as long, so a boolean would throw away most of the
signal. Estimate continuously, in tokens:

```
cold_rate(model)  = 5th-percentile prefill throughput (tok/s) over that
                    model's most recent 200 calls
cached_est        = clamp(prompt_eval_count
                          - prompt_eval_duration_s * cold_rate(model),
                          0, prompt_eval_count)
```

Below 20 recorded calls for a model, `cold_rate` is not yet meaningful:
A reports "calibrating" rather than a confident wrong number.

Tokens — not a ratio — because that is the unit the stacked chart needs,
and it makes A directly comparable with B.

### B — reusable prefix (deterministic)

What the runtime *could* have cached, computed from rainbox's own outgoing
prompt with no provider cooperation:

- Chop the prompt into 1000-character blocks; store a cumulative hash chain
  (~20 hashes for a 20k-char prompt). Prompt text is never retained — the
  chain is the whole record.
- Compare against the chains of the **last 8 calls to the same model**;
  eight because the probe showed Ollama keeping several prefixes live at
  once, so "compare with the immediately previous call" would under-report.
- `reusable_prefix_tokens = prompt_tokens * matched_blocks * block_size /
  total_chars`.

Character-block granularity, scaled by the reported token count, avoids
needing a per-model tokenizer. It is an approximation of token position,
and is documented on the page as such.

### The gap between A and B is the product

- **B high, A low** — the runtime is evicting the cache. Fix: fewer models
  in rotation, larger cache, less interleaving.
- **B low** — rainbox's own prompt assembly breaks the prefix (a timestamp
  or a shuffled block near the top). Fix: reorder prompt construction.

A hosted dashboard cannot show the second case. rainbox owns the prompt
construction, so it can.

### C — provider-reported, when offered

`prompt_tokens_details.cached_tokens` and `prompt_cache_hit_tokens` are
stored whenever a provider returns them. Always null on Ollama today; a
remote API would populate it, and the page prefers a reported number over
the estimate whenever one exists.

### Not included

**Blended cost ($/1M tokens).** Every model here is local and free — a
column of zeros. The schema leaves room for it if a paid provider is ever
added.

## Storage

New table `llm_call`, one row per LLM call:

| column | type | note |
| --- | --- | --- |
| `uuid` | UUID PK | |
| `started_at` | timestamptz | from the Start event |
| `finished_at` | timestamptz | |
| `provider` | text | `ollama` / `jan` / `lm_studio` |
| `model` | text | |
| `model_uuid` | UUID null | when the caller knows its ModelConfig |
| `caller` | text | from `instrument_tags`; `unknown` if untagged |
| `ok` | bool | |
| `error_category` | text null | PlanExe's `classify_error` vocabulary |
| `prompt_tokens` | int null | |
| `completion_tokens` | int null | |
| `prefill_ms` | int null | `prompt_eval_duration` |
| `decode_ms` | int null | `eval_duration` |
| `total_ms` | int null | wall clock, Start→End |
| `cached_tokens_reported` | int null | metric C |
| `cached_tokens_estimated` | int null | metric A; null while calibrating |
| `reusable_prefix_tokens` | int null | metric B |
| `prefix_chain` | JSONB | metric B's block hashes |

Indexes: `started_at`, `(model, started_at)`, `(caller, started_at)`.
Created by `create_all()` — no ALTER needed for a brand-new table. Needs a
Flask-Admin view, or `test_admin_model_coverage` fails.

Failed calls are recorded too (`ok=false` plus an error category), which
yields a failure-rate KPI on the same page later at no extra cost.

Retention: a prune job on the existing cron drops rows older than 90 days.

## Components

**`llm/activity.py` — the recorder.** One `BaseEventHandler` registered
once at startup, the same shape as the existing `_ReasoningTally`
(`llm/__init__.py:199`) but global rather than context-scoped. Handles
`LLMChatStartEvent` (stamp start time, hash the outgoing prompt) and
`LLMChatEndEvent` (read `raw`, compute A/B/C, insert the row). It reuses
`_ReasoningTally`'s guard that skips the structured/tool wrapper's
reconstructed response, so a structured call is counted once. The whole
body is wrapped: a recording failure must never break an LLM call. Agent
workers already push an app context (`agents/__main__.py:56`), so
`db.session` is available wherever the handler runs.

**Caller tags.** `instrument_tags({"caller": "..."})` around call sites
(verified present in llama_index 0.14.22). Untagged calls record as
`unknown`, so the per-caller panel degrades gracefully while sites are
tagged incrementally.

**`db/activity.py` — queries.** Pure aggregation over `llm_call`: bucketed
series for the chart, per-dimension rollups for the tables, percentiles for
the latency and throughput metrics. No Flask imports, so it is testable
directly.

**`webapp/activity_views.py` — the page.** House style: one module, one
template string, `.pp-activity` classes, server-rendered, no JS framework
and no CDN.

## The page

Modelled on OpenRouter's activity view.

**Controls.** A time-range picker (15m / 30m / 1h / 3h / 24h / 48h / 1w /
1mo / 1y, plus Today / Yesterday / This week / Prev week / This month /
Prev month) and a `<metric> by <dimension>` pair. Plain `<select>`s in a
GET form that submit on change, so state lives in the query string and
every view is a shareable URL. No client-side state machine.

Metrics: Cached Tokens · Cache Hit Rate · Prompt Tokens · Completion
Tokens · Calls · Avg/P50/P90/P99 Latency · Avg/P50/P90 Throughput (tok/s).
Dimensions: Model · Caller · Provider.

**Panels.**

1. **Summary bar** — range picker and the headline cache hit rate.
2. **Prompt token caching** — stacked bars per time bucket, cached vs
   uncached, rendered as inline SVG server-side. Bucket width derives from
   the range (5-minute buckets for 1h, daily for 1mo).
3. **A vs B, and the gap** — measured hit rate against reusable-prefix
   rate, with the interpretation spelled out in words next to the numbers.
4. **Time saved** — `cached_tokens / cold_rate(model)`, summed. Converts
   the hit rate into seconds, which is the number that justifies spending
   an afternoon restructuring prompts.
5. **By model** — calls, hit rate, avg cold vs warm prefill, tokens and
   seconds saved.
6. **By caller** — the same, grouped by tag. The panel that says where to
   go fix something.
7. **Recent calls** — last 50: model, caller, hit/miss, prefix reuse %,
   prefill ms, ok/error.

Panels 2, 5 and 6 honour the metric selector; 1, 3, 4 and 7 are fixed.

Throughout, **cache hit rate** means `sum(cached tokens) / sum(prompt
tokens)` over the selected range — a token-weighted ratio, not the
fraction of calls that hit. A hit on a 20k-token prompt matters more than
one on a 200-token prompt, and the token-weighted form says so.

**Empty and calibrating states.** A fresh install has no rows: the page
says so plainly instead of rendering empty axes. A model under 20 calls
shows "calibrating" in place of metric A, while B — which needs no
calibration — displays from the first call.

**Honesty about the estimate.** OpenRouter is handed `cached_tokens` by
upstream APIs. Here the orange band is timing-derived, and the page labels
it "estimated from prefill timing" wherever it appears. When a provider
reports real numbers, those are charted instead and the label changes.

**Nav.** A top-level `Activity` link in `NAV_TEMPLATE` (`webapp/core.py`).

## Testing

- **Metric maths** (`llm/test_activity_metrics.py`) — pure functions for
  `cold_rate`, `cached_est` clamping, the calibrating threshold, block
  hashing and longest-common-prefix against several candidate chains.
  Table-driven, no DB, no network.
- **The recorder** (`llm/test_activity_recorder.py`) — synthetic
  `LLMChatEndEvent`s carrying a native-Ollama `raw` dict and an
  OpenAI-compat `.choices` object; assert one row per call, the
  structured-wrapper double-count guard holds, a malformed event records
  nothing and raises nothing.
- **Queries** (`db/test_activity.py`) — seeded rows, assert bucketing
  boundaries, percentile correctness, and per-dimension rollups.
- **The page** (`webapp/test_activity_views.py`) — 200 on an empty DB, the
  empty-state copy, every metric/dimension/range combination renders,
  bad query params fall back to defaults rather than erroring, and the
  admin view exists so model coverage passes.
- **Live check** — drive real calls through Ollama and confirm the page
  reports a hit rate consistent with the 45× timing split the probe found.

## Risks

**The estimate is a heuristic.** `cold_rate` from a 5th percentile is
wrong if a model's *only* calls were all cache hits — everything looks
cold, and the hit rate reads near zero. The calibrating gate hides the
worst of it; metric B is unaffected and stays trustworthy, which is why
both are shown side by side rather than blended into one number.

**Recording on the hot path.** One INSERT per LLM call, against calls that
take seconds. Negligible, but the handler is defensive anyway: any
exception is swallowed and logged, never propagated to the caller.

**Prompt hashing cost.** Hashing a 20k-char prompt in 1000-char blocks is
~20 cheap hashes per call. Irrelevant next to inference.
