# Sealed forecasts — where the pipeline leaks, and which model sees it coming

**Status:** Proposal. Nothing implemented.
**Date:** 2026-07-29

## What this is for

The assistant's prompt is assembled from a dozen sources: a language
classification, acceptance criteria, user settings, a formatting guide, a
knowledge-calibration block, a profile digest, retrieved skills, a transcript,
and one observation per step. When a reply is wrong, nothing in the trace says
**which of those** made it wrong. The operator reads six steps and a bad
message and guesses.

This is an instrument for answering that question. It asks the model to
commit to an answer at each point in the pipeline, seals the commitments, and
compares them — so a wrong reply can be traced to the stage where the answer
first went wrong, and to the block that pushed it there.

It is **not** a per-turn guard. It does not run on every turn, it does not
run in the turn at all, and it changes no reply. It is run deliberately, over
recorded runs, when someone wants to know where the pipeline is leaking.

Three questions on one mechanism:

- **When did the answer go wrong?** The ladder: forecast at each context
  boundary, in the order the pipeline adds them.
- **Which block made it go wrong?** The ablation: rebuild one recorded prompt
  with a single block removed, and forecast again.
- **Which model sees any of this coming?** The benchmark: run the same
  forecasts across every bound local model and score them against what the
  recorded run actually did.

The ladder finds the stage. The ablation names the culprit. The ladder alone
cannot — context arrives in a fixed order, so the rung where the answer moves
is confounded with everything that arrived at that rung.

The benchmark is not a bonus use of the first two. It is the one that
produces hard numbers, because forecasting **what a step will do** has
ground truth sitting in the trace already — the action taken, the arguments,
and whether it succeeded are recorded facts, not judgments. Everything the
first two instruments need labels for, the benchmark gets free.

## It does not run inside a turn

The instrument replays **recorded runs**. `assistant_step` already persists
the exact `system_prompt` and `user_prompt` of every decide call, the raw
`model_response`, the model identity, and the full observation JSONB. The
context at every boundary of a finished run is therefore reconstructible
without re-running a single tool.

Three consequences, and each removes an objection that sank the live version:

**Production cost is zero.** No forecast is issued during a turn, no latency
is added to a reply, and no switch gates a hot path. The operator pays only
when they run the instrument, on hardware that is otherwise idle.

**The seal is free and unbreakable.** A live forecast has to be kept out of
the answerer's context by discipline — a scratchpad renderer that skips a
row, a summarizer that ignores an action, a test asserting a sentinel never
leaks. In replay there is nothing to enforce: the run being analyzed
*already happened*, and no forecast existed when it did. The answerer cannot
be contaminated by a forecast made after it finished.

**Repeatability is affordable.** Local generation is stochastic, so a single
sample per rung proves nothing — two rungs can differ because the context
differed or because two samples differed. Establishing that needs the same
rung sampled several times, which is unthinkable in a live turn and routine
offline.

### Two invariants

**The forecaster gets the step's own input, not a special one.** For a step
target, the forecast prompt **is** the recorded `user_prompt` of that step.
Not a reconstruction of it, not a summary of it, not a purpose-built context
containing the same facts. The only differences are the task instruction —
*decide the next step* becomes *predict what the decider will do* — and the
response schema.

This is what makes the measurement mean anything. A forecaster given a
tidier, shorter, or better-organized context is not predicting the assistant;
it is answering an easier question, and its score says nothing about the run
it claims to be about. In particular a forecaster must inherit the same
scratchpad truncation, the same section order, and the same guidance blocks —
including the ones suspected of causing the trouble.

The test is a byte comparison: every section of the forecast prompt that
corresponds to a section of the recorded prompt must be identical to it.

**Nothing is added to the assistant's own prompt.** No forecast text, no
forecast instruction, no field reserved for one. The production prompt is
byte-identical whether this instrument has ever been run. A measurement that
changes the thing it measures is not measuring it, and this one cannot,
because the run it reads finished before the instrument existed.

The one rung with no recorded prompt is `cold`, which sees only the request
and transcript — there was never a call with that context. Code builds it
from the stored messages. Every other rung is either a stored prompt or a
stored prompt with sections removed.

## The ladder — when the answer moved

The rungs are the prompt's own section boundaries, in assembly order. They
are not time intervals: if two rungs see the same context, one of them is
worthless.

