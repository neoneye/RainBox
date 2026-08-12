# Second-opinion review records — making the gate's judgment inspectable

**Status:** Implemented — all three rollout steps. Nothing is backfilled, so
the queries below see only reviews written from 2026-07-28 onward. Current
behaviour lives in `notes/second-opinion-design.md`; this document is the
design rationale.
**Date:** 2026-07-28
**Last updated:** 2026-07-28

## The Problem

Two questions the operator wants to ask months after the fact:

1. **Why did it go wrong?** A run produced a bad answer. Did the gate see the
   problem and get overruled by a retry? Did it approve the bad program? Did it
   never run at all?
2. **Why did it provide the right answer but for the wrong reasons?** This is
   the case the gate was *built* for — `second-opinion-design.md` names it in
   its opening paragraph: a metric-country operator asking to convert feet, and
   the deciding model reasoning about it as a generic US-units question,
   reaching the right answer by accident.

Neither question is answerable from what is stored today. Not "hard to query" —
not answerable.

## Why today's storage can't answer them

The review payload lives in `observation.data["second_opinion"]` — a JSON blob
nested inside the `observation` JSONB column of the `assistant_step` row.

The core asymmetry: **the decide call that proposes a program gets first-class
columns** on `assistant_step` — `system_prompt`, `user_prompt`, `reasoning`,
`model_response`, `input_tokens`, `output_tokens`, `duration_ms`,
`requested_at`, plus three indexes. **The review that gates it gets a blob
inside another column's payload.** They are the same kind of event — one
structured LLM call with a prompt, a reasoning channel, and a parsed result —
stored at two very different tiers.

Six concrete consequences:

**1. Unqueryable.** "Every rejection in the last month" is a sequential scan of
`assistant_step` plus a JSON dig, with no index and no schema. There is no
`WHERE verdict = 'rejected'` to write.

**2. The fail-open cases are encoded as absence.** `_second_opinion` returns
`{"skipped": "no_model_group"}` or `{"error": …}` — these carry no `approved`
key at all, while a real approval carries `approved: true`. Downstream, all
three mean "the action ran". So a run that went wrong *because the gate never
ran* is indistinguishable from one the gate actively approved. For question 1
that distinction is the whole answer.

**3. Problems are free text, so the founding failure class is invisible in
aggregate.** `SECOND_OPINION_SYSTEM_PROMPT` enumerates five rejection grounds,
one of which is precisely the identity/profile mismatch that motivates the
whole feature. The verdict records only `problems: list[str]`. You cannot count
how often that ground fires, or whether it is getting better.

**4. Approved-with-problems is stored identically to a clean approval.** The
design doc explicitly supports this state: "Approved with a non-empty `problems`
list → the program runs; the problems stay in the trace as advisory notes."
That state *is* the right-answer-wrong-reasons signal — the reviewer saw
something and let it through. Nothing aggregates it.

**5. No aftermath link.** A rejection's meaning depends entirely on what
happened next: did the revision get approved, did the run still resolve, or did
the gate just burn one of the assistant's few steps? Those live in sibling
`assistant_step` rows with no relation to the review.

**6. No operator judgment.** There is nowhere to record "this rejection was
wrong" or "this approval was a miss". Without that the gate's own precision is
unmeasurable, and the rejection bar in the prompt can only ever be tuned by
vibe.

### A bug found while writing this

`_run_dashboard` sums `input_tokens` / `output_tokens` / `duration_ms` from
`assistant_step` rows only. The review's model call is a real structured LLM
call whose cost is captured nowhere — so **the /assistant dashboard
under-reports tokens and model time for every gated run**. The gate's running
cost is currently invisible.

## The proposal

### 1. Promote the review to a first-class row

```text
second_opinion_review
- id, uuid
- run_uuid            → assistant_run.uuid   (indexed)
- step_uuid           → assistant_step.uuid  (nullable; the gated step)
- step_index          int    the attempt's step index, mirroring assistant_step
- journal_id, room_uuid, agent_uuid          provenance trio, as retrieval_event
- action              text   the gated action ('python_run')
- verdict             text   approved | rejected | skipped | error
- skip_reason         text   'no_model_group' etc; NULL unless verdict='skipped'
- error               text   NULL unless verdict='error'
- problems            JSONB  [{category, text}]
- categories          text[] denormalized distinct categories, for indexing
- group_from          text   second_opinion | own
- model_uuid          uuid
- system_prompt, user_prompt, reasoning, response   text
- input_tokens, output_tokens, duration_ms, requested_at
- created_at
```

