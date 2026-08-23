# A self-accounting assistant timeline

## Problem

The "Model calls" waterfall on `/assistant` draws one bar per model call. A run
whose wall-clock exceeds the sum of its model calls therefore shows gaps, and a
gap carries no information — there is nothing to click, nothing named, and no
way to tell a model load from a slow database query.

Observed on run `dc017e7a`, 6 calls, model 1m9s, total 1m29s:

```
23:04:12  response_language_classifier   13.9s
23:04:26  acceptance_criteria             5.3s
23:04:32  memory_query (decide)          11.8s
          ................................ 20.2s of nothing
23:05:04  recall_filter (model)          12.7s
23:05:17  reply (decide)                 20.5s
23:05:37  reply_audit                     4.9s
```

Most of that gap is already recorded. `memory_query`'s observation carries a
`timing.phases` block, rendered further down the page as its own table:

```
| claim retrieval | 10.4s | 23:04:43 |
| seed KB load    |  0.0s | 23:04:54 |
| recall filter   | 22.8s | 23:04:54 |
```

`_PhaseTimer`'s docstring already states the intent — phases are recorded with
start times "so the trace can lay them out on the same wall-clock as the model
calls" — but the waterfall never consumed them.

A residue survives even so. The `recall filter` phase runs 23:04:54–23:05:16.8
while its model call runs 23:05:04–23:05:16.7, leaving ~10s inside the phase
that nothing describes. `_PhaseTimer` is wired into `_action_query_memory`
only, so any other action doing non-model work gaps the same way and always
will, for as long as instrumentation is a thing someone has to remember.

## Goal

Make the timeline account for the run's whole wall-clock, so a gap becomes a
measured, labelled row rather than empty space.

Two parts:

1. **Phases become rows**, indented under the step whose action recorded them.
2. **Whatever is still uncovered becomes an `unaccounted` row**, computed from
   the gaps rather than instrumented.

The second is what makes the property hold permanently. Instrumenting more
actions narrows the unaccounted bars; it is never required to keep the timeline
honest, and the bars themselves say where instrumenting would pay.

## Rows

`db.assistant_llm_calls` gains two kinds beyond the existing
`decide` / `code-driven` / `inner` / `review` / `rejected` / `embedding`:

- **`phase`** — one per entry in a step's `timing.phases`, with that phase's
  `started_at` and `ms`.
- **`unaccounted`** — synthesized, never stored.

### Nesting

Depth is assigned by a flat rule rather than a tree walk, because the only
containment that exists is phase-inside-step and call-inside-phase:

- a `phase` row is depth 1 — it is a child of its step's own call row
- any other row whose start falls inside a phase's span is depth 2
- everything else is depth 0

Which reproduces the shape the operator asked for:

```
memory_query (decide)          11.8s
├─ claim retrieval             10.4s
├─ seed KB load                 0.0s
└─ recall filter               22.8s
   ├─ unaccounted              10.0s
   └─ recall_filter (model)    12.7s
```

### Where unaccounted rows come from

Computed per level, not globally. A global complement would find nothing here:
the `recall filter` phase covers its own 10s hole, so the hole only appears
when the phase's children are measured against the phase's span.

- **Top level** — gaps between consecutive depth-0 rows, plus any lead-in from
  `run.started_at` and tail-out to `run.finished_at`.
- **Inside each phase** — gaps between its child rows, bounded by the phase's
  own span.

Gaps shorter than `UNACCOUNTED_MIN_MS` (1000) are not emitted. Sub-second
scheduling jitter between two adjacent calls is not a finding, and a row per
0.1s gap would bury the ones that matter.

An unaccounted row is deliberately unlabelled beyond its duration. It is the
absence of evidence; naming it "model load" would be a guess printed as fact.

## What must not change

**The dashboard totals.** `assistant_run_stats` derives `calls`, tokens,
`duration_ms` and `tps` from this same enumeration. `phase` and `unaccounted`
rows are spans, not calls — counting them would inflate the call count and
double-count seconds already inside a model bar. They are excluded there
alongside `embedding`, which is already excluded for its own reason (it
produces no tokens, so its seconds would drag throughput down against work it
never did).

The summary line stays `N calls · model Xs · total Ys`, and its `N` keeps
meaning model calls.

**The card title** becomes "Timeline". It no longer shows only model calls, and
leaving it as "Model calls" while drawing phases and gaps would misdescribe it.

## Rendering

The existing markup already applies `kind-{{ c.kind }}` to both the label and
the bar, so the two new kinds need only CSS plus an indent:

- `phase` — a lighter, outlined bar, reading as a span that contains things
  rather than as work of its own.
- `unaccounted` — a hatched or muted amber bar, visibly not a measurement of
  anything.
- indentation from `depth` on the label.

The Markdown export gains the same rows, since it exists so "the gaps that show
where the time went survive the export" — which is now literal.

## Out of scope

- Adding `_PhaseTimer` to other actions. The unaccounted rows are what should
  decide which ones deserve it, and that is a follow-up informed by real
  numbers rather than a guess made now.
- Attributing an unaccounted gap to a cause (model load, DB, GC).
- Changing what `_PhaseTimer` records or how `memory_query` phases are named.

## Testing

- A phase in a step's `timing.phases` becomes a row at depth 1.
- A model call starting inside a phase's span is depth 2; one outside is 0.
- A gap above the threshold becomes an `unaccounted` row; one below does not.
- A hole inside a phase, covered globally but not by the phase's children,
  is still emitted.
- `assistant_run_stats` call count, tokens and duration are unchanged by the
  presence of phase and unaccounted rows.
- A run with no phases recorded renders exactly as it does today apart from
  top-level unaccounted rows.