| Rung | What it adds |
|---|---|
| `cold` | the request and the recent transcript — nothing else |
| `language` | `reply_language_markdown` from the classifier |
| `criteria` | `acceptance_criteria_json`, when the switch was on for that run |
| `settings` | `user_settings_json`, the formatting guide, knowledge calibration |
| `profile` | the query-independent profile block |
| `skills` | the retrieved skill block |
| `step_1` … `step_n` | one observation per rung, in the order the run gathered them |

Two rungs earn their place before any data exists.

**`cold` → `language` should not move the answer's substance.** Language is
delivery: which language a reply is written in cannot change what the correct
answer is. If that rung moves the substance, the language block is steering
content, which is a defect in a block whose entire justification is that it
does not. Cheap to test, and falsifiable.

**A rung that never moves anything is a block that is not earning its
tokens.** The guidance blocks share a 2 700-char budget and were added on
reasoning, not measurement. A block that never changes an answer across a
corpus is a block whose budget belongs to its neighbour.

## The ablation — which block did it

The ladder is monotone: every rung adds context and nothing is ever removed,
so a rung that moves the answer indicts everything that arrived there. The
`settings` rung adds three blocks at once.

The counterfactual is the sharper instrument. Take one recorded prompt, remove
exactly one block, forecast again, and compare against the unmodified
forecast. A block whose removal changes the answer is a block that determined
the answer — and if the delivered answer was wrong and removing the block
fixes it, the block is the defect.

`build_turn_prompts` is already this seam. It exists for
`evals/profile_guidance.py`, it renders the declared-profile blocks from a
**given** profile dict rather than the global setting, and it already takes
`include_formatting`, `include_calibration` and `include_classifier` as
prompt-construction overrides. Leave-one-out over the remaining sections is
an extension of a mechanism built for exactly this purpose, not new
architecture.

Ablation is more expensive than the ladder — one forecast per block per case,
times the repeat count. That is the right place for the budget: it is the
only instrument here that produces attribution rather than correlation.

## Two things worth forecasting

The ladder and the ablation both forecast **the final reply**, and they must:
comparing rungs requires every rung to predict the same thing, or the
divergence you find is just the point where the question changed.

Benchmarking a forecaster is a different job, and it wants a second target.
*Predict the final reply from the pre-step-1 context* and *predict what step
4 will return* are different skills, and a model can be good at one and
useless at the other. Keeping them apart is what makes the result readable.

| Target | Question | Ground truth |
|---|---|---|
| `terminal` | what will the final reply say? | the delivered reply (weak), labels (scarce) |
| `next_action` | which capability will the next step choose? | recorded `action` — exact |
| `step_args` | with what arguments? | recorded `args` JSONB — exact |
| `step_success` | will it succeed? | the observation's recorded `ok` — exact |
| `step_outcome` | what will it return? | recorded `{text, data}` — exact |

The bottom four rows are why the benchmark is the sharpest of the three
instruments. `next_action` is closed-set classification over
`AssistantActionName`, `step_success` is a binary, and both are settled by a
row that already exists. No LLM judge, no operator labelling,
no argument about whether the delivered reply was right.

**The two targets are never mixed into one score.** A model that predicts
tool choices well and final replies badly is a specific, useful finding; an
average across both is a number nobody can act on.

## The step forecast

A forecast is a **bound, not a point.** Wondering what time it is and
answering "twenty past four" tells you almost nothing when you look at the
watch; answering "between four and half past" tells you whether you were
calibrated, and by how much. The same holds here: a single predicted action
scores as right or wrong and throws away everything about how sure the
forecaster was, while a short ranked set with probabilities can be scored
properly — and a forecaster that hedges across everything is caught by the
same scoring rule that rewards a confident correct call.

```python
class ActionCandidate(BaseModel):
    action: AssistantActionName
    probability: int = Field(ge=0, le=100, description=(
        "Probability this is the capability the assistant chooses next."))


class StepForecast(BaseModel):
    reason: str = Field(min_length=1, description=(
        "Brief audit-safe note on what in the context points at this step. "
        "Do not provide hidden chain-of-thought."))
    candidates: list[ActionCandidate] = Field(
        min_length=1, max_length=4, description=(
            "The capabilities the next step might choose, most likely first. "
            "Give one when the context determines it and several when it "
            "does not."))
    args_sketch: str = Field(min_length=1, description=(
        "The arguments you expect for the most likely candidate, as far as "
        "the context determines them. Name what is determined and say what "
        "is not."))
    success_probability: int = Field(ge=0, le=100, description=(
        "Probability the step returns ok rather than an error."))
    outcome: str = Field(min_length=1, description=(
        "What you expect the step to return."))
    outcome_low: str = Field(min_length=1, description=(
        "When the result is a quantity, its lower bound with units. When it "
        "is not a quantity, say so."))
    outcome_high: str = Field(min_length=1, description=(
        "When the result is a quantity, its upper bound with units. When it "
        "is not a quantity, say so."))
```

