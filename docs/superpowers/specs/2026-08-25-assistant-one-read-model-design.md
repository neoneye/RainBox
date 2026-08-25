# One read model for the assistant run

## Problem

`/assistant` renders a run as a stream of typed events. `db/assistant_log.py`
derives that stream, `webapp/assistant_components.py` renders one component per
kind, and `webapp/assistant_log_view.py` lays the same events out as a gantt and
a list. That part works and is not what this is about.

Two things behind it do not.

**The markdown export is a second read model of the same run.** `_run_markdown`
walks `assistant_step` rows directly and rebuilds — from scratch — the meta
lines, the prompt blocks, the review rendering, the phase timing table and the
write-intent listing that the components already produce from events. Twenty-
six functions and roughly 700 lines of `webapp/assistant_views.py` exist for
no other reason. Two enumerations of one run that could disagree is the bug the
event stream was built against, and the export is where it survived.

The page no longer reads most of what feeds it. Of the context
`assistant_page()` passes the template, sixteen keys are referenced zero times:

```
decision_json  exchanges  step_kinds  duplicate_result  second_opinion
obs_data  unlinked  model_names  action_descriptions  code_driven_descriptions
response_meta  request_meta  call_meta  result_meta  review_meta  rejected_meta
```

`timeline` survives only as `timeline|length + 1`, for the live row's step
number. `timing` and `waterfall` appear only in CSS class names and a comment.
`_waterfall` itself has no production caller at all. Every request still builds
all of it, including `get_run_trigger_message` twice.

**A call is joined to its step by guessing.** `llm_call` holds the prefill /
decode split and the cache-reuse numbers — strictly more than `assistant_step`
carries — and `_attach_llm_call_kpis` attaches them by walking every row of the
run and taking the one whose `started_at` is nearest the event's, within a five
second tolerance, consuming matches greedily. On a step that retried, the
rejected attempt and the attempt that replaced it sit well inside that
tolerance of each other, so which row lands on which event is decided by
arithmetic rather than by record.

The design this page was built from
(`2026-08-24-assistant-log-design.md`) specified the fix: *"`llm_call` gains two
nullable columns, `run_uuid` and `step_uuid`."* Only `run_uuid` shipped. The
docstrings on `LlmCall.run_uuid` and `StructuredLLMAgent._instrument_tags` both
still claim the row carries the step.

It could not ship, and the reason is structural: **the step row is written after
the call it describes.** `_open_step` takes `usage`, `model_response`,
`requested_at` and `rejected_attempts` as arguments — the row does not exist
while the call is in flight, so there is no step uuid to tag it with. That one
ordering fact is also why:

| Consequence | Where |
|---|---|
| Steps are re-sorted by clock, because commit order is not causal order | `db/assistant.py:assistant_trace_steps` |
| A start is back-computed as `created_at − duration_ms` | `db/assistant_log.py:step_started_at` |
| An in-flight call has no row, only `run.metadata_["active_call"]` | `db/assistant.py:checkpoint_assistant_call` |

## Goal

One read model, two serializations. The export becomes markdown rendered from
`run_events`, and a call's identity becomes a recorded fact rather than a
nearest-neighbour match.

## Scope

**In:** the export rebuilt on events, the component split that lets it be, one
nullable `llm_call` column, and minting a step's uuid before its call.

**Out:** changing how the loop records phases, embeddings or inline reviews.
Those live in `observation.data` JSONB and are derived from there. Deriving is
the contract — it is why every run that has already happened renders — and
nothing here relaxes it.

**Also out: the `'planned'` phase.** It is in the check constraint and in
`StepPhase`, and nothing writes it. Removing it means dropping and re-adding
the constraint, which Postgres validates against existing rows — a single
legacy `planned` row would fail the migration at startup, in every process.
The value stays. `StepPhase` may drop it, since that type governs writes only.

## Part 1 — the export reads events

### The split

Every component today is built from `_block(title, body)` returning `Markup`,
plus a few `<p class="ev-note">` notes and two link widgets. The content and
its HTML are decided in the same function, which is why a second surface has
to redo the content.

Split them. A renderer returns blocks; a serializer turns blocks into a
surface.

```python
#: One labelled span of a pane. `body` is text or a JSON-able value; `note`
#: marks the prose a kind writes when there is nothing recorded to show, which
#: reads as a paragraph rather than as a labelled block.
Block = dict  # {"title": str, "body": Any, "collapsed": bool, "note": bool}


def event_blocks(event: dict) -> list[Block]:
    """The content of one event's detail pane, before any surface renders it.

    Dispatches exactly as `render_event_detail` does — on kind, on the action
    name, and on the `review` variant — so the two surfaces cannot diverge on
    which renderer an event gets.
    """
```

`render_event_detail` keeps its signature and becomes the HTML serializer over
`event_blocks`, plus the HTML-only extras: the sender and room links on a
`start`, and the confirm / reject / undo controls on an action's intents. Those
are interactive, and markdown has no business with them.

`event_kpis` needs no change. It already returns surface-neutral dicts —
`{label, text, title, href, html}` — which is precisely the meta-line builder
the export currently duplicates in `_field`, `_model_field`, `_time_field`,
`_usage_fields` and the five `_*_meta` functions. Consuming it closes the
follow-up left by the step-section removal.

### The export