The column vocabulary deliberately mirrors `assistant_step` — same names, same
nullability reasoning — because it is the same kind of event. That also makes
the token/time columns available to fix the dashboard under-reporting above.

Indexes: `(run_uuid, step_index, id)` for the per-run trace,
`(verdict, created_at)` for the overview, and a GIN index on `categories`.

### 2. `verdict` as a four-value enum, not a bool

Today `approved: bool` plus optional `skipped` / `error` keys. Replacing that
with one `verdict` column means "how often is the gate actually gating?" is a
single `GROUP BY` — and it stops a skipped review from reading as an approval.
`CHECK (verdict IN ('approved','rejected','skipped','error'))`, matching how
`assistant_step.phase` and `cron_job.action_type` are already constrained.

### 3. Categorize the problems

`SecondOpinionVerdict.problems` becomes a list of objects:

```python
class SecondOpinionProblem(BaseModel):
    category: Literal[
        "not_asked",           # doesn't answer what the operator asked
        "identity_mismatch",   # contradicts operator profile: units, locale,
                               # language, currency, timezone, date format
        "logic_error",         # wrong formula/constant, off-by-one, rounding
        "sandbox_infeasible",  # needs network, files, or non-allowed packages
        "reason_mismatch",     # stated reason misrepresents the program
        "other",
    ]
    text: str
```

These are not new concepts to teach: they are the five grounds
`SECOND_OPINION_SYSTEM_PROMPT` already sets as the rejection bar, plus an
escape hatch. The model is being asked to name the ground it already reasoned
from, so the added prompt burden is near zero. Because the categories are a
`Literal` in structured output rather than illustrative prose, they cannot be
parroted into the reply the way example phrasing can.

`other` is included deliberately — a closed set with no escape hatch forces
miscategorization, which is worse than an honest bucket. A rising `other` share
is the signal that the category set needs another member.