`action` is typed as the enum, so a forecast cannot name a capability that
does not exist and cannot arrive as prose. The registry is code-owned and the
prompt catalog is generated from it, so the closed set the forecaster picks
from is the same closed set the assistant picked from.

`candidates` is capped at four for the reason the original brief gave for
guessing a place: a short list of live possibilities is a forecast, and a
long one is a refusal to make one.

There is no `will_succeed` boolean beside `success_probability`, and no
`is_terminal` flag beside the candidate list. Both would be a second control
over a decision the first already makes — code thresholds the probability
when it needs a point prediction, and predicting `reply` or
`ask_clarifying_question` *is* predicting a terminal step. Two fields that
can contradict each other is a defect the rest of this design has been
careful to avoid.

`outcome_low` and `outcome_high` are the watch case, and they are required
strings rather than optional numbers for the reason `AcceptanceCriteria`
settled: an optional field beside a filled one gets left blank, and a blank
cannot be told apart from an oversight. When the step's recorded observation
turns out to be numeric, code reads the bounds and scores interval coverage;
otherwise it ignores them. Code reading a number out of a field it asked for
a number in is not prose parsing.

`success_probability` is the field that finally makes calibration measurable
here. Everything else in this document has been careful to say that a
probability beside a free-text claim is not calibration, because calibration
needs a numeric probability, an independently resolved outcome, and enough
comparable cases to test whether 70% claims are right 70% of the time. Step
success supplies all three, in quantity, for free: the probability is
numeric, the outcome is the recorded `ok`, and every step of every stored run
is a case. **This is the calibration corpus** — not the terminal forecasts,
which will always be label-starved.

## Benchmarking forecasters

The hypotheses worth testing are about *where* a model's forecasting holds
up, not whether it forecasts well overall.

- **Good early, bad late** — accuracy against step index. The prior runs the
  other way: later steps have more evidence and a more determined answer, so
  accuracy should *rise*. A model whose accuracy falls as the run lengthens
  is degrading under context length, which is a finding about
  `MAX_SCRATCHPAD_CHARS` and prompt order, not about forecasting.

  Step index confounds two things that move together — more evidence and a
  longer prompt — so it cannot separate them on its own. The control is
  prompt length **at a fixed step index**: runs differ in how much their
  observations produced, so step 3 of a `memory_query`-heavy run carries a
  scratchpad near the cap while step 3 of a short run carries a fraction of
  it. If accuracy tracks length rather than position, the degradation is
  about context size. If it tracks position regardless of length, it is
  about the task getting harder. Without that split, "bad late" is a story.
- **Good at the destination, bad at the route** — high `terminal` accuracy
  from pre-step-1 context together with poor `next_action` accuracy. A model
  that knows the answer but not how this assistant will get there is a good
  reply model and a bad planner, and the codebase can act on that: the roles
  are separately bindable.
- **Domain specialists** — a coding model forecasting `python_run` outcomes,
  scored per capability rather than pooled. If it wins there and loses
  everywhere else, that is an argument for a per-capability binding, not for
  making it the assistant.
- **Size against role** — whether a small model is adequate at closed-set
  action prediction while a larger one is needed for terminal content. The
  cheapest possible finding, and the most immediately spendable.

The benchmark's product is therefore not a winner. It is a **routing table**:
which model to bind to which role, backed by measurement instead of by which
one felt better in chat. `/agentmodel` already has binding-only roles
(`second_opinion`, `reply_audit`, `response_language_classifier`), so the
finding lands somewhere that exists.

### Scoring

| Target | Metric |
|---|---|
| `next_action` | top-1 and top-k accuracy, **macro-F1**, confusion matrix, **log loss** over the candidate probabilities |
| `step_success` | accuracy at a 50% threshold and **Brier score** over `success_probability` |
| `step_args` | field-level match on the args the context determines |
| `step_outcome` | **interval coverage** for quantities; exact for computed values; claim-level for text |
| `terminal` | claim-level against the delivered reply; labelled subset for correctness |

