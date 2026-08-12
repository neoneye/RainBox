# Forecasting recorded runs — where the pipeline becomes sensitive, and which model sees it coming

**Status:** Proposal. Nothing implemented.
**Date:** 2026-07-29

## What this is for

The assistant's prompt is assembled from a dozen sources: a language
classification, acceptance criteria, user settings, a formatting guide, a
knowledge-calibration block, a profile digest, retrieved skills, a transcript,
and one observation per step. When a reply is wrong, nothing in the trace says
**which of those** made it wrong. The operator reads six steps and a bad
message and guesses.

This is a screening instrument for that question. It asks a model to commit
to an answer under controlled prompt variants, seals the commitments, and
compares them. Without correctness labels, that reveals where an answer
became **sensitive** to added context and which block controls the change. On
the labelled subset, it can say whether the change was an improvement or a
regression. The distinction is load-bearing: movement is cheap to measure;
damage is not.

It is **not** a per-turn guard. It does not run on every turn, it does not
run in the turn at all, and it changes no reply. It is run deliberately, over
recorded runs, when someone wants to know where the pipeline is leaking.

Four diagnostics and one ship gate on the same replay foundation:

- **Where did the answer move?** The ladder: forecast at each context
  boundary, in the order the pipeline adds them.
- **Which block controls the movement?** The ablation: rebuild one recorded
  prompt with a single block removed, and forecast again.
- **Which model sees any of this coming?** The benchmark: run the same
  forecasts across every bound local model and score them against what the
  recorded run actually did.
- **Did it use the right kind of support?** The authority report: distinguish
  identity, live operating state, evidence, reusable lessons, and unsupported
  priors instead of treating every retrieved block as proof.
- **Does a second shot improve the delivered answer?** The guard-readiness
  simulation: compare audit-only with forecast-assisted audit and revision on
  held-out labelled replies.

The ladder finds a sensitive boundary. The ablation nominates a controlling
block. Only an independently labelled outcome can call that block a culprit.
The ladder alone cannot even nominate one — context arrives in a fixed order,
so the rung where the answer moves is confounded with everything that arrived
at that rung.

The benchmark is not a bonus use of the first two. It is the one that
produces hard numbers, because forecasting **what a step will do** has
targets sitting in the trace already — the action taken, the arguments, the
wrapper's `ok` status, and the returned observation are recorded historical
facts, not judgments. They are exact targets for imitation and execution
prediction. They are not automatically evidence that the action was wise or
the returned content semantically correct.

### If you are here to build it