```python
def _event_md(event: dict) -> list[str]:
    """One event as markdown: heading, meta line, then its blocks."""
    out = [f"### {event['label']}"
           + (f" — {d}" if (d := event_description(event)) else ""), ""]
    if step_ref := event.get("step_ref"):
        out += [f"*{step_ref}*", ""]
    if fields := event_kpis(event):
        out += [" · ".join(f["text"] for f in fields), ""]
    for block in event_blocks(event):
        if block["note"]:
            out += [block["body"], ""]
            continue
        out += ([f"**{block['title']}**", ""] if block["title"] else [])
        out += [_fence(_as_text(block["body"])), ""]
    return out
```

`_run_markdown`'s "## Timeline" section becomes a loop over
`ctx["log"]["events"]` calling this. Its "## Model calls" table already reads
those events and stays as it is — the two now agree by construction rather than
by test.

Nothing else in the export moves. The dashboard reads `dash`, the summary reads
`run.summary`, the trigger reads the `start` event, pending controls read
`list_pending_controls`, unlinked writes ride on the `start` event's intents
(`_attach_intents` already puts them there), and the verdict reads
`get_run_final_reply`.

### Deleted

Twenty-six functions in `webapp/assistant_views.py`, contiguous at 1050–1699
and 1721–1766, plus `_waterfall`:

```
_hms  _field  _model_field  _time_field  _usage_fields
_response_meta  _exchanges  _rejected_meta  _request_meta  _call_meta
_result_meta  _review_meta  _meta_md  _labelled  _intent_md  _review_payload
_split_second_opinion  _split_timing  _timing_view  _iso_hms
_second_opinion_md  _exchange_md  _step_md  _step_kinds  _same_payload
_waterfall
```

`_fence` survives at 1037, as the export's one fencing helper. `_waterfall` is
already dead and goes with the rest.

`_load_run_detail` drops to what both surfaces read: `dash`, `log`,
`pending_controls`, `trigger`, `reply`, `verdict`, `active_call`. The
`get_run_trigger_message` call collapses from two to one. `assistant_page`
passes nine names instead of twenty-eight — `selected`, `trigger`, `duration`,
`dash`, `log`, `pending_controls`, `reply`, `verdict`, `active_call`.

The template's one use of `timeline` — the live row's step number — reads
`log.events` instead, counting the rows that carry a step.

### The tests

They are the reason the earlier removal attempt cost 39 of them. Each test
asserting export markup moves to assert the same fact about the event it now
comes from; none is deleted for being inconvenient. `_waterfall`'s two tests go
with the function, since what they pin is now pinned on `run_events`.

## Part 2 — a call knows its step

### Mint the uuid before the call

The loop already stamps `requested_at` immediately before the decide call
(`agents/assistant.py`, the `for step_index in range(self.step_limit)` head).
Mint the step's uuid at the same point, and pass it through to the insert:

```python
requested_at = datetime.now(UTC)
# The row lands after the call returns, so a call in flight has no step to be
# tagged with — which is what left llm_call joined to its event by nearest
# start time. The uuid is minted here instead and used as the row's own when
# it is written, so the tag is a record rather than a match.
with self._logging_step(uuid4()) as step_uuid:
    decision = self._decide_next_step(...)
```

`_logging_step` is a context manager setting and clearing `_log_step_uuid`.
Clearing matters: a call made outside a step — the run summarizer, an embed
inside an action — must not inherit a stale uuid, and a `finally` is what
guarantees that after a call raises.

`open_assistant_step`, `append_assistant_step` and `_record_step` take an
optional `uuid`, defaulting to `uuid4()` as now. Every code-driven call site
that already stamps its own `requested_at` (the reply audit, the criteria
revision, the language classifier) takes the same two lines.

### Carry it

`_instrument_tags` gains the tag beside the three it already sets, and its
docstring stops claiming a step it did not carry:

```python
step_uuid = getattr(self, "_log_step_uuid", None)
if step_uuid:
    tags["step_uuid"] = str(step_uuid)
```

`llm/activity.py` reads it in `_on_start` and `_on_embedding_start` alongside
`run_uuid`, and writes it in the `_on_end` and `_on_failure` rows.

### Store it

```python
# llm_call gained the assistant step as well as the run, so a call joins the
# event it belongs to by record rather than by nearest start time — which two
# attempts of one retried step sit inside the tolerance of.
_add_column_if_missing("llm_call", "step_uuid", "step_uuid UUID")
```

On the model, beside `run_uuid`, plain column and no FK for the reason
`model_uuid` gives: a call's record outlives the row it points at.

### Join on it

`_attach_llm_call_kpis` keeps its one query per run and its shape. Rows with a
`step_uuid` index by it; the time-nearest walk stays for everything else, which
is every call recorded before this and every event with no step of its own. Its
docstring says which of the two an event took, because "matched by time" and
"matched by record" are different claims about the same numbers.

## What this does not fix

`assistant_trace_steps` still re-sorts by clock, `step_started_at` still
back-computes a legacy start, and the live call is still a JSONB checkpoint.
All three follow from the row being written after its call, and all three would
be closed by inserting the row *before* the call instead of only minting its
uuid. That is a change to the loop's durability story — what a crash between
the insert and the response leaves behind — and it earns its own design rather
than riding in on this one.

Minting the uuid is the part that costs nothing and unblocks the join. The rest
waits until there is a reason beyond tidiness.