Three of those are proper scoring rules, and that is the point. Accuracy
alone rewards a forecaster that guesses confidently and punishes one that
hedges honestly; log loss and Brier score both. **Interval coverage is the
watch measurement in its purest form** — of the intervals stated with 80%
confidence, how many contained the recorded value? A forecaster whose 80%
intervals contain the truth 55% of the time is overconfident by a number, not
by an impression, and one whose intervals always contain the truth is stating
bounds so wide they cost nothing.

Macro-F1 and the confusion matrix are not decoration. The action distribution
is heavily skewed — `reply` and `memory_query` dominate ordinary runs — so
**every action-prediction table prints the majority-class baseline beside
it.** A forecaster that always guesses `memory_query` will beat a thoughtful
one on raw accuracy, and a benchmark that cannot show that is worse than no
benchmark.

### Four ways this benchmark lies

**Self-forecasting is easier.** A model predicting runs it produced is
predicting its own idioms, not forecasting. Report the cross-model matrix —
every forecaster against every producer's runs — and keep the diagonal
separate. Without this, whichever model generated the corpus wins by
construction and the result looks like a capability finding.

**The corpus inherits its producers.** Recorded runs came from whatever was
bound at the time, so the action distribution reflects those models' habits.
A forecaster tuned to a different style is penalised for a difference in the
corpus rather than a deficiency. Report per-producer breakdowns and the
corpus composition beside any headline.

**The outcome leaks in more places than the prompt.** The stored
`user_prompt` at step *n* contains steps 1..*n*-1 and is safe, but the
subject run also carries an `assistant_run.summary` whose `outcome` field
describes how it ended, a delivered reply in the room, and an
`active_call` checkpoint with partial reasoning. A replay that assembles
context from the run rather than strictly from that step's stored prompt can
hand the forecaster the answer. Every one of those is an explicit exclusion,
and the leak test is a fixture run whose summary contains a sentinel that
must never appear in a forecast prompt.

**Repeats are mandatory here too.** Same-context sampling variance applies to
action prediction exactly as it does to the ladder, and a one-shot accuracy
difference between two models is not a difference. Every cell reports its
repeat count and spread.

## The forecast

A forecast of the answer's *shape* — "a positive quantity of roughly this
magnitude" — catches a unit catastrophe and nothing else. The ordinary local
failure is a plausible, specific, wrong claim, so the forecast is a concise
independent answer decomposed into checkable claims.

```python
class ForecastClaim(BaseModel):
    claim_id: str = Field(min_length=1, description=(
        "Stable id unique within this forecast, used by later resolution."))
    claim: str = Field(min_length=1, description=(
        "One material factual, numerical, or action-outcome claim in the "
        "answer. Keep it short and checkable."))
    source_refs: list[str] = Field(min_length=1, description=(
        "Exact ids from the supplied evidence that support this claim. Use "
        "the id 'unsupported' when nothing supplied supports it."))
    probability: int = Field(ge=0, le=100, description=(
        "Estimated probability that this claim is correct. This is a "
        "forecast, not a style score."))


class AnswerForecast(BaseModel):
    reason: str = Field(min_length=1, description=(
        "Brief audit-safe note about the decisive evidence and uncertainty. "
        "Do not provide hidden chain-of-thought."))
    kind: Literal[
        "quantity", "place", "computation",
        "explanation", "action", "refusal",
    ]
    proposed_answer: str = Field(min_length=1, description=(
        "A concise answer to the request. Substance matters; polished "
        "delivery does not."))
    claims: list[ForecastClaim] = Field(min_length=1)
    unknowns: str = Field(min_length=1, description=(
        "Missing information that could materially change the answer, and "
        "the cheapest check that would resolve it. Say 'none known' only "
        "after checking the supplied evidence."))
```

Every string required and non-empty: an optional field beside a filled one
gets left blank, and a blank field cannot be told apart from an oversight —
the argument `AcceptanceCriteria` already settled. `reason` and `unknowns`
are free text beside constrained fields on purpose (trap 6 of
`2026-07-24-operator-locale-and-language.md`: removing the free-text field
makes the model reason inside the constrained one).

Claims are what make the comparison mechanical. Two prose answers differ on
every rung because wording differs on every rung; two claim sets can be
compared on substance. **The ladder scores claims, never text similarity.**

