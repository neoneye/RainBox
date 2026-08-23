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

1. **Phases become activity bars**, contributing the time their calls do not
   occupy, so nothing is drawn over anything else.
2. **Whatever is still uncovered becomes an `unaccounted` row**, computed from
   the gaps rather than instrumented.

The second is what makes the property hold permanently. Instrumenting more
actions narrows the unaccounted bars; it is never required to keep the timeline
honest, and the bars themselves say where instrumenting would pay.

## Rows

Every row is a **leaf**: one thing that spent time. No row contains another.

A bar drawn over other bars hides them, and hides any stall between them —
which is the failure the page exists to expose, so a container bar reintroduces
it one level down. `recall filter` as a 22.8s span over a 12.7s call is not an
observation; it is two facts wearing one bar.

`db.assistant_llm_calls` therefore emits:

- the existing model calls (`decide`, `code-driven`, `inner`, `review`,
  `rejected`) and `embedding` calls, unchanged
- **`activity`** — a phase minus the calls made inside it
- **`unaccounted`** — synthesized, never stored

### Phases become activities by subtraction

A recorded phase is wall-clock that *overlaps* the calls made during it. What
it contributes on its own is the remainder:

```
recall filter   |—————————————————————————|   22.8s recorded
recall_filter             |———————————————|   12.7s call
activity        |—————————|                   10.0s  ← the bar drawn
```

A phase that made no calls yields one bar its own length — `claim retrieval`
is 10.4s of work and nothing needs subtracting. A phase wrapping a call yields
the slices around it, which is how ten seconds of model loading become a named
bar instead of vanishing under the call that followed.

Leftovers below `MIN_ACTIVITY_MS` (100) are rounding, not activity, and are
dropped.

### Unaccounted is now one thing

With every row a leaf, the complement is taken once over the whole run rather
than per level, and it means exactly one thing: **time nothing measured**.

That is what makes a gap worth drawing. The waterfall is meant to be
continuous — one activity ending where the next begins — so a break in it is a
real hole in the instrumentation and nothing else. Gaps below
`UNACCOUNTED_MIN_MS` (1000) are not drawn; sub-second jitter between adjacent
calls is not a finding.

An unaccounted row stays unlabelled beyond its duration. It is the absence of
evidence; naming it "model load" would be a guess printed as a fact.

## What must not change

**The dashboard totals.** `assistant_run_stats` derives `calls`, tokens,
`duration_ms` and `tps` from this same enumeration. `activity` is an action's
own seconds and `unaccounted` is the absence of a call — counting either would
inflate the call count and its seconds. They are excluded there alongside
`embedding`, which is already excluded for its own reason (it produces no
tokens, so its seconds would drag throughput down against work it never did).

The summary line stays `N calls · model Xs · total Ys`, and its `N` keeps
meaning model calls.

**The card title** becomes "Timeline". It no longer shows only model calls, and
leaving it as "Model calls" while drawing an action's own work would
misdescribe it.

## Rendering

The existing markup already applies `kind-{{ c.kind }}` to the label and the
bar, so the only addition is `unaccounted`: a hatched amber bar and an italic
name, visibly not a measurement of anything.

`activity` is ordinary measured work and is styled like a call — neutral.
Colour stays reserved for rows a reader should stop on, which is `rejected`
and `unaccounted`.

There is no indentation, because there is no nesting left to show.

## Out of scope

- Adding `_PhaseTimer` to other actions. The unaccounted rows are what should
  decide which ones deserve it, and that is a follow-up informed by real
  numbers rather than a guess made now.
- Attributing an unaccounted gap to a cause (model load, DB, GC).
- Changing what `_PhaseTimer` records or how `memory_query` phases are named.

## Testing

- **No row contains another** — asserted over every pair, since this is the
  property the layout rests on.
- A phase wrapping a call contributes only the remainder; the phase's own
  length appears nowhere.
- A phase with no calls inside it is one whole bar.
- An embedding call gets its own bar, and the phase around it shrinks by
  exactly that much.
- With everything measured the rows tile their span continuously and no
  `unaccounted` row is emitted at all.
- A stretch neither a call nor a phase covers does become one; one below the
  threshold does not.
- `assistant_run_stats` call count, tokens and duration are unchanged by the
  presence of activity and unaccounted rows.
- A run with no phases recorded is unchanged apart from gaps.