`identity_mismatch` is the one that answers question 2. It is also the operator's
standing complaint per `2026-07-24-operator-locale-and-language.md` ("Making the
LLM understand that I'm not an american, that's my struggle"), so this column
turns that struggle into a number that can be tracked.

### 4. Derive the retry chain — don't denormalize it

A rejected step is re-attempted at the *same* `step_index`; run `35579b20` has
three rows at `#0`. So `(run_uuid, step_index)` grouped and ordered by
`created_at` already gives the attempt chain. No `attempt_index`, no
`superseded_by` pointer, no back-reference to maintain. This follows
`relevance-telemetry.md`'s standing rule that counters and rollups are derived,
never primary.

### 5. The operator's assessment is a separate, append-only judgment

```text
second_opinion_assessment
- id, uuid
- review_uuid   → second_opinion_review.uuid  (indexed)
- assessment    text   agree | over_blocked | under_blocked | unsure
- note          text   the operator's own words — why it went wrong
- created_at
```

- `over_blocked` — rejected something that was fine; cost a step for nothing.
- `under_blocked` — approved something that should have been stopped. **This is
  the right-answer-wrong-reasons miss.**

A separate table rather than mutable columns on the review, because the review
row is a record of what the model said at a point in time and must not be
edited after the fact — the same append-only discipline `relevance-telemetry.md`
sets for `retrieval_event`. Changing your mind appends a row; latest wins.

`note` is the part that actually serves "why did it go wrong" — the aggregate
counts tell you *where* to look, the note tells future-you *what you concluded*.

### 6. `observation.data` keeps a pointer, not a copy

The new table becomes the source of truth and
`observation.data["second_opinion"]` shrinks to `{"review_uuid": "…"}`. The
inspector loads reviews once per run by `run_uuid` in `_load_run_detail`,
exactly as it already does for steps and write-intents, and
`_split_second_opinion` resolves the pointer.

Historical rows keep their inline payload and have no `review_uuid`, so the
view falls back to the inline blob when the pointer is absent. No backfill, no
data loss, no permanent duplication.

## What becomes answerable

Question 2 — right answer, wrong reasons — becomes one query:

```sql
SELECT r.run_uuid, r.created_at, p->>'text'
FROM second_opinion_review r, jsonb_array_elements(r.problems) p
WHERE r.verdict = 'approved'
  AND p->>'category' = 'identity_mismatch';
```

The gate let it through, having noticed the exact thing the gate exists to
catch. Today: invisible.

Question 1 — why did it go wrong — becomes a join against the run's outcome:

```sql
SELECT r.verdict, r.categories, a.summary->>'outcome'
FROM second_opinion_review r
JOIN assistant_run a ON a.uuid = r.run_uuid
WHERE a.summary->>'outcome' <> 'resolved';
```

Split by `verdict`, that separates "the gate approved a bad program" from "the
gate never ran" from "the gate blocked and the run never recovered" — three
different bugs that are one undifferentiated blob today.

Further rollups the schema enables: rejection rate by category over time; share
of rejections whose next attempt was approved (gate working) versus rejected
again (gate thrashing); `skipped`/`error` rate (how often the gate isn't
gating); `over_blocked` rate from assessments (is the bar too tight); and the
gate's token/time cost per run.

## Explicitly not: `retrieval_event`

It is the obvious place to reach for and it is wrong. Its `target_type` CHECK
is `('qa_entry','memory_claim','skill')` and its `stage` set is retrieval-shaped;
a review has no retrieval target. `relevance-telemetry.md` already set this
precedent in its "Not Here: Suppressed Re-assertions" section, declining to
shoehorn a different event shape into that table. Same reasoning applies.

## Rollout

Additive and reversible, in three independent steps:

1. **Table + dual write.** `create_all()` makes both tables;
   `_add_column_if_missing` is not needed since nothing existing changes.
   `_second_opinion` writes the row and returns `{"review_uuid": …}` alongside
   today's payload. Nothing reads the table yet — safe to ship alone.
2. **Categories.** Change `SecondOpinionVerdict` and the prompt. Independent of
   step 1; the `problems` column accepts both shapes during the transition
   (a bare string normalizes to `{category: "other", text: …}`).
3. **Read paths.** Inspector reads by pointer with inline fallback; dashboard
   adds the review's tokens/time; a `/second-opinion` overview page with
   verdict/category/date filters and the inline assessment control, following
   `/assistant-overview`'s existing chip-and-filter shape.

## Testing

Mirroring `test_assistant_second_opinion.py`'s existing seams — the review call
is monkeypatched, the sandbox is a recording fake, so all of this stays
deterministic:

- a row lands per gated step, with the right `verdict` for each of approved /
  rejected / skipped / error (the four-way split is the point of the change);
- the fail-open paths write `skipped` / `error` rows rather than nothing;
- categories survive the round trip; a bare-string problem normalizes to
  `other`;
- the retry chain derives correctly for a run with three attempts at one
  `step_index`;
- the inspector renders from the pointer, and still renders a legacy inline
  payload with no `review_uuid`;
- an assessment attaches to a review and the newest one wins.

## Deliberately out of scope

- **Backfilling history.** Existing runs keep their inline payload and stay
  readable in the trace; they will not appear in aggregate queries. Backfill is
  possible later from `observation.data` but the categories would have to be
  invented, which would poison the very counts the table exists to produce.
- **Widening the gated surface** beyond `python_run`. That is a one-flag
  registry change and an orthogonal decision.
- **Auto-acting on the counts.** Per `relevance-telemetry.md`: telemetry says
  where to inspect; evals say whether a change helped. Nothing here should
  automatically retune the rejection bar.

## Open question

Whether the assessment should be operator-only, or whether a later model pass
can propose assessments for the operator to confirm. Model-proposed assessments
would scale the labelling, but a model judging another model's judgment of a
third model's program is a long chain to trust — and the assessments are meant
to be ground truth for tuning the gate. Recommend operator-only for the first
cut; revisit once there is enough volume that manual labelling is the
bottleneck.