`source_refs` are data, not decoration. Code issues every id — `request`,
`criteria`, `observation:4` — and marks unknown ids as invented rather than
silently counting them as support. `unsupported` is one of those code-issued
ids, present in the allowlist handed to the model, not a magic string the
validator special-cases; a sentinel outside the id space invites `none`,
`None`, `n/a` and a validator that grows a synonym table.

This matters more here than it would in a guard: **a claim's `source_refs`
are what let the ablation be interpreted.** A claim citing
`observation:4` that survives removing the profile block is expected; a claim
citing `unsupported` that changes when the profile block is removed is a prior
the profile was quietly supplying.

No worked example belongs in the prompt. Smaller models copy example content
into unrelated output (trap 1). Field descriptions state the form; the
fixtures carry concrete cases.

### An evidence ceiling on certainty

Local models put 95 on priors they invented. Code owns the ceiling: the
strongest `source_ref` class a claim cites bounds how certain that claim may
be — a deterministic check supports near-certainty, a tool observation less,
an `unsupported` prior much less.

The ceiling constrains **analysis, never the recorded value.** Clamping the
stored probability would make every later reliability plot a measurement of
the clamp: buckets fill at the ceiling, the curve bends to meet it, and the
picture says the model is well calibrated because code made it so. Both
numbers persist — what the model claimed, and what its evidence allowed.

Thresholds live in the instrument's configuration. Any number written into a
design document survives unexamined into production.

## What a divergence proves, and what it does not

The instrument's whole output is disagreements between model calls, and a
disagreement is weak evidence. Four limits, each of which is affordable to
handle offline and was not affordable live.

**Two samples disagreeing is not one of them being wrong.** Deciding which
needs an independent outcome: a deterministic calculation, a cited
observation, a later operator correction, an eval label, or a human
judgment. The delivered reply is not one — it is the thing under study.

**Adjacent calls differ even when context does not.** Every rung comparison
needs a same-context control: sample the lower rung K times, sample the
higher rung K times, and compare within-rung variation against between-rung
variation. Only a between-rung difference larger than ordinary sampling noise
is evidence that the added context did anything. Without that control, "the
first divergent rung" is a coin flip with a name.

**An unchanged forecast does not prove a block was useless.** The block may
have raised confidence, ruled out an alternative, or improved the explanation
without moving the headline claim. The claim-level comparison catches some of
this; `probability` movement catches more; neither catches all of it, so
"never moves anything" is a flag for investigation, not a verdict.

**More calls repeat the same error.** Shared weights, shared prompt framing
and shared retrieved context produce correlated failures, so agreement across
rungs is a stability signal and never a correctness proof. This is precisely
why ablation matters more than the ladder: removing a block and watching the
answer change is a causal claim about the pipeline, which survives the
models being correlated.

### Ground truth, in three grades

The four limits above all bite hardest on `terminal` forecasts, where the
only reference is another model's output. They barely touch the step targets,
because those resolve against recorded facts. Grade the evidence and never
report across grades:

**Hard.** `next_action`, `step_success`, `step_args`, and computed values.
Settled by `assistant_step.action`, `args`, and the observation's `ok`. No
labels, no judge, available for every step of every stored run. The benchmark
lives here, and so does the calibration corpus.

**Free but weak.** Whether a `terminal` forecast agrees with the delivered
reply. Cheap at any scale, and it establishes only that two outputs agree or
differ — never which is right.

**Scarce and strong.** Labelled correctness for terminal answers, and for the
ladder and ablation, where the question is whether a rung's change was an
improvement. Spend these where the weak tier points.

That last grade is the scarce one, so screening decides where it is spent.
Replay every recorded run and count where answers move — a fact about the
pipeline regardless of which version was right — then build labelled cases
only for the boundaries that ranks highest: exact calculations, questions
answered by a supplied observation, multi-part requests with labellable
omissions, ambiguous requests whose correct outcome is a clarifying question.
Screening finds candidates; only labels assign blame. Reporting a movement
count as a defect rate is the mistake this structure exists to prevent.

## The two reports

**Stage damage**, over many runs: which pipeline stage most often precedes a
wrong answer. Per stage boundary, across the corpus — how often the answer's
claims changed; how often the change was an improvement, a regression, or
unlabelled; movement rate against the same-context control, so noise is
visible; how often removing the block changed the answer; and how often the
block was cited in `source_refs` at all.

