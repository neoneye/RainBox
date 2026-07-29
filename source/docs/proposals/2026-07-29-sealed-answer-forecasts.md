# Sealed answer forecasts — locating the pipeline stage that costs the most

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

Two questions, two instruments:

- **When did the answer go wrong?** The ladder: forecast at each context
  boundary, in the order the pipeline adds them.
- **Which block made it go wrong?** The ablation: rebuild one recorded prompt
  with a single block removed, and forecast again.

The ladder finds the stage. The ablation names the culprit. The ladder alone
cannot — context arrives in a fixed order, so the rung where the answer moves
is confounded with everything that arrived at that rung.

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

### Two-tier ground truth

Labels are the scarce resource, so spend them where the instrument points.

**Tier 1 — screening, unlabelled, cheap.** Replay every recorded run. Count
where answers move. This needs no ground truth at all, because "the answer
changed at the `settings` rung in 40% of runs" is a fact about the pipeline
regardless of which version was right.

**Tier 2 — confirmation, labelled, expensive.** Take the boundaries tier 1
ranks highest and build labelled cases for those specifically: exact
calculations, questions answered by a supplied observation, multi-part
requests with labellable omissions, ambiguous requests whose correct outcome
is a clarifying question. Now a rung transition can be scored as improvement
or damage rather than movement.

Screening finds candidates; only tier 2 assigns blame. Reporting a tier-1
count as a defect rate is the mistake this structure exists to prevent.

## The report

The deliverable is not a per-run strip. It is a ranking over many runs:
**which pipeline stage most often precedes a wrong answer.**

Per stage boundary, across the corpus:

- how often the answer's claims changed;
- how often the change was an improvement, a regression, or unlabelled;
- movement rate against the same-context control, so noise is visible;
- the ablation result: how often removing the block changed the answer;
- how often the block was cited in `source_refs` at all.

That last row is the cheap embarrassment. A block that is never cited, never
moves an answer, and whose removal changes nothing is occupying a guidance
budget its neighbours are competing for.

An ordered list of stages by damage is what "where are the problems" means
operationally, and it is the artifact that justifies the whole exercise.

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

Replaying a run re-issues forecasts against reconstructed contexts and
persists them; it posts nothing, touches no room, and writes nothing to the
run being analyzed. Forecast rows are stored against the analysis, not
appended to the subject's trace — a diagnostic must not mutate its
specimen, and a later replay must not read a previous replay's output as if
the original run had produced it.

The model used for forecasting is bound separately, and unbound falls back to
the assistant's group. Same-model replay measures the *context* boundary,
which is the question being asked; a different model measures the capability
gap between two models instead, which is a different and also interesting
question, but the report must never mix them. Every forecast row records the
model, prompt revision, sampling configuration and duration, or same-model
and cross-model results are not comparable.

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

1. `AnswerForecast` and `ForecastClaim` beside the existing narrow structured
   models in `agents/assistant.py`.
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
   `eval_run` / `eval_result`, and the two CLIs above.
7. The stage-damage report, with sample counts and the control's variation
   beside every rate.

Tests must prove:

- the subject run's trace is not modified by a replay;
- a rung's reconstructed prompt differs from its neighbour by exactly the
  sections that rung adds;
- ablation removes one block and nothing else;
- invalid `source_refs` are surfaced as invented, not counted as support;
- claim comparison ignores wording;
- repeats with a fixed configuration produce a reported variance, and the
  report refuses to rank stages without one.

## What this proposal does not claim

It does not claim a model can certify itself, that two samples equal ground
truth, or that a numeric probability is a calibrated one.

It claims something narrower and testable: the assistant's prompt is built in
stages, its runs are recorded in full, and asking a sealed question at each
stage boundary of a recorded run tells you which stage is costing the most —
which is the thing nobody can currently see, and the thing every prompt
change is currently guessing at.