Read [Implementation phases](#implementation-phases), then the **Phase 1
build sheet** under it, and stop. Phase 1 is one probability scored against
one recorded boolean; it needs no ablation, no labels, no sandbox, and no new
prompt-assembly code, and it carries a kill gate that makes the rest
unnecessary if it fails.

Everything between here and there is why the design is shaped this way. It is
worth reading before Phase 3, where the choices start to bind, and it is not
worth reading before Phase 1.

## It does not run inside a turn

The instrument replays **recorded runs**. `assistant_step` already persists
the exact `system_prompt` and `user_prompt` of every decide call, the raw
`model_response`, the model identity, and the full observation JSONB. The
actual decide contexts are replayable without re-running a tool. Earlier
within-prompt boundaries are synthetic removals from those stored artifacts,
and `cold` is reconstructed from messages preceding the subject turn; reports
label both accordingly.

Three consequences, and each removes an objection that sank the live version:

**Production cost is zero.** No forecast is issued during a turn, no latency
is added to a reply, and no switch gates a hot path. The operator pays only
when they run the instrument.

That is zero *production* cost, not zero cost, and the difference is worth
being honest about: 200 runs at four steps, three repeats, six models and two
targets is roughly 29 000 inferences before ablation, and ablation multiplies
by the number of blocks. On a single local GPU that is a weekend, not a
coffee break. The suite has to be sized deliberately — see
[Running it](#running-it) — and the ablation is opt-in for a reason.

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

**The subject context is byte-faithful, even though the forecast prompt cannot
be.** A prediction call needs a different instruction and schema from the
decide call, so saying the two prompts are identical would be false. For step
targets, code places the recorded `system_prompt` and `user_prompt` verbatim
inside inert, explicitly delimited subject-prompt sections under a narrow
forecaster system prompt. Conditional mode additionally supplies the recorded
action and arguments as target data. Nothing inside either stored prompt is
rewritten, tidied, reordered, or regenerated.

This is what makes the measurement mean anything. A forecaster given a
shorter or better-organized subject context is answering an easier question.
It must inherit the same scratchpad truncation, section order, guidance
blocks, and action catalog — including the pieces suspected of causing the
trouble. The test is a byte comparison of both embedded subject prompts.

Terminal-answer probes are different: they ask the forecaster to answer from
a synthetic subset of the recorded context, not to predict the producer's
next token. Their surviving prompt sections remain byte-identical and in
their original order, but the probe is correctly labelled synthetic.

The allowed action set is part of the recorded case. It is recovered from the
subject run's action catalog (or a persisted catalog snapshot), not from the
current `AssistantActionName`: a capability added next month was not available
to a run recorded today, and a removed historical action must remain
scoreable.

Behavior targets also include the producer model revision and sampling
configuration as explicit case metadata, because “what will this decider do?”
is conditional on which decider it is. A model-blind ablation can measure how
much that metadata helps, but silently withholding it and then interpreting
producer differences as forecaster skill would be an underspecified task.

**Nothing is added to the assistant's own prompt.** No forecast text, no
forecast instruction, no field reserved for one. The production prompt is
byte-identical whether this instrument has ever been run. A measurement that
changes the thing it measures is not measuring it, and this one cannot,
because the run it reads finished before the instrument existed.

## Prompt stage is not evidence authority

The ladder groups context by **when it enters the prompt**. That is the right
axis for prompt sensitivity and the wrong axis for deciding what a claim may
rely on. A profile preference, a failed tool observation, a pending approval,
and a reusable skill can all be visible in one prompt while carrying four
different kinds of authority.

Use a second, orthogonal classification in every case manifest:

| Context class | RainBox sources | What it is authoritative for |
|---|---|---|
| `identity_context` | user settings; active, non-expired profile facts and preferences; project decisions | who the operator/project is and durable declared preferences |
| `operating_state` | current request, current-turn scratchpad, `assistant_step` rows, the run's completed-write signatures and `failed_actions` set, `KanbanTaskEvent` rows, `assistant_write_intent` rows still `proposed` | continuity: what is in progress, already attempted, failed, or awaiting a decision |
| `evidence` | typed tool observations, action arguments/results, `memory_evidence` rows whose `provenance` qualifies (below), append-only run/task logs | what a named source observed or recorded; claim authority remains limited by provenance |
| `reusable_lesson` | skills, procedures, episode summaries, confirmed eval findings | how similar work may be approached; never proof that this run or fact is correct |
| `unsupported` | model prior with no supplied source | a hypothesis that may be forecast, never evidence |

This is not a replacement `MemoryClaim.kind` taxonomy. RainBox already keeps
durable claims separate from their many `memory_evidence` rows; filters by
status, expiry, sensitivity, and scope; keeps model-inferred writes as
candidates; records retrieval telemetry separately; and stores operational
history in assistant/task/journal rows. Flattening those systems into four new
tables would lose information. The classification above is an eval-time
authority overlay over the contracts in `notes/memory-architecture.md`.

### `memory_evidence` grades itself; use its grades

Mapping the table wholesale into `evidence` would open the laundering path
the overlay exists to close. `memory_evidence.provenance` is CHECK-constrained
to four values, and they are not four flavours of the same authority:

| `provenance` | Overlay class |
|---|---|
| `observed_from_source` | `evidence` — a named source was read |
| `confirmed_by_user` | `identity_context` for a preference; `evidence` for a fact the operator attested |
| `imported_from_transcript` | `operating_state` at best — something was *said*, which is a report, not an observation |
| `inferred_by_model` | `unsupported` — a model prior, whatever row it now sits in |

`inferred_by_model` is the one that matters. Without this split, a model
guesses something, `memory_remember` stores it as a candidate, and a later
forecast cites the resulting row for full evidence authority — a prior that
laundered itself into evidence by being written down. Nothing about being
persisted makes a guess an observation.

`source_type` (`chat_message`, `journal`, `file`, `api`, `manual`,
`transcript`) grades the channel and is recorded beside the class, but it
does not override it: an `inferred_by_model` claim attached to a `file`
source is still a prior about a file.

### Recall locates context; it does not prove it

`memory_query` has two different facts attached to it:

1. the trace can prove that a particular claim was retrieved and shown to the
   model;
2. retrieval cannot prove that the claim text was true.

The first is an exact historical outcome of the retrieval pipeline. The
second depends on the claim's lifecycle and evidence: `confirmed_by_user`,
`observed_from_source`, `inferred_by_model`, later correction, expiry, and so
on. Vector similarity, lexical rank, and the recall-filter verdict explain
**why the claim was found**, not why it should be believed.

Consequences:

- predicting which memories `memory_query` returned is an execution/retrieval
  benchmark, not a factuality benchmark;
- a forecast may cite a recalled claim for identity or preference only with
  its claim uuid and as-of lifecycle metadata;
- a claim that some event happened must cite the underlying evidence or
  append-only event row, not merely the semantically recalled summary of it;
- what is still awaiting a decision comes from `assistant_write_intent`
  rows in `proposed`, never from a retrieved episode that may be stale;
- reusable lessons may justify a proposed method, never a claim that the
  method ran or succeeded here.

### Code owns source metadata

The model emits short `source_refs`; code resolves each one through a
case-local catalog:

```python
from datetime import datetime


class ContextSourceDescriptor(BaseModel):
    source_id: str
    context_class: Literal[
        "identity_context", "operating_state",
        "evidence", "reusable_lesson", "unsupported",
    ]
    authority: Literal["operator", "code", "tool", "model", "unknown"]
    observed_at: datetime | None
    confidence: float | None
    expires_at: datetime | None
    provenance: list[str]
```

These fields are derived from stored rows and prompt assembly; the forecaster
never invents them. `confidence=None` means the source type has no comparable
confidence field, not zero confidence. `expires_at=None` means no declared
expiry, not “fresh forever.” The case manifest also retains scope, sensitivity,
and owner/actor identifiers where the source model has them, without exposing
those as free-form forecast fields.

A prompt section without a descriptor may still participate in sensitivity
analysis — it was visible to the subject model — but it cannot satisfy an
evidence-backed audit problem or raise an evidence-policy ceiling. Missing
provenance is an explicit limitation, not something a model repairs in prose.

Promotion into a durable lesson has a stricter gate: the record must resolve
its source, observation time, owner/actor, confidence semantics, and expiry or
explicit no-expiry decision. If those questions cannot be answered, the
finding remains short-term eval context rather than durable memory.

### Historical visibility and present truth are separate

Replay asks what the subject model could see **then**. If an active memory was
later corrected or expired, the exact stored subject prompt remains the
authority for historical visibility. Semantic scoring records the as-of claim
state when reconstructible and the later correction as separate evidence; it
does not rewrite history with today's memory value.

Freshness is evaluated at the subject timestamp. A claim that expired later
was current for that run; a claim already expired but nevertheless present in
the stored prompt is preserved as historical input and flagged as a prompt
assembly defect.

Paired producer runs are stricter. Every model must start from the same as-of
memory/profile/task fixture. If that state cannot be reconstructed without
consulting today's mutable claims, the case is ineligible for paired
comparison. Ecological expansion may still run it, tagged with the new
environment fingerprint.

## The sensitivity ladder — where the answer moved

The rungs are semantic information sets, not literal prompt prefixes and not
actual calls the historical run made. Each synthetic variant removes whole
sections while preserving the original order and bytes of everything that
survives. Observation rungs insert nothing at the end; they reveal the next
stored scratchpad event at the scratchpad's original position.

| Rung | What it adds |
|---|---|
| `cold` | the request and the recent transcript — nothing else |
| `language` | `reply_language_markdown` from the classifier |
| `criteria` | `acceptance_criteria_json`, when the switch was on for that run |
| `settings` | `user_settings_json`, the formatting guide, knowledge calibration |
| `profile` | the query-independent profile block |
| `skills` | the retrieved skill block |
| `step_1` … `step_n` | one observation per rung, in the order the run gathered them |

Rungs and context classes deliberately do not align one-to-one. `profile` is
mostly identity context; `skills` are reusable lessons; a step observation
can be direct evidence, operating state, or a bundle of semantically recalled
claims. The report therefore shows both the stage where a claim moved and the
classes it cited. It never upgrades every observation to `evidence` merely
because the action loop recorded it.

Two rungs earn their place before any data exists.

**`cold` → `language` should not move the answer's substance.** Language is
delivery: which language a reply is written in cannot change what the correct
answer is. If that rung moves the substance, the language block is steering
content, which is a defect in a block whose entire justification is that it
does not. Cheap to test, and falsifiable.

**A rung that never moves anything is a candidate for ablation.** The
guidance blocks share a 2 700-char budget and were added on reasoning, not
measurement. No movement is not enough to delete one — it may affect
confidence or rare cases — but it is enough to ask whether its budget belongs
to its neighbour.

## The ablation — which block controls the movement

The ladder is monotone: every rung adds context and nothing is ever removed,
so a rung that moves the answer indicts everything that arrived there. The
`settings` rung adds three blocks at once.

The counterfactual is the sharper instrument. Take one recorded prompt, remove
exactly one block, forecast again, and compare against the unmodified
forecast. A block whose removal reliably changes the answer is a block that
was driving the probe. If labelled probe answers improve without it, the
block is a strong harmful-input candidate for end-to-end confirmation.

*Reliably* is doing real work in both sentences. A single altered sample
differing from a single unaltered one is the sampling noise this document
insists on controlling for everywhere else; the comparison is between
distributions over repeats, not between two generations. And what it
establishes is causal about **the forecaster's answer under that context**,
not about the recorded run, which happened once and cannot be re-run. That is
strong evidence about this probe's local sensitivity and not proof about the
failure that prompted the investigation. Treating it as one is how a one-case
finding becomes a prompt change nobody can reproduce.

The stored prompt is the primary seam: remove a structurally identified XML
section from that immutable artifact and leave every surviving byte in place.
`build_turn_prompts` is useful for current-revision fixtures and already takes
`include_formatting`, `include_calibration` and `include_classifier`
overrides, but it is not an authority for historical reconstruction. Prompt
assembly changes over time. A replay that regenerates an old prompt with
today's builder has silently changed its specimen, so every case carries a
prompt-revision hash and old cases are ablated from their stored text.

Ablation is more expensive than the ladder — one forecast per block per case,
times the repeat count. That is the right place for the budget: it is the
only instrument here that produces local probe attribution rather than an
ordered correlation.

### Leave-one-out assumes the blocks act independently

They do not, necessarily. The failure this misses is a **pair**: an answer
that only derails when the profile block and a particular observation are
both present, because they conflict. Remove either and the answer is fine, so
each scores innocent and the bug survives with every block cleared.

The remedy is not a leave-two-out sweep — the pairs grow quadratically and
almost all of them are uninteresting. It is a targeted 2×2 on a *suspected*
pair, run over the cases where the bug appears: both blocks, neither, and
each alone. Four cells, one hypothesis, and it is a debugging tool rather
than a suite. `--ablate-pair a,b` covers it.

The default suite therefore reports single-block attribution and says so.
A block set that clears every block on a case that is reliably wrong is not a
clean bill of health; it is the signature of an interaction, and it is the
cue to reach for the pair flag.

An ablation result is a **screening result about the probe model under a
synthetic prompt intervention**. It does not prove that the historical
producer would have answered differently, and it never ships a prompt change
on its own. A proposed change must pass a paired end-to-end eval on labelled
cases, using the actual assistant role and a state-matched sandbox where tools
are involved.

## Two things worth forecasting

The ladder and the ablation both produce an **independent terminal answer**,
and they must: comparing rungs requires every rung to answer the same request,
or the divergence is just the point where the question changed. This probe is
not asked to imitate the producer's wording or predict its next token.

Benchmarking a forecaster is a different job, and it wants a second target.
*Answer from the pre-step-1 context* and *predict what step 4 will return* are
different skills, and a model can be good at one and useless at the other.
Keeping them apart is what makes the result readable.

| Target | Question | Ground truth |
|---|---|---|
| `terminal` | what answer does this context support? | delivered reply agreement (weak), correctness labels (scarce) |
| `next_action` | which capability will the next step choose? | recorded `action` — exact |
| `step_args` | with what arguments? | recorded `args` JSONB — exact |
| `step_ok` | will the capability wrapper return `ok`? | observation's recorded `ok` — exact execution status |
| `step_outcome` | what historical observation will it return? | recorded `{text, data}` — exact event, semantic quality varies |
| `continuity_policy` | does the predicted/recorded next action respect completed writes, failed actions, and proposals awaiting confirmation? | code-owned invariants where deterministic; labels where repetition is ambiguous |

The recorded-step targets are why the benchmark is the sharpest of the three
instruments. `next_action` is closed-set classification over the case's
recorded action set, `step_ok` is binary, and both are settled by rows that
already exist. `continuity_policy` is deliberately different: code can settle
hard violations, while genuinely ambiguous repeat-work cases stay labelled.
No LLM judge or operator labelling is needed merely to say whether a forecast
matched the trace. Semantic correctness is a separate target and is not free.

**The two target families are never mixed into one score.** A model that
predicts tool choices well and final replies badly is a specific, useful
finding; an average across both is a number nobody can act on.

### Behaviour, execution, and correctness are three different things

"Exact" in that table means the *fact* is exact, not that the fact is right,
and the step targets split on exactly this line:

- `next_action` and `step_args` are settled by **what the assistant chose**.
  Hard as a fact about behaviour, silent as a fact about quality — a
  forecaster predicting `python_run` where the run chose `memory_query`
  scores wrong even when `python_run` was the better move. This is an
  **imitation** score.
- `step_ok` is settled by **execution status**. It says whether the capability
  wrapper reported success. A query can return `ok` with stale or irrelevant
  data, so this must not be called semantic success.
- `step_outcome` is settled by **what was observed in that historical
  environment**. A typed deterministic computation can be treated as a hard
  value. A memory lookup, clock read, external API call, or prose result is
  stateful, time-dependent, or semantically contestable even though the
  recorded bytes are exact.

The sharpest metric in the document is therefore the one most easily
misread. A model scoring badly on `next_action` is not a worse assistant; it
is a worse *predictor of this assistant*. Those coincide only when the
incumbent's choices were good, which nothing here establishes.

All three are worth having, and they answer different questions. Imitation is
useful for predicting this assistant, execution prediction estimates whether
a planned call will return normally, and semantic correctness is what a
model-selection decision ultimately needs. Every scorecard column is labelled
`imitation`, `execution`, or `semantic`; nothing is averaged across those
lines.

`continuity_policy` supplies a narrow correctness signal that imitation cannot:
a challenger may disagree with the incumbent action and still be right not to
repeat a successful write or resubmit an unchanged failed call. Start with
invariants code can defend — no duplicate mutating action after recorded
success, no execution past a pending approval, no identical resubmission after
an error that explicitly requires changed arguments. Reads and verification
repeats are labelled rather than presumed wasteful.

### Joint and conditional modes

Predicting an outcome is entangled with predicting the action, and pooling
them hides the most interesting finding available here.

In **joint** mode the forecaster gets the step's context and predicts both:
which capability, and what comes back. That is what a live guard would need,
and it is the harder task. But an outcome predicted for the wrong action
cannot sensibly be scored against the recorded action's result. Joint mode
therefore reports action metrics on every case, outcome metrics only on the
subset where the top action matched, and an end-to-end hit rate with that
coverage printed beside it. It never pretends the unmatched outcomes are
comparable.

In **conditional** mode code supplies the recorded action and arguments, and
the forecaster predicts only the outcome. *The assistant is about to run this
program in the sandbox — what does it return?* *It is about to query memory
for this — is the fact there, and what does it say?*

Conditional mode isolates outcome prediction from routing: not "do you know
this assistant's habits" but "given this planned call and the state visible in
the prompt, what do you expect?" For deterministic computation this is close
to a world-model test. For memory, time, filesystem, or external-service
capabilities it also measures how much of the required state is hidden from
the prompt. Results are therefore stratified as `deterministic`,
`state_snapshot`, `time_varying`, or `external`; pooling them would turn
environment visibility into apparent model skill.

This is where a coding model would actually show up. Asked to predict
`next_action` it may do poorly — it has no feel for how this assistant
sequences its work — while predicting what a Python program returns is
precisely what it is for. Pooled into one number those cancel and the model
looks mediocre. Scored conditionally and per capability, the finding is
specific enough to justify a direct role eval.

Conditional mode is also the cheaper diagnostic, because it does not spend
the run's hardest prediction to get at the one being asked about.

## The step forecast

A forecast is a **bound, not a point.** Wondering what time it is and
answering "twenty past four" tells you almost nothing when you look at the
watch; answering "between four and half past" tells you whether you were
calibrated, and by how much. The same holds here: one predicted action throws
away uncertainty, while an unbounded list refuses to forecast.

The sparse action forecast must not pretend to be a full multiclass
distribution. Four named candidates with four probabilities leave the mass
for every omitted action undefined, so multiclass log loss would be
mathematically invalid. The default schema instead forecasts two explicit
binary events: whether the first candidate is right, and whether the true
action is anywhere in the short set.

```python
from typing import Any


class QuantityBounds(BaseModel):
    low: float
    high: float
    unit: str = Field(min_length=1)
    coverage_probability: int = Field(ge=1, le=99, description=(
        "Probability that the observed quantity falls inside [low, high]."))


class ActionForecast(BaseModel):
    candidates: list[str] = Field(
        min_length=1, max_length=4, description=(
            "Distinct case-allowed actions, most likely first."))
    top_probability: int = Field(ge=0, le=100, description=(
        "Probability that candidates[0] is the recorded next action."))
    set_probability: int = Field(ge=0, le=100, description=(
        "Probability that the recorded next action appears anywhere in "
        "candidates. Must be at least top_probability."))
    args: dict[str, Any] = Field(description=(
        "Expected arguments for candidates[0]. Use the action's real keys "
        "and omit keys the context does not determine."))


class OutcomeForecast(BaseModel):
    ok_probability: int = Field(ge=0, le=100, description=(
        "Probability the capability wrapper returns ok. This is execution "
        "status, not semantic correctness."))
    outcome: str = Field(min_length=1, description=(
        "What you expect the step to return."))
    bounds: QuantityBounds | None = Field(description=(
        "Bounds on the result when it is a quantity. Null when the step does "
        "not return one."))


class StepForecast(BaseModel):
    reason: str = Field(min_length=1, description=(
        "Brief audit-safe note on what in the context points at this step. "
        "Do not provide hidden chain-of-thought."))
    action: ActionForecast
    outcome_if_top_action: OutcomeForecast


class ConditionalStepForecast(BaseModel):
    reason: str = Field(min_length=1)
    outcome: OutcomeForecast
```

`ActionForecast.candidates` uses strings because Pydantic's enum would be the
**current** registry, not the registry the historical run saw. Code validates
every candidate against `case.allowed_actions`, rejects duplicates, and
checks `top_probability <= set_probability`. This is still a closed set; it
is simply the correct historical one.

`candidates` is capped at four for the reason the original brief gave for
guessing a place: a short list of live possibilities is a forecast, and a
long one is a refusal to make one.

There is no `will_succeed` boolean beside `ok_probability`, and no
`is_terminal` flag beside the candidate list. Both would be a second control
over a decision the first already makes — code thresholds the probability
when it needs a point prediction, and naming `reply` or
`ask_clarifying_question` *is* predicting a terminal step. Two fields that
can contradict each other is a defect the rest of this design has been
careful to avoid.

`args` is structured JSON rather than `args_sketch` prose because field-level
matching otherwise requires parsing prose. It is scored only when the top
action matched, after capability-specific normalization; omitted,
secret-generated, time-generated, and nondeterministic fields are reported as
unscored rather than guessed.

`bounds` is the watch case, and it is the one nullable payload in this
schema. That needs justifying, because the standing rule from
`AcceptanceCriteria` is that an optional field beside a filled one gets left
blank and a blank cannot be told apart from an oversight.

The rule does not transfer, because the two blanks are different. An empty
`formatting` is a *skipped judgment* — there is always something to say about
formatting, so silence hides work not done. A `memory_query` returning fenced
text has no lower bound; a bound there is not omitted, it is inapplicable.
Forcing "not a quantity" into two string fields on most steps would spend
tokens and generation time restating a type mismatch.

Two ways of making it conditional are worse than nullable. A validator that
decides whether `outcome` "represents a quantity" has to read prose to do it,
which is trap 3 arriving in the schema layer. A `bounds_apply` boolean beside
the object is a second control over what the object's presence already says —
the same defect that removed `will_succeed`.

The blank-versus-oversight worry does not disappear; it moves somewhere it
can be measured. “Numeric” is decided only by a capability-specific scorer
reading a typed JSON path, never by parsing `outcome` prose. When that scorer
marks an observation boundable and `bounds` is null, the forecaster declined
to bound something boundable; report that rate rather than silently skipping
it.

`coverage_probability` supplies what the earlier bounds schema was missing:
the nominal coverage of the interval. Coverage alone rewards intervals from
negative infinity to positive infinity, so report normalized width and a
proper interval score beside coverage. Unit normalization is
capability-specific; an unrecognized or incompatible unit is an invalid
forecast, not a string-comparison miss. Code also requires `low <= high`.

`low` and `high` are `float`, and deliberately not `Decimal`. Every numeric
field in every structured-output model this codebase already runs is an
`int` with bounds; nothing exotic has been through the llama-index path,
which is the layer trap 5 of `2026-07-24-operator-locale-and-language.md`
names — its streaming partial parser constructs the response *without*
validation, so a field can arrive as a type the schema forbids. `Decimal`
also renders as a JSON-schema string under Pydantic v2, which would ask a
grammar-constrained model for a string and hand the scorer prose to parse
while looking typed. Forecast bounds need no exact decimal arithmetic, so
the risk buys nothing. Re-validate on the way out regardless, per that same
trap.

`ok_probability` is the field that makes one useful calibration measurement
cheap. Everything else in this document has been careful to say that a
probability beside a free-text claim is not calibration, because calibration
needs a numeric probability, an independently resolved outcome, and enough
comparable cases. Wrapper `ok` supplies all three, in quantity, for free.
This is an **execution-status calibration corpus**, stratified by capability
and environment class — not a semantic-correctness corpus, and not the
terminal forecasts, which will always be label-starved.

## Benchmarking forecasters

The hypotheses worth testing are about *where* a model's forecasting holds
up, not whether it forecasts well overall.

- **Good early, bad late** — accuracy against step index. The prior runs the
  other way: later steps have more evidence and a more determined answer, so
  accuracy might rise. A model whose accuracy falls as the run lengthens is a
  candidate for a context-length investigation, not proof of degradation.

  Step index confounds two things that move together — more evidence and a
  longer prompt — so it cannot separate them on its own. The control is
  prompt length **at a fixed step index**: runs differ in how much their
  observations produced, so step 3 of a `memory_query`-heavy run carries a
  scratchpad near the cap while step 3 of a short run carries a fraction of
  it. If accuracy tracks length rather than position, the degradation is
  about context size. If it tracks position regardless of length, it is
  about the task getting harder. Without that split, "bad late" is a story.
- **Good at the destination, bad at the route** — high labelled terminal
  correctness from pre-step-1 context together with poor `next_action`
  imitation. That pattern nominates the model for reply-generation eval and
  argues against using its forecast score as evidence of planning skill.
- **Domain specialists** — a coding model forecasting `python_run` outcomes,
  scored per capability, conditionally, rather than pooled. If it wins there
  and loses everywhere else, that earns it a direct per-capability role eval,
  not a binding.
- **System understanding against habit** — conditional accuracy high and
  `next_action` accuracy low can indicate a model that predicts tool outcomes
  without knowing this assistant's habits — but only on deterministic or
  state-matched cases. The reverse can indicate imitation without outcome
  understanding. Both are hypotheses for direct tests, not role verdicts.
- **Continuity under a long run** — whether the forecast respects completed
  work, failed arguments, and proposals awaiting confirmation as the scratchpad
  grows. A model can imitate the next action well overall while repeatedly
  failing the few state transitions that make workflow agents unsafe.
- **Size against target** — whether a small model is adequate at closed-set
  action forecasting while a larger one is needed for terminal content. The
  cheapest possible finding, and an immediate way to narrow the role evals.

The benchmark's product is therefore not a winner or a routing decision. It
is a **routing shortlist**: which model deserves a direct eval for which
role. Predicting a capability's outcome and performing that capability's
checking role are related skills, not identical ones. `/agentmodel` already
has binding-only roles (`second_opinion`, `reply_audit`,
`response_language_classifier`), so a forecast result has somewhere concrete
to be tested. No binding changes until the candidate also wins the role's
own labelled end-to-end eval.

### Scoring

| Target | Metric |
|---|---|
| `next_action` | top-1 and top-k accuracy, **macro-F1**, confusion matrix; Brier score for the declared top-1 and candidate-set events; average set size beside coverage |
| `step_ok` | accuracy at a 50% threshold and **Brier score** over `ok_probability`, stratified by capability and environment class |
| `step_args` | normalized field-level match when the top action matched; scored-field coverage printed beside it |
| `step_outcome` | capability-specific: exact for deterministic typed values; interval coverage, normalized width and interval score for quantities; retrieved-id set/rank metrics for `memory_query`; generic prose is descriptive unless a separate labelled scorer exists. Joint outcome scores include route-match coverage and are never pooled with conditional |
| `continuity_policy` | deterministic violation count and rate by rule; labelled precision for ambiguous repeat-work cases |
| `terminal` | claim-level against the delivered reply; labelled subset for correctness |
| any target with claims | lexical citation-mismatch screen, reported alone and never called entailment |

Every target also reports **first-attempt schema validity**, repair/retry
count, and valid-output latency. A local model that becomes excellent only
after three structured-output repairs is not a cheap forecaster. Invalid
first outputs are model failures, but a missing probability cannot be inserted
into a proper score without inventing a fallback. Therefore proper scores are
reported conditional on schema validity and always paired with validity
coverage; the release gate treats an invalid case as failed. Any combined
utility that assigns maximum loss to invalid output is explicitly named,
versioned, and kept separate from the raw proper score.

Proper scoring rules matter, but only where the schema defines the probability
event completely. The sparse action set does not support multiclass log loss;
its two declared binary events support Brier scores. **Interval coverage is
the watch measurement in its purest form** — of the intervals stated with
80% confidence, how many contained the recorded value? Coverage is printed
with width and interval score because an interval that contains everything is
otherwise unbeatable.

Macro-F1 and the confusion matrix are not decoration. The action distribution
is heavily skewed — `reply` and `memory_query` dominate ordinary runs — so
**every action-prediction table prints the majority-class baseline beside
it.** A forecaster that always guesses `memory_query` will beat a thoughtful
one on raw accuracy, and a benchmark that cannot show that is worse than no
benchmark.

### Scored coverage is itself a reported number

Outcome scoring needs to know what a capability's observation *means*, and
writing one deterministic scorer per capability does not scale: every new
tool would owe the eval suite a unit normalizer and a JSON-path comparator
before it could ship. A suite that taxes every new capability is a suite that
quietly discourages new capabilities.

The way out is to stop treating full coverage as a precondition. Three tiers,
and the report says which one each result came from:

- **Typed** — the observation is already JSON (`kanban_read`, `kanban_query`,
  `find_uuid`) or a computed value (`python_run`). A generic JSON-path and
  numeric comparator covers these with no per-capability code.
- **Generic extraction** — numbers, dates and uuids pulled from text by one
  shared scorer, not twelve.
- **Unscored** — prose observations with no comparator. `memory_query`'s
  fenced text and `workspace_read_command`'s output start here.

**Every table prints scored coverage: how many capabilities and what share of
steps were actually scored.** "Scored 4 of 12 capabilities, 61% of steps" is
an honest headline. Silently averaging over whatever happened to be scoreable
is the same failure as omitting the majority-class baseline — a number that
looks like it describes the system and describes a convenient subset.

Coverage grows when a capability earns a comparator, which is a deliberate
decision with a cost, not a tax collected at capability-creation time.

### Ways this benchmark lies

**Self-forecasting may be easier.** A model predicting runs it produced may
recognize its own action habits. Report the cross-model matrix — every
forecaster against every producer's runs — and keep the diagonal separate.
The diagonal advantage is an empirical quantity, not something to assume or
pool into a capability finding.

**The corpus inherits its producers.** Recorded runs came from whatever was
bound at the time, so the action distribution reflects those models' habits.
A forecaster tuned to a different style is penalised for a difference in the
corpus rather than a deficiency. Report per-producer breakdowns and the
corpus composition beside any headline.

Reporting is not enough on its own, because an incumbent-only corpus makes
every challenger imitate the model it might replace. A distinctive but better
policy can score poorly for disagreeing with bad incumbent choices. Left
there, the benchmark can rank predictors of the incumbent and cannot justify
a policy swap.

The fix is **not** shadow-routing candidate models onto the operator's real
turns. That spends the operator's actual replies to buy corpus diversity, and
in a single-operator system that bill is not small.

The corpus does not need production to grow producers. Recorded requests can
seed new runs with a different model bound against an isolated evaluation
environment. Those are genuine model-B trajectories and every forecaster can
be scored against what model B actually did there.

They are **not counterfactual replays of the production runs**. A request such
as “summarize today's unread mail” can see five messages in production and
none in the evaluation environment; a memory query can hit a different
profile; “today” can be another date. The resulting trajectory is valid as a
new forecasting case, but producer differences cannot be attributed to the
model when state, transcript, clock, or tool fixtures also changed.

The producer sweep therefore has two explicitly separate modes:

- **ecological expansion** reuses eligible requests in whatever isolated
  fixture state is declared. It grows action diversity, is tagged with an
  environment fingerprint, and is never presented as a paired model
  comparison;
- **paired producer comparison** starts every model from the same hermetic
  case bundle: request and required prior transcript, declared-profile
  snapshot, frozen clock, database fixture, workspace fixture, and stubbed
  external responses. Only this mode supports statements about model A versus
  model B on the same task.

Requests that depend on unavailable personal or external state are excluded
from paired mode rather than converted into trivial empty-result cases. Every
report stratifies production trajectories, ecological sandbox trajectories,
and paired fixture trajectories.

### What the producer sweep is actually for

The hermetic mode above is a state-perfect replay harness — frozen clocks,
fixture databases, stubbed external responses — and it is worth being precise
about what would need it, because the honest answer is: nothing in the first
four phases.

The sweep was introduced to fix incumbent bias: a corpus of one model's runs
grades every challenger on imitating the model it would replace. That worry
was half a mis-framing of my own, and the document has since removed its own
grounds for it.

**`next_action` is explicitly an imitation score.** If the target is *predict
what this assistant does*, then a corpus of this assistant's runs is not a
biased sample of the target — it **is** the target. There is no producer
diversity to add, because a different producer would be a different question.
The bias only bites when imitation is read as competence, which the document
now forbids twice.

**Conditional outcome forecasting is already producer-independent.** Code
supplies the action and arguments, so whichever model chose them has left the
scoring entirely. Model A and model B can be compared on the same recorded
step, from the same recorded prompt, with no fixture anywhere — the
comparison is paired by construction because the case is a fixed historical
artifact.

What genuinely needs the hermetic bundle is one question neither target asks:
*would model B have chosen better?* That is competence, it needs labels, and
labelled correctness does not arrive until Phase 4. Building a replay
hypervisor to answer a question that is four phases away and gated on a
scarce resource is the definition of premature.

So the isolation requirements below stand as the contract for **if** a
producer sweep is ever run — they are the right requirements and they should
not be softened — but nothing in Phases 1 to 5 runs one. Ecological expansion
remains available for corpus growth; paired hermetic comparison is deferred
until something needs it.

### A sandbox database is not a sandbox

Pointing at `rainbox_claude` protects `rainbox_production`; it does not isolate
the filesystem, network, provider credentials, calendar, email, or other
external capabilities. Nor does it mean no operator data was touched: the
recorded request and any copied transcript are operator data, even on the
same machine.

Before a producer sweep starts, code proves all of the following:

- the database is the designated evaluation database, never production;
- each case receives a fresh snapshot or transaction that is discarded, so a
  write in case 1 cannot change case 2 or make model order matter;
- filesystem access is rooted in a disposable case directory;
- external network and real provider credentials are absent;
- write and confirm-tier capabilities are disabled or replaced by
  deterministic fixtures;
- the case manifest allowlists which transcript/profile fields may be copied
  and carries their sensitivity and retention policy;
- the clock and locale are fixed when the request depends on them.

The sweep refuses to start when any proof is missing. “Reads are harmless” is
not an acceptable safety rule: a read can exfiltrate personal data, consume a
paid API, or make the benchmark state-dependent.

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
    claim_role: Literal[
        "fact", "preference", "operating_state",
        "execution_event", "calculation", "recommendation",
    ]
    claim: str = Field(min_length=1, description=(
        "One material factual, numerical, or action-outcome claim in the "
        "answer. Keep it short and checkable."))
    source_refs: list[str] = Field(min_length=1, description=(
        "Exact ids from the supplied context that support this claim. Use "
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

Claims make the unit of comparison explicit. Two prose answers differ on
every rung because wording differs on every rung; claim sets at least expose
the propositions to compare. Exact typed values can be normalized
mechanically. Generic semantic equivalence still needs a labelled scorer or a
reported judge model and is never disguised as a string metric. **The ladder
scores claims, never whole-answer text similarity.**

`source_refs` are data, not decoration. Code issues every id — `request`,
`criteria`, `observation:4` — and marks unknown ids as invented rather than
silently counting them as support. `unsupported` is one of those code-issued
ids, present in the allowlist handed to the model, not a magic string the
validator special-cases; a sentinel outside the id space invites `none`,
`None`, `n/a` and a validator that grows a synonym table.

Each id resolves to the `ContextSourceDescriptor` catalog above. Support is
claim-relative: an operator-confirmed preference can support how to format an
answer, operating state can support that approval is pending, and a skill can
support a proposed procedure. None of those proves that a tool ran or a
historical event occurred. The scorer records both citation presence and
authority compatibility against `claim_role`. The role is model-declared and
therefore auditable, not trusted: labelled cases separately count role
misclassification so a model cannot make a weak source compatible merely by
calling an execution claim a recommendation.

This matters more here than it would in a guard: **a claim's `source_refs`
are what let the ablation be interpreted.** A claim citing
`observation:4` that survives removing the profile block is expected; a claim
citing `unsupported` that changes when the profile block is removed is a prior
the profile was quietly supplying.

### Citing a real id is not the same as being supported by it

Id validation catches the easy half — a citation to `observation:9` in a
context with four observations is mechanically invented. It cannot catch the
common half: a model that made something up and attributed it to
`observation:1` because the schema demanded a reference and `unsupported` felt
like an admission. Models are reliably bad at declaring their own priors, and
a required citation field is pressure to name *something*.

Left unmeasured, that failure is invisible and the `unsupported` id becomes
decorative. It is partly screenable without a judge: for quoted identifiers,
UUIDs, dates, and exact scalar claims, code can check normalized value overlap
with the cited block. A miss is a **lexical citation mismatch**. It is not
called hallucination or lack of support: a derived value may be calculated
from cited inputs, a paraphrase may share no token, and a copied token may
still be used dishonestly.

The screen gets its own scorecard column and is never folded into calibration
or correctness. Capability-specific provenance checks can promote a subset to
hard mismatches — for example, an asserted returned uuid differing from the
typed uuid in the cited observation. Everything else is a candidate for
labelled support review. Token overlap is cheap triage, not entailment.

No worked example belongs in the prompt. Smaller models copy example content
into unrelated output (trap 1). Field descriptions state the form; the
fixtures carry concrete cases.

### An evidence policy beside certainty

Local models put 95 on priors they invented. Code may define a policy ceiling
by source authority, provenance, and as-of freshness — not because every tool
observation is intrinsically truer than every confirmed preference, but
because the operator may choose to treat unsupported or stale confidence as a
review trigger. Context class alone never sets the ceiling.

The policy never clamps or rescales the probability. Clamping would make every
later reliability plot a measurement of the policy rather than the model.
Persist the model's probability, resolved source descriptors, authority
compatibility, and a separate `evidence_policy_violation` flag. Calibration
is scored on the raw probability; stale citations, lesson-used-as-proof,
recall-used-as-receipt, and other policy violations are counted separately.

Thresholds live in versioned eval configuration and are validated against
labelled cases. They are governance choices, not laws of probability.

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
rungs is a stability signal and never a correctness proof. Ablation is still
sharper than the ladder because it controls one synthetic input at a time,
but its causal claim stops at the probe model under that intervention. It
does not become a causal claim about the historical producer or the full
pipeline merely because repeats agree.

### Reference strength, in three grades

The four limits above all bite hardest on `terminal` forecasts, where the
only reference is another model's output. They barely touch the step targets,
because those resolve against recorded facts. Grade the evidence and never
report across grades:

**Hard historical targets.** `next_action`, recorded `step_args`, wrapper
`step_ok`, and values extracted by capability-specific deterministic scorers.
Settled by `assistant_step.action`, `args`, and typed observation fields. No
judge is needed to compare them with the trace. Action and args remain
imitation targets, `ok` remains execution status, and only the deterministic
value subset is semantic ground truth.

**Free but weak.** Whether a `terminal` forecast agrees with the delivered
reply. Cheap at any scale, and it establishes only that two outputs agree or
differ — never which is right.

**Scarce and strong.** Labelled correctness for terminal answers, generic
prose outcomes, and the ladder and ablation, where the question is whether a
rung's change was an improvement. Spend these where the weak tier points.

That last grade is the scarce one, so screening decides where it is spent.
Replay every recorded run and count where probe answers move — a fact about
prompt sensitivity regardless of which version was right — then build
labelled cases only for the boundaries that rank highest: exact calculations,
questions
answered by a supplied observation, multi-part requests with labellable
omissions, ambiguous requests whose correct outcome is a clarifying question.
Screening finds candidates; only labels assign blame. Reporting a movement
count as a defect rate is the mistake this structure exists to prevent.

## The reports

**Prompt sensitivity**, over many runs: which pipeline boundary most often
precedes a material answer change. Per boundary — how often claims changed;
on the labelled subset, how often the change was an improvement or regression;
movement against the same-context control; how often removing the block
changed the answer; and how often the block was cited in `source_refs`.
“Stage damage” is reserved for labelled regressions.

That last row is the cheap embarrassment. A block that is never cited, never
moves an answer, and whose removal changes nothing is occupying a guidance
budget its neighbours are competing for.

**Authority use**, per claim and model: which context class was cited, whether
the citation was compatible with the claim, whether it was current at the
subject timestamp, and whether semantic recall was mistaken for an audit
receipt. This is where “the model found a memory” stays visibly separate from
“the evidence says it happened.”

**Forecaster scorecard**, per model: accuracy by target, by step index, and
by capability, with the majority-class baseline, the repeat spread, the
self-forecasting diagonal, environment class, and lexical citation-mismatch
screen called out. First-attempt schema validity and retry cost sit at the
left of every score row. Every column is labelled imitation, execution, or
semantic. Read down a column to see which role eval a model has earned; read
across a row to see where its forecasting falls apart.

The scorecard turns "local models are worse" into a testable nomination.
*Worse at what, by how much, at which step, on which capability* tells you
what direct role eval to run next. It does not replace that eval.

**Guard readiness**, on held-out labelled terminal cases, compares the current
audit with forecast-assisted audit and revision. It is defined after the
replay-to-fix loop because it is the release report, not another descriptive
dashboard.

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
python -m evals.forecast_bench --recent 200 --targets next_action,step_outcome --mode joint --all-models
```

The benchmark sweeps every model in the forecaster set over the same recorded
corpus, so a scorecard is one command and re-running it after a model upgrade
is the same command. It is the long-running one — models by targets by steps
by repeats. Imitation, wrapper-`ok`, and deterministic typed-outcome targets
need no labels and can run unattended; generic prose and terminal correctness
cannot.

Every run starts with a frozen manifest: case ids and hashes, producer and
environment fingerprints, prompt revisions, model revisions, sampling
configuration, scorers, seed schedule, and requested call budget. The CLI
prints the estimated calls and tokens before starting, accepts explicit
wall-time and call ceilings, checkpoints each completed cell, and resumes by
manifest id. A weekend sweep must fail resumably, not restart expensively.

Cases are split by originating request/trajectory, never by generated repeat,
into discovery and locked holdout manifests. Prompt, schema, threshold, and
model-routing choices may use discovery results; the release report is run
once on holdout. Near-duplicate requests and producer variants of the same
case stay in the same split.

### Schedule for locality; verify cache reuse

Repeats are a large multiplier. The sweep loops model-outermost, then case,
then repeats innermost to avoid model reloads and to give any provider prompt
cache the best chance of hitting.

That is a scheduling optimization, not a cost guarantee. Ollama, Jan, LM
Studio, and their configured backends do not share one cache contract;
identical API requests may perform a full prefill every time, cache only
inside a session, or evict on structured-schema changes. Benchmark cache hits
and prefill time per provider and report what was observed. Correctness,
sample count, and budget estimates assume no cache until measurement proves
otherwise.

The ladder tempts a second optimization that should be declined. Its rungs
add sections cumulatively, so building them as strict prefixes of one another
would make each rung cost only its delta tokens. But the assistant's real
prompt has a fixed section order — the request leads, the guidance blocks and
local time trail — and rungs built as prefixes would put sections in an order
no real step ever saw. The instrument's value rests on the prompts being the
pipeline's own, so fidelity wins and the ladder pays full prefill per rung.
Recording the trade here so nobody re-derives it as a clever saving later.

Each repeat uses the manifest's distinct seed when the provider supports one.
At deterministic settings, identical outputs are recorded as zero observed
sampling variance rather than treated as independent evidence. Statistical
intervals resample by case or originating request, not by generation: K
decodes of one prompt are repeated measurements, not K independent tasks.
Model differences use paired case-level bootstrap intervals, and every table
prints case count separately from generation count.

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

- **A guidance block that never moves an answer** → nominate a smaller or
  removed variant, then test that variant in the actual assistant eval.
- **A block whose removal improves labelled probe answers** → treat the block
  as a strong defect candidate and run the end-to-end assistant ablation.
- **The `language` rung repeatedly moving substance beyond the control** →
  investigate whether the classifier Markdown is doing more than routing.
- **An observation the answer appears to ignore** → check first that the
  observation is correct and visible; then investigate capping, placement, or
  overload such as `REPLY_AUDIT_MAX_OBSERVATION_CHARS`-style truncation.
- **The labelled `cold` probe right and full probe wrong** → later context may
  be harmful. Repeats and end-to-end confirmation decide whether the
  historical assistant was actually confused.
- **A model that predicts actions well and replies badly** → nominate it for
  the direct eval of a choosing role. Prediction alone does not earn a
  binding.
- **A model whose accuracy falls as the run lengthens** → investigate context
  length using the fixed-step control. Position, prompt length, and case
  difficulty remain confounded until that control separates them.
- **A specialist that wins one capability** → nominate a per-capability role
  eval; making it the assistant because it won one forecast column is not.

Replay verifies that the synthetic sensitivity moved in the expected
direction. Shipping still requires a paired end-to-end run of the actual
assistant on labelled cases. The confirmation uses the same case manifest and
state fixture, changes only the proposed prompt or binding, and scores the
delivered behavior. This instrument nominates fixes; it does not certify them.

## Guard-readiness simulation — does the second shot improve the answer?

Movement and forecaster accuracy do not answer the operator's original
question: local models are inaccurate on the first shot, so does this
mechanism make the delivered answer better? That needs an end-to-end offline
simulation of the proposed guard, not an inference from disagreement rates.

On the labelled terminal subset:

1. retain the historical delivered reply as the first candidate;
2. generate one candidate-blind `AnswerForecast` from the last pre-reply
   context, excluding the delivered reply and all later trace state;
3. run the current `reply_audit` twice on the same case — audit-only, then
   audit plus forecast claims — with model and sampling held fixed;
4. when the forecast-assisted audit says revise, run one bounded revision
   call with the request, constraints, observations, and evidence-backed audit
   problems whose source descriptors are current and authority-compatible;
   never show the reviser the raw proposed answer from the forecast;
5. score the historical candidate and simulated delivered reply against the
   same independent label.

Cases whose repair requires a new tool observation are reported as
`verification_required`, not silently converted into rewrite cases. A second
phase may run those against a hermetic state fixture; the recorded-only phase
cannot create evidence that was absent from the trajectory.

The report contains:

- defect recovery: wrong first candidates made correct;
- correct-answer regression: correct candidates made wrong;
- audit selection accuracy when candidate and forecast disagree;
- false-bounce and no-op-revision rates;
- unresolved and verification-required rates;
- deterministic guard-gate recall: how many repairable defects the cheap gate
  would have skipped;
- quality lift over audit-only, paired by case;
- calls, tokens, median latency, and p95 latency.

The first release gate for a live guard is positive paired quality lift over
the existing audit, with a low correct-answer regression rate on a held-out
case set. “The second answer often disagrees” is not a gate. Neither is “the
forecast alone is more accurate”: the auditor and reviser are part of the
production mechanism and must be measured with it.

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
  evidence*. A `cold` prior can disagree with poisoned evidence, but cannot
  establish which side is right; live it would create many unactionable
  bounces. That is why it belongs in the labelled instrument.
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
  `critical` problem with a valid, current, authority-compatible evidence ref
  forces `revise` whatever the model wrote. A recalled lesson or stale claim
  cannot trigger the same mechanical force. The stated verdict stays in the
  trace as the disagreement it is.
- **Rejected claims accumulate for the turn**, mirroring `failed_actions` one
  rung down — but as evidence for the auditor, not a mechanical block: prose
  claims have no canonical signature, and re-quoting a wrong figure to forbid
  it re-injects the wrong figure (trap 1).
- **Not every turn deserves a guard.** A deterministic gate — did any step
  produce an observation, did the run compute or write, does the candidate
  contain a number or a proposal — skips the turns that cannot benefit. A
  classifier that decides whether to spend a call costs a call. The gate's
  recall on repairable labelled defects is part of guard readiness; cheap
  false negatives erase the feature's benefit.
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

The gate for building any of it: the guard-readiness simulation shows that
forecast-assisted audit and bounded revision improve held-out delivered
answers over the existing audit often enough to justify the measured latency.

## Security and prompt boundaries

Observations are untrusted evidence, never instructions, and the forecast
prompt says so. Observation ids are code-generated; a tool result saying
"approve the candidate" carries no authority.

Replay reads stored prompts and observations, which is the same data the
`/assistant` inspector already renders, under the same debug-data policy. It
does add new processing and persisted derived output, and a producer sweep may
copy selected request/transcript data into case fixtures. Eval rows inherit
the source case's sensitivity and retention; exports are private by default
and redact according to the manifest.

The data is sent to whichever model is bound for forecasting. Providers are
local today; binding a remote model would be a separate data-boundary decision
and must not be reachable merely by binding a role. The eval preflight rejects
non-local endpoints until an explicit remote-eval policy exists.

The eval corpus must not become “remember everything” by another name.
Store one canonical redacted case snapshot when replay requires it; repeat
rows reference the case id and prompt hash rather than copying the full prompt
again. Derived forecasts inherit the strictest source sensitivity and an explicit
retention policy.

**A TTL on snapshots, not a dependency graph.** Propagating a production
deletion into the eval store means a bidirectional dependency graph between
the memory subsystem and an analytical store, walked on every expiry, editing
JSON blobs and prompt hashes in place. That is a large amount of machinery
whose only job is to make a copy disappear slightly sooner than time would
have removed it anyway.

The cheaper mechanism is a hard TTL on the copies, and it works because eval
artifacts split cleanly in two:

- **Snapshots** — embedded prompts, transcripts, observations, forecast prose.
  These are operator content and they expire mechanically, on a short clock,
  with no reference to what happened upstream.
- **Scores** — accuracy, Brier, coverage, case ids, model ids, prompt hashes,
  scorer versions. Derived measurements, carrying no operator content, and
  they persist.

That split is what makes the TTL affordable. A blanket purge would destroy
the longitudinal comparison the scorecard exists for — the whole point is
re-running it after a model upgrade and seeing what moved. Keeping the
numbers and dropping the copies preserves that, and an expired case can be
re-derived from the live trace whenever the trace is still there.

The consequence is stated rather than hidden: after the TTL, a score can no
longer be audited back to the exact text that produced it. That is the price
of not building the graph, and for an internal instrument it is the right
trade. A finding worth keeping past the TTL is worth promoting into a
labelled case, which is a deliberate act with an owner.

Findings remain eval artifacts. A repeated labelled finding may be proposed as
a reusable lesson or skill only with its supporting case set, owner, review
date, confidence, and expiry; model-generated “lessons learned” never enter
durable active memory automatically.

## Traps and countermeasures

- **Self-consistency mistaken for truth.** Agreement across rungs is a
  stability signal. Only independent evidence resolves correctness.
- **Movement mistaken for damage.** A rung that changes the answer may have
  fixed it. Tier-1 counts are candidates, not defects.
- **Noise mistaken for signal.** No rung comparison without a same-context
  control.
- **Text similarity mistaken for agreement.** Compare claims. Two correct
  answers differ in wording on every rung.
- **Evidence policy mistaken for calibration.** Policy violations are
  reported beside raw probabilities and never used to reshape them.
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
- **A tidier context than the assistant got.** The forecaster receives the
  recorded subject prompts verbatim inside the forecast wrapper, truncation
  and all. Cleaning them up measures an easier question.
- **Accuracy without a proper scoring rule.** Point-prediction accuracy
  rewards confident guessing. Report Brier for fully declared events and
  interval score with coverage and width. Never apply log loss to the sparse
  action list.
- **Malformed first shots silently retried away.** First-attempt validity and
  every repair call remain visible; conditional-on-valid scores are never
  shown without their coverage.
- **Imitation read as competence.** `next_action` scores agreement with the
  incumbent, not quality. Label the column.
- **Semantic recall read as proof.** Retrieval rank proves discoverability;
  claim/evidence lifecycle determines authority. Score the two separately.
- **Operating state reconstructed from durable memory.** Steps, failures,
  and pending proposals come from exact run/task rows whenever they exist,
  not from a stale episode summary.
- **Eval storage becoming indiscriminate memory.** Case snapshots are
  minimized, sensitivity/expiry propagate, and findings stay eval artifacts
  until reviewed promotion.
- **An incumbent-only corpus.** Every challenger is graded on imitating the
  model it would replace. Grow producer trajectories in isolated,
  fingerprinted environments, and use hermetic case bundles for paired model
  claims.
- **A citation that exists but does not support.** Id validation catches
  invented ids, not misattributed ones. Lexical overlap screens exact-value
  cases; it does not establish support.
- **A database mistaken for a sandbox.** Database identity, filesystem root,
  credentials, network, capability allowlist, clock, and per-case reset are
  all part of the isolation proof.
- **A sweep planned around imaginary cache hits.** Schedule repeats for
  locality, measure provider cache behavior, and budget as if every prefill
  misses.

## Considered and not taken

- **A large remote model as the semantic judge.** It would solve the scorer
  coverage problem in one step, and it cannot be used here. Grading an
  outcome forecast against the historical trace means sending recorded
  prompts, observations and operator content to a third party — the exact
  data boundary this document draws twice, once for the forecaster and once
  for the eval store. Providers are local (`providers/base.py`: Ollama, Jan,
  LM Studio), and "it's only offline" does not change where the bytes go;
  offline is when it happens, not whether it leaves the machine. A **local**
  judge is admissible, and it owes a debt the deterministic scorers do not:
  its agreement with the labelled subset must be measured before any verdict
  of its own is allowed to count. An unvalidated judge is a model grading a
  model, which is where this document started and declined to stay.

- **A new four-table memory subsystem.** Identity, operating state, evidence,
  and lessons are valuable authority classes, but RainBox already has
  specialized stores with richer lifecycle and provenance. Use the classes as
  a manifest/report overlay rather than flattening claims, evidence, task
  events, journals, and skills.
- **Treating a recalled memory line as its own receipt.** The retrieval event
  proves that the line was surfaced. Historical or factual claims still point
  through to `memory_evidence`, a source observation, or an operator
  confirmation.
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
- **Shadow-routing candidate models onto real turns to diversify the
  corpus.** It buys producer variety with the operator's actual replies, and
  an isolated producer sweep buys trajectory variety without changing their
  replies. It still processes copied operator requests and is governed as
  operator data.
- **Prefix-shaped ladder rungs.** Cheap, and they would put prompt sections
  in an order no real step saw. Fidelity is the instrument's only claim.
- **A full leave-two-out sweep.** Quadratic in blocks and almost entirely
  uninteresting. A targeted 2×2 on a suspected pair answers the same question
  for four cells.

## Implementation phases

This document describes a complete instrument. Nobody should build a complete
instrument. The phases below are ordered so that the cheapest one produces a
real number, each later phase is gated on the previous one having been worth
it, and the riskiest engineering arrives only after three chances to abandon
the idea.

Each phase names what it needs, what it can say, what it cannot say yet, and
the condition under which the right move is to stop.

### Phase 1 — execution-success calibration

The smallest thing that produces a number, and smaller than it first looks.
Conditional mode only: code hands the forecaster the recorded action and
arguments and asks what comes back. One model. No sweep, no ladder, no
ablation, no labels.

The scoreable core is **one probability against one recorded boolean**.
`OutcomeForecast.ok_probability` versus `observation.ok`, scored by Brier.
That needs no comparator, no unit normalization, and no JSON path — and
because every capability's observation carries `ok`, it works across the
**whole** action surface rather than a typed subset. An earlier draft of this
phase restricted it to capabilities returning JSON; that restriction belongs
only to outcome *content* scoring, and applying it to the calibration metric
throws away most of the corpus for no reason.

Content scoring rides along where it is free:

- `bounds` versus a numeric located in `observation.data` — interval
  coverage, on the subset where a capability-specific path to a number
  exists;
- `outcome` prose — **unscored in this phase.** It is stored, and read by a
  human when a result is surprising. Pretending otherwise would mean a
  semantic comparator, which is the thing Phase 1 exists to avoid.

Deliverable: `evals/forecast_bench.py` with a `--targets step_success` path,
one persisted `EvalRun`, and a printed Brier score with the base rate beside
it.

- **Needs:** the case builder and persistence below; nothing else.
- **Says:** whether a local model knows when a tool call is about to fail.
- **Cannot say:** anything about action choice, prompt stages, outcome
  content, or correctness.
- **Stop if:** no bound model beats the trivial baseline of always predicting
  `ok` at the corpus base rate. Cheap, decisive, and it means the premise is
  too weak to spend Phase 2 on.

#### The Phase 1 build sheet

Everything here is checkable against the schema today.

**Case eligibility.** One query over `assistant_step`: rows with
`phase='observed'`, a non-null `user_prompt`, a non-null `action`, and a
non-null `observation` JSONB carrying `ok`. Control rows, failed validations
and crash rows have no executed action and are excluded. Terminal actions
(`reply`, `ask_clarifying_question`) have no meaningful `ok` and are excluded
too — this phase is about tool calls.

**Persistence.** `case_type="tool_output"`, which the
`eval_case_case_type_check` constraint already permits and which
`notes/evals-design.md` names as an extension point. `run_eval_case` currently
drops it into the else-branch and scores 0.0 with
`unsupported case_type` — the seam is a new branch beside the `chat_reply`
and `memory_retrieval` ones at [evals/runner.py:285](evals/runner.py:285).
`split="holdout"`, following `monitor.py`'s precedent for rows that are
sampled rather than curated.

**Score normalization.** `EvalResult.score` is CHECK-constrained to
`[0.0, 1.0]`, so the per-case score is `1 - brier` (already in range for a
binary event) and every raw metric — Brier, base rate, coverage, width,
interval score, schema-validity and retry counts — lands in `details`
alongside the scorer version. Do not store a raw log loss anywhere in
`score`: it is unbounded and would violate the constraint.

**Prompt construction.** The recorded `system_prompt` and `user_prompt` are
embedded verbatim inside delimited subject-prompt sections under the narrow
forecaster system prompt, plus the recorded action and arguments as target
data. No section of either stored prompt is rewritten, reordered or
regenerated — the byte-comparison test is the acceptance criterion.

**What is not needed yet:** no ablation, no section surgery, no
`allowed_actions` catalog, no producer sweep, no sandbox, no labels, no
cross-model matrix, no TTL machinery beyond deleting the run.

### Phase 2 — the imitation benchmark

`next_action` top-1 and set membership across every bound model, with the
majority-class baseline, repeats, per-capability breakdown, and first-attempt
schema validity beside every score.

Deliverable: a scorecard table, one row per bound model, with top-1, set
membership, macro-F1, schema validity, the majority-class baseline, and
scored coverage.

- **Needs:** `allowed_actions` recovered per case from the historical
  catalog; the sweep ordered model-outermost and repeats-innermost;
  case-clustered intervals.
- **Says:** which model predicts this assistant's behaviour, where it stops
  doing so, and which models can hold a schema at all — a routing shortlist.
- **Cannot say:** whether any predicted choice was better than the recorded
  one. This is imitation.
- **Stop if:** every model sits at the majority-class baseline. The corpus is
  then too skewed to distinguish forecasters, and the fix is corpus
  composition, not more phases.

Phases 1 and 2 together need **no new prompt-assembly code**: they embed
stored prompts verbatim. That is why they come first — most of the value at
almost none of the risk.

### Phase 3 — the sensitivity ladder and single-block ablation

Where the real engineering starts, and the first phase that can be got subtly
wrong: removing prompt sections from a stored prompt while keeping every
surviving byte and its order intact.

Deliverable: the stage-damage table — movement rate per boundary against the
same-context control, plus single-block attribution.

- **Needs:** byte-faithful section surgery, the synthetic-variant labelling,
  repeats with the same-context control.
- **Says:** where an answer becomes sensitive to added context, and which
  single block controls the movement.
- **Cannot say:** whether any movement was an improvement. Everything here is
  unlabelled.
- **Stop if:** movement rates are indistinguishable from the same-context
  control. Nothing is being localized and the labels of Phase 4 have nothing
  to be spent on.

### Phase 4 — the labelled subset

Labels are the scarce resource, and Phase 3's ranking is what decides where
they go. Build cases only for the boundaries that ranked highest.

Deliverable: a labelled case set, and the first stage-damage table whose
movements are signed as improvement or regression.

- **Says:** improvement against damage, for the first time. Correctness
  claims start here and nowhere earlier.
- **Cannot say:** anything about the live guard.

### Phase 5 — guard-readiness simulation

Only reachable with Phase 4's labels. Produces the paired quality lift that
gates Part 2, and nothing about Part 2 should be built before it exists.

Deliverable: one number with a confidence interval — defect recovery minus
correct-answer regression, paired by case, against audit-only — and the
latency it cost.

### Deferred indefinitely

**Paired producer comparison and hermetic case bundles.** A frozen clock,
database fixture, workspace fixture and stubbed external responses per case
is a state-perfect replay harness, and it is not needed by anything above.
See [What the producer sweep is actually for](#what-the-producer-sweep-is-actually-for).

**Cascading redaction across the eval store.** Replaced by a TTL — see
[Security and prompt boundaries](#security-and-prompt-boundaries).

**Semantic scorers for capabilities that return prose.** Phase 1 covers the
typed ones and reports its coverage honestly; extending it is a phase of its
own with its own justification.

## Implementation seams

1. `ContextSourceDescriptor`, `AnswerForecast`, `ForecastClaim`,
   `QuantityBounds`, `ActionForecast`, `OutcomeForecast`, `StepForecast` and
   `ConditionalStepForecast` in an eval-owned
   `evals/forecast_models.py`. Nothing imports eval schemas into the assistant
   hot path. If the production guard later earns implementation, its shared
   contract moves to a neutral module in that proposal. Case-time validation
   owns the historical closed action set and source catalog.
2. A binding-only `answer_forecast` role, resolved through the same fallback
   pattern as `reply_audit` and `response_language_classifier`. No production
   switch is needed, because nothing runs in a turn.
3. An immutable case manifest with stored system/user prompt hashes, prompt
   revision, case-allowed actions, producer model, environment class and
   fingerprint, transcript cutoff, source descriptors and as-of lifecycle,
   scorer versions, sensitivity, retention, and expiry.
4. Context reconstruction from a recorded run: `cold` built only from
   messages preceding the subject turn; every later synthetic rung removes
   structurally identified sections from stored prompt text. The current
   `build_turn_prompts` include flags are fixture helpers, not historical
   reconstruction.
5. Source ids and code-owned descriptors on the shared context/observation
   projection — today
   `_build_reply_audit_prompt` emits `<observation action=… status=…>` with
   no ids, so this is new plumbing on an existing prompt builder. Memory claim
   descriptors join lifecycle metadata with their `memory_evidence` rows;
   retrieval telemetry remains a separate “was found” source.
6. `evals/forecast_ladder.py`: replay, repeats, ablation, persistence through
   `eval_run` / `eval_result`, structural leave-one-out, targeted pairs, and
   its two CLIs.
7. `evals/forecast_bench.py`: the model sweep ordered model-outermost and
   repeats-innermost, joint and conditional modes, the three scorer tiers with
   scored-coverage reporting (including retrieved-id set/rank metrics for
   `memory_query`), sparse-event Brier, interval score, continuity-policy
   invariants, authority compatibility, environment strata, and the
   cross-model matrix.
8. `evals/forecast_guard.py`: the paired audit-only versus
   forecast-assisted-audit simulation on held-out labelled terminal cases.
9. Prompt-sensitivity, authority-use, forecaster-scorecard, and
   guard-readiness reports, with case counts, generation counts, clustered
   intervals, control variation, environment fingerprints, scored coverage,
   and majority-class baselines.
10. A snapshot TTL sweep: expire embedded prompts, observations and forecast
    prose on the clock; leave scores, ids and hashes in place.

Producer-sweep isolation — per-case state reset, as-of fixtures, disposable
workspace, frozen clock, fail-closed preflight — is **not** a seam here. No
phase runs a producer sweep; the requirements stay documented under
[What the producer sweep is actually for](#what-the-producer-sweep-is-actually-for)
as the contract for if one is ever built.

Tests must prove — grouped by the phase that first needs them, so a phase's
list is a checklist and anything below the phase in progress is out of scope:

**Phase 1 — replay fidelity, isolation, and scoring hygiene**

- the subject run's trace is not modified by a replay;
- the embedded subject system and user prompts are byte-identical to the
  recorded pair;
- the assistant's own production prompt is byte-identical with the
  instrument present and absent;
- sentinels in `summary`, delivered reply, future room messages, and
  `active_call` never enter a forecast prompt;
- conditional mode supplies the recorded action and arguments and the
  forecaster's own action prediction is absent from its prompt;
- an unbounded metric never reaches `EvalResult.score`, which the schema
  constrains to `[0.0, 1.0]`; raw metrics land in `details`;
- a malformed first output remains counted after a later repair succeeds, and
  every conditional-on-valid metric carries validity coverage;
- confidence intervals cluster repeats by case rather than counting each
  decode as an independent task;
- a null `bounds` against a capability-typed numeric observation is counted
  as declined, while prose containing digits is not auto-classified numeric;
- reversed or incompatible-unit intervals fail scoring, and wide intervals
  pay through the interval score;
- the TTL sweep expires embedded prompts, observations and forecast prose
  while leaving scores, ids and hashes intact.

**Phase 2 — the imitation benchmark**

- a forecaster that always names the majority action scores at the printed
  baseline, not above it;
- an action outside `case.allowed_actions` is rejected even when it exists in
  the current registry, and a removed historical action remains scoreable;
- `top_probability <= set_probability`, candidate actions are unique, and the
  sparse action output is never passed to multiclass log-loss code;
- joint and conditional results are never pooled into one cell, and joint
  outcomes from action-mismatch cases are never compared to the recorded
  action's outcome;
- every table reports scored coverage, and an unscored capability is absent
  from the numerator rather than counted as passing.

**Phase 3 — section surgery**

- a rung's reconstructed prompt differs from its neighbour by exactly the
  sections that rung adds;
- ablation removes one block and nothing else;
- repeats with a fixed configuration produce a reported variance, and the
  report refuses to rank stages without one;
- claim comparison ignores wording.

**Phase 4 — the authority overlay, once claims carry evidence**

- invalid `source_refs` are surfaced as invented, not counted as support;
- a `memory_evidence` row whose provenance is `inferred_by_model` cannot
  satisfy an evidence-class citation however it is stored;
- a retrieved memory uuid can score as an exact retrieval outcome without
  being accepted as proof that the claim text is true;
- a reusable lesson cannot mechanically support "this action ran," while the
  corresponding append-only step/event row can;
- relabelling an execution event as a recommendation is surfaced by the
  labelled claim-role scorer rather than laundering an incompatible source;
- descriptor freshness is evaluated at the subject timestamp, and a later
  correction is retained as later evidence rather than rewriting the prompt;
- an exact scalar absent from its cited block is counted as a lexical
  mismatch rather than automatically labelled hallucinated;
- continuity checks reject a duplicate successful write, execution past a
  pending approval, and unchanged resubmission after a corrective error,
  while leaving ambiguous repeated reads for labels;
- an eval finding cannot create an active reusable memory or skill without a
  reviewed promotion carrying sources, owner, confidence, review date, and
  expiry.

**Phase 5 — guard readiness**

- guard simulation retains the original candidate, keeps the forecast answer
  out of the reviser prompt, and reports correct-answer regressions beside
  defect recovery.

**Deferred with their features** — producer-sweep isolation preflight, model
order not changing paired outcomes, and paired mode refusing an
unreconstructable as-of state. These belong with the producer sweep and are
not written until something needs it.

## The standard names for what this does

None of the machinery here is novel, and knowing what it is called is the
difference between checking the method against established practice and
re-deriving it badly. Four well-studied things, combined:

- **Calibration measurement.** Eliciting a probability or an interval and
  scoring it against resolved outcomes is what Brier and interval scores are
  for. The failure mode — a model asserting 95% and being right 60% of the
  time — is the standard one, and the remedy is a proper score on a fully
  declared event rather than accuracy alone.
- **Feature attribution by ablation.** Removing one input and measuring the
  change in a probe output estimates local sensitivity to that input. Its
  limitations are the ones recorded above: interactions, synthetic prompts,
  model dependence, and the need for repeats and labelled confirmation.
- **Supervised prediction over logged trajectories.** Predicting recorded
  actions and observations avoids live intervention and is reproducible under
  a frozen manifest. Its standard hazards are producer, selection, and state
  bias. This is **not off-policy evaluation**: there are no action
  propensities, counterfactual rewards, or support guarantees, so the
  benchmark cannot estimate how a replacement policy would perform.
- **Retrieval separated from provenance.** Semantic or lexical retrieval is
  an index for finding candidate context. Lifecycle state, evidence rows, and
  append-only events are the audit trail for deciding what the candidate can
  support. Retrieval rank is never promoted into source authority.

The document's own contribution is not the metrics. It is that this
assistant's runs retain the subject prompts, arguments and observations
needed to build these screens without adding instrumentation to the
production path — while keeping historical prediction, synthetic sensitivity,
and paired end-to-end evaluation visibly separate.

## What this proposal does not claim

It does not claim a model can certify itself, that two samples equal ground
truth, that semantic recall proves a remembered claim, or that a numeric
probability is a calibrated one.

It claims something narrower and testable: the assistant's prompt is built in
stages, its runs retain enough context to probe those stages offline, and
sealed repeated probes can screen which boundaries and blocks deserve
labelled end-to-end investigation. They do not identify damage without those
labels.

And one more, which costs nothing extra once the replay exists: recorded
actions, arguments, wrapper status and observations are real historical
targets. Predicting them produces an imitation/execution table; deterministic
typed outcomes and labelled cases add semantic evidence. Together they turn
“which local model deserves the next direct test?” from an impression into a
reproducible shortlist.