That last row is the cheap embarrassment. A block that is never cited, never
moves an answer, and whose removal changes nothing is occupying a guidance
budget its neighbours are competing for.

**Forecaster scorecard**, per model: accuracy by target, by step index, and
by capability, with the majority-class baseline, the repeat spread, and the
self-forecasting diagonal called out. Read down a column to see whether a
model is worth binding to a role; read across a row to see where its
forecasting falls apart.

The scorecard is what turns "local models are worse" into something
spendable. *Worse at what, by how much, at which step, on which capability*
is a routing decision. "Worse" is not.

## Running it

The eval package's existing shape: a module with a CLI, persisting through
the `eval_run` / `eval_result` tables, alongside `runner.py`, `compare.py`,
`monitor.py`, `profile_gate.py` and `profile_guidance.py`.

```bash
python -m evals.forecast_ladder --recent 50 --repeats 3
```

```bash
python -m evals.forecast_ladder --run <assistant-run-uuid> --ablate
```

```bash
python -m evals.forecast_bench --recent 200 --targets next_action,step_outcome --all-models
```

The benchmark sweeps every model in the forecaster set over the same recorded
corpus, so a scorecard is one command and re-running it after a model upgrade
is the same command. It is the long-running one — models by targets by steps
by repeats — and it is also the one that needs no labels, so it can run
unattended against whatever the operator has bound.

Replaying a run re-issues forecasts against reconstructed contexts and
persists them; it posts nothing, touches no room, and writes nothing to the
run being analyzed. Forecast rows are stored against the analysis, not
appended to the subject's trace — a diagnostic must not mutate its
specimen, and a later replay must not read a previous replay's output as if
the original run had produced it.

The model used for forecasting is bound separately, and unbound falls back to
the assistant's group. Which model to use depends on which instrument is
running, and conflating the two is the easiest way to produce a meaningless
number:

- **Ladder and ablation** hold the model **fixed** and vary the context. The
  question is what a block does, so a changing forecaster is a confound.
  Same-model-as-producer is the cleanest choice.
- **The benchmark** holds the context fixed and varies the model. The
  question is what a model can see coming, so the corpus must be identical
  across forecasters, down to the reconstructed prompts.

Every forecast row records the model, prompt revision, sampling configuration
and duration. Without that provenance, no two cells in either report are
comparable.

## From finding to fix

A ranking is only worth producing if something follows it.

- **A guidance block that never moves an answer** → its budget goes to the
  block competing with it, and the change is verified by re-running the
  instrument.
- **A block whose removal improves labelled answers** → the block is wrong,
  not merely useless. The most valuable finding this can produce.
- **The `language` rung moving substance** → the classifier's Markdown is
  doing more than routing, and the fix is in its rendering.
- **An observation the answer ignores** → the rung that added it changed
  nothing while the answer contradicts it. That is a retrieval or capping
  problem: `REPLY_AUDIT_MAX_OBSERVATION_CHARS`-style truncation, or an
  observation the model never sees at the right altitude.
- **`cold` right and the pipeline wrong** → the model knew the answer before
  the pipeline confused it. Rare and worth reading carefully.
- **A model that predicts actions well and replies badly** → bind it to the
  roles that choose rather than compose. The binding-only roles exist.
- **A model whose accuracy falls as the run lengthens** → not a forecasting
  finding. It is context-length degradation, and it points at
  `MAX_SCRATCHPAD_CHARS` and section order rather than at the model.
- **A specialist that wins one capability** → a per-capability binding is a
  real option; making it the assistant because it won one column is not.

Each of these is a prompt or retrieval change, re-measured by re-running the
same replay over the same corpus. That loop — measure, change, re-measure on
identical inputs — is what a recorded-trace instrument buys that a live
mechanism cannot.

## Part 2 — the production guard this might justify later

The same forecast, run live, could **change** a reply rather than explain
one: the decide loop's reply becomes a candidate, a candidate-blind call
answers the same request independently, and `reply_audit` adjudicates the
disagreement against evidence before anything posts.

That is a real feature and it is deliberately downstream of the instrument.
It costs latency on every guarded turn, it needs the seal enforced by
discipline rather than by replay, and its value depends entirely on how often
a second answer disagrees usefully — which is a number the instrument
produces and nobody currently has. Building the guard first means paying for
it before knowing whether it pays back.

The design decisions worth recording now, so the instrument's report can be
read against them:

