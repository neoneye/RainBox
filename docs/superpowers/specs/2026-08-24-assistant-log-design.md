# The assistant page as a log

## Problem

`/assistant` is built around `assistant_step`. That table is one row playing
four roles at once — a model call (prompts, tokens, reasoning, retries), an
action dispatch (action, args, reason), an observation (result, error), and a
container for sub-events buried in JSON (`observation.data.timing.phases`,
`.timing.embeddings.calls`, `log`). Twenty-six columns.

The page mirrors that shape, so everything a run did is rendered inside a
"Step N of 6" block, and anything without a step row of its own has nowhere to
go. Observed on run `ea7c1661`: a `claim retrieval` of 10.3s appears only as a
line in a phase table, with nothing to select and nothing to read.

The cost is structural rather than cosmetic. Each new thing worth showing needs
new markup inside the step template, so the template grows and the page scales
with the number of *kinds of thing* rather than staying fixed.

Meanwhile the richest record already exists somewhere else. `llm_call` holds
every call's prompts, tokens, prefill/decode split and cache-reuse metrics —
strictly more than `assistant_step` carries — but nothing ties a row to a run,
so the assistant page cannot reach it and the two surfaces disagree about the
same call.

## Goal

Render a run as a flat stream of typed events, with one component per type.

- **Gantt on top** for spotting an anomaly at a glance.
- **Split view below** — event list on the left, detail on the right.
- Both are renderings of one event list, so a new event type costs one
  component and appears in both.

## Scope

This is the first of two slices, and the boundary is deliberate.

**In:** the derived read model, the `llm_call` run linkage, and the reworked
page.

**Out:** instrumenting the interiors that are currently opaque. `claim
retrieval` becomes a selectable row whose detail pane shows a duration and
nothing else, because nothing measures inside it — that run recorded no embed
calls and no sub-timings. Making that pane say "vector 9.7s, fulltext 0.4s" is
work in the retrieval stack, and it belongs in the second slice, informed by
which rows this one shows as empty.

Stating it plainly: **the case that prompted this work is not fixed by this
work.** What this slice buys is that the case becomes a first-class row with a
place to put the answer, instead of a line in a table nobody can click.

## The read model

New module `db/assistant_log.py`, one entry point:

```python
run_events(run, steps, reviews=None) -> list[RunEvent]
```

`RunEvent` carries `uuid, kind, label, started_at, duration_ms, anchor, kpis,
payload`. Kinds:

| kind | derived from |
|---|---|
| `llm` | a step's own call, its `rejected_attempts`, `_inner_calls`, second-opinion reviews |
| `embedding` | `observation.data.timing.embeddings.calls` |
| `action` | a step's `action`/`args`/`observation` |
| `activity` | `timing.phases`, minus the calls inside them |
| `control` | steps with `phase == "control"` |
| `unaccounted` | synthesized from the gaps |

**Nothing is written differently, so every historical run renders at once.**
That is the reason for deriving rather than emitting: a new event table would
leave every existing run blank until backfilled, and would put a writer change
through the whole loop before a single screen could be seen.

One step becomes **two events** — the model call that chose an action, and the
action itself. That split is the point. Today a request and its result are the
same row as the call that produced them, which is why there is nowhere to
render either properly.

The interval arithmetic already written for the timeline (`_subtract`,
`_activity_rows`, `_unaccounted_rows`) moves here unchanged. It is what
guarantees no event contains another, so the gantt stays a staircase.

`assistant_llm_calls` stays as the LLM-only projection that
`assistant_run_stats` and the in-chat progress row consume, implemented as a
filter over `run_events`. Two enumerations of one run that could disagree is
the bug this whole change is against.

## The `llm_call` run linkage

`llm_call` gains two nullable columns, `run_uuid` and `step_uuid`, set from the
instrumentation tags that already carry `caller`. Populated going forward;
older rows keep whatever they have.

This is the only writer change in the slice, and it is what lets the `llm`
component show prefill vs decode and cache reuse — the numbers that explain a
16-second call. Without it the log can only repeat what the step row stores:
total duration, in/out tokens, model.

An event joins its `llm_call` row when one exists and degrades to the step's
own fields when it does not, so a run predating the linkage still renders.

## Components

One renderer per kind. Actions are the interesting case: there are 32 of them,
and a component each would move the scaling problem rather than solve it.

| kind | KPIs | tabs |
|---|---|---|
| `llm` | model, in/out tokens, prefill/decode, cached, tok/s | prompt · response · reasoning · rejected |
| `embedding` | model, texts, chars, ms | texts |
| `action` (generic) | action, status, duration | args · result · raw |
| `action:python_run` | exit, stdout bytes, duration | code · stdout · raw |
| `action:memory_query` | candidates, kept, truncated | candidates · result · raw |
| `activity` | duration | — |
| `control` | command, operator | payload |
| `unaccounted` | duration | — |

The generic action renderer is the one implemented once: a new action needs no
code and still looks right. A bespoke renderer is a promotion earned by a
payload that genuinely differs, not a requirement.

Detail panes are tabbed so a 50k-token prompt cannot push the KPIs off screen.

## Page structure

Three bands:

1. **Dashboard** — a thin strip: status, events, total, model, other, tokens.
2. **Gantt** — one bar per event, contiguous, the anomaly visible by width.
3. **Split view** — list left, detail right; selecting in either drives both.

The "Step N of 6" sections are removed. Their content is not lost: prompts,
response, reasoning and log become the `llm` component's tabs; args and
observation become the `action` component.

The Markdown export renders from the same event list, so the page and the
export cannot disagree about a run's structure.

## Files

`webapp/assistant_views.py` is 1980 lines, which is what the problem looks like
from the inside. It splits by responsibility:

- `webapp/assistant_views.py` — routes, page shell, dashboard
- `webapp/assistant_log_view.py` — events to view-model, gantt, list
- `webapp/assistant_components.py` — one renderer per kind
- `db/assistant_log.py` — the read model

## Testing

- One step yields both an `llm` event and an `action` event.
- No event contains another (the staircase property, already tested; it moves).
- An `llm` event joins its `llm_call` row for prefill/decode/cached, and falls
  back to the step's fields when no row exists.
- A run recorded before the linkage still renders every event.
- Every action renders through the generic component; `python_run` and
  `memory_query` render through their own.
- An action with no bespoke renderer needs no code to appear.
- `assistant_run_stats` totals are unchanged by the rework.
- The Markdown export lists the same events in the same order as the page.

## Out of scope

- Instrumenting retrieval internals (the second slice).
- A new `assistant_event` table. Deriving covers this slice; if the second
  slice needs events no step implies, that is when a table earns its place.
- Changing what the loop records, beyond the two `llm_call` columns.