- **Placement.** After the decide loop produces a `reply` candidate, not
  before every step. A forecast before a step that turns out to be a tool
  call is wasted, because the observation changes the evidence.
- **What it cannot catch.** The forecast would inherit the observations the
  loop chose to gather. A wrong read poisons both calls, they agree, and it
  ships. The guard covers *wrong summary of right evidence*, never *wrong
  evidence*. The `cold` rung is the only thing with the latter property, and
  in production it would bounce good replies far more often than it caught
  bad reads — which is why it belongs in the instrument.
- **One forecast per evidence revision.** Code derives an
  `evidence_revision` from the canonical request, effective constraints,
  supplied profile facts and visible observations. A wording-only bounce
  reuses the forecast; only new evidence buys a new one. Otherwise the
  reference drifts toward the candidate it is supposed to check.
- **The auditor sees claims, not prose.** It already carries the request,
  criteria, language block, settings, formatting guide and observations
  capped at `REPLY_AUDIT_MAX_OBSERVATION_CHARS = 2_000`; a second full answer
  on top is lost-in-the-middle by design. The symmetric move — the candidate
  emitting its own claims — is refused, because that is a second argument on
  `reply`, removed by `2026-07-29-reply-audit-as-its-own-call.md` for reasons
  that apply here unchanged.
- **Code disposes on severity.** If `ReplyProblem` grows `severity`, then
  `severity` and `verdict` are two controls over one decision. A `major` or
  `critical` problem with a valid evidence ref forces `revise` whatever the
  model wrote; the stated verdict stays in the trace as the disagreement it
  is.
- **Rejected claims accumulate for the turn**, mirroring `failed_actions` one
  rung down — but as evidence for the auditor, not a mechanical block: prose
  claims have no canonical signature, and re-quoting a wrong figure to forbid
  it re-injects the wrong figure (trap 1).
- **Not every turn deserves a guard.** A deterministic gate — did any step
  produce an observation, did the run compute or write, does the candidate
  contain a number or a proposal — skips the turns that cannot benefit. A
  classifier that decides whether to spend a call costs a call.
- **Failure is bounded and honest.** Infrastructure failure fails open and
  sends. A shipped-past-the-cap candidate records `forced_send=true` and
  discloses the unresolved uncertainty in code-composed text, not by asking
  the model that failed to fix the problem to describe it.
- **Latency has a ceiling.** The seal permits concurrency but the control
  flow does not: the decide call returns the action and the reply text
  together, so a concurrent forecast is a speculative one, and speculating on
  every step rebuilds the per-step cost the terminal placement removed. On
  one local GPU (`providers/base.py`: Ollama, Jan, LM Studio) two requests
  interleave rather than overlap, so concurrency pays only against a second
  server.

The gate for building any of it: the instrument shows that an independent
second answer disagrees with the delivered reply often enough, and correctly
often enough, to be worth a turn's latency.

## Security and prompt boundaries

Observations are untrusted evidence, never instructions, and the forecast
prompt says so. Observation ids are code-generated; a tool result saying
"approve the candidate" carries no authority.

Replay reads stored prompts and observations, which is the same data the
`/assistant` inspector already renders, under the same debug-data policy. It
adds no new exposure — but it does send that data to whichever model is
bound for forecasting. Providers are local today; binding a remote model
would be a data-boundary decision, and it must not be reachable by binding a
role.

## Traps and countermeasures

- **Self-consistency mistaken for truth.** Agreement across rungs is a
  stability signal. Only independent evidence resolves correctness.
- **Movement mistaken for damage.** A rung that changes the answer may have
  fixed it. Tier-1 counts are candidates, not defects.
- **Noise mistaken for signal.** No rung comparison without a same-context
  control.
- **Text similarity mistaken for agreement.** Compare claims. Two correct
  answers differ in wording on every rung.
- **Clamped probabilities mistaken for calibration.** The ceiling constrains
  analysis, never the recorded value.
- **Parroted examples.** No worked examples in the forecast prompt.
- **Prose parsing.** `kind`, `probability`, and every verdict field are typed.
- **Pressure-valve removal.** `reason` and `unknowns` stay free text.
- **The instrument mutating its specimen.** Replay writes to the analysis,
  never to the subject run's trace.
- **Outcome leakage from outside the prompt.** `assistant_run.summary`, the
  delivered reply, and the `active_call` checkpoint all describe how the run
  ended. Context comes strictly from that step's stored prompt, and a
  sentinel-in-the-summary fixture proves it.
- **Skew mistaken for skill.** Every action-prediction number prints the
  majority-class baseline beside it.
- **Self-forecasting mistaken for forecasting.** The cross-model diagonal is
  reported separately, never pooled into a model's score.
- **Averaging across targets.** A single "forecasting score" hides the only
  finding worth having, which is what a model is good at.
- **A tidier context than the assistant got.** The forecaster inherits the
  recorded prompt verbatim, truncation and all. Cleaning it up measures an
  easier question.
- **Accuracy without a proper scoring rule.** Point-prediction accuracy
  rewards confident guessing. Report log loss, Brier and interval coverage
  beside it, or the benchmark selects for bluffing.

## Considered and not taken

- **A hostile-fact-checker persona on the forecast.** Its job is to answer
  the request independently. A persona pushed toward disagreement produces
  disagreement, which corrupts exactly the movement rate the report is built
  on.
- **Raised temperature as the diversity strategy.** More variance is not more
  truth, and here it directly inflates the noise floor the same-context
  control is measuring. Sampling configuration is a variable to record and
  hold fixed, not a mechanism.
- **A background ladder that corrects the next turn.** Injecting "the last
  message was wrong" into a later scratchpad assumes the ladder detects
  hallucinations, which is the claim this document declines to make.
  Sampling production runs into the corpus is fine; acting on the result
  unsupervised is not.
- **Writing unresolved conflicts into persistent memory.** The assistant
  writing its own unverified uncertainty into memory is what the memory trust
  model exists to prevent — `memory_remember` deliberately creates an inert
  candidate pending operator confirmation. Findings live in the report.

## Implementation seams

1. `AnswerForecast`, `ForecastClaim`, `ActionCandidate` and `StepForecast`
   beside the existing narrow structured models in `agents/assistant.py`.
   `ActionCandidate.action` reuses `AssistantActionName` directly, so the
   closed set stays owned by the registry.
2. A binding-only `answer_forecast` role, resolved through the same fallback
   pattern as `reply_audit` and `response_language_classifier`. No production
   switch is needed, because nothing runs in a turn.
3. Context reconstruction from a recorded run: `cold` built from stored
   messages, every later rung from the stored `user_prompt` with sections
   added or removed.
4. Evidence ids on the shared observation projection — today
   `_build_reply_audit_prompt` emits `<observation action=… status=…>` with
   no ids, so this is new plumbing on an existing prompt builder.
5. Leave-one-out ablation as an extension of `build_turn_prompts`'s existing
   include flags.
6. `evals/forecast_ladder.py`: replay, repeats, ablation, persistence through
   `eval_run` / `eval_result`, and its two CLIs.
7. `evals/forecast_bench.py`: the model sweep, the scorers (accuracy,
   macro-F1, confusion, Brier), and the cross-model matrix.
8. The two reports, with sample counts, the control's variation, and the
   majority-class baseline beside every rate.

Tests must prove:

- the subject run's trace is not modified by a replay;
- a rung's reconstructed prompt differs from its neighbour by exactly the
  sections that rung adds;
- ablation removes one block and nothing else;
- invalid `source_refs` are surfaced as invented, not counted as support;
- claim comparison ignores wording;
- repeats with a fixed configuration produce a reported variance, and the
  report refuses to rank stages without one;
- a run whose `summary` contains a sentinel never produces a forecast prompt
  containing it;
- a forecaster that always names the majority action scores at the printed
  baseline, not above it;
- a candidate's `action` cannot hold a name outside the capability registry;
- every section of a step-target forecast prompt that corresponds to a
  section of the recorded prompt is byte-identical to it;
- the assistant's own production prompt is byte-identical with the
  instrument present and absent.

## What this proposal does not claim

It does not claim a model can certify itself, that two samples equal ground
truth, or that a numeric probability is a calibrated one.

It claims something narrower and testable: the assistant's prompt is built in
stages, its runs are recorded in full, and asking a sealed question at each
stage boundary of a recorded run tells you which stage is costing the most —
which is the thing nobody can currently see, and the thing every prompt
change is currently guessing at.

And one more, which costs nothing extra once the replay exists: because a
recorded step's action, arguments and result are facts rather than
judgments, *predicting them* is a benchmark with real answers. That is where
the question "which local model is good at this" stops being an impression
and becomes a table.
