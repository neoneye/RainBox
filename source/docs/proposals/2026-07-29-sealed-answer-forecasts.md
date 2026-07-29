# Sealed answer forecasts — give local models a second shot before sending

**Status:** Proposal. Nothing implemented.
**Date:** 2026-07-29

## Decision

Do **not** ship one forecast before every decide step as the first version.
That design roughly doubles the calls, adds latency throughout the run, and
still cannot improve the reply the operator receives.

Ship a **terminal answer guard** instead:

1. the normal decide call produces a candidate reply;
2. a second, candidate-blind call independently forecasts the answer from the
   request, constraints, and observations;
3. the existing `reply_audit` call compares the candidate, the independent
   forecast, and the evidence;
4. material, evidenced problems bounce through the existing bounded revision
   loop before anything is posted.

The first generation is a candidate, not a conclusion.

This is the useful version of sealed forecasts for RainBox. Local models often
miss on the first pass. Another answer-shaped sample can expose a brittle
guess, but agreement between two model calls is not truth. The audit must
settle disagreements against evidence, request verification when possible,
and treat unresolved uncertainty honestly.

The full context ladder — one forecast at every context boundary, resolved
after the run — remains valuable as an **eval and diagnostic mode**, where
its cost is intentional and its output can be compared with labelled
outcomes. It is not the production path.

## Naming

`calibration` is the natural word for this and it is **taken**:
`user_profile/calibration.py` renders the operator's self-declared knowledge
rows, injected as the `knowledge_calibration` block behind its own switch. A
second thing called calibration in the same prompt assembly is a maintenance
trap, not a naming quibble. `prediction` is accurate and silent about timing,
which is the whole mechanism. `guess` reads as an admission of sloppiness in
a trace the operator opens while troubleshooting.

Hence **`answer_forecast`** for the sealed independent answer and
**`forecast_resolution`** for the later comparison against an independent
outcome. *Resolution* is the term of art for settling a forecast, and it
collides with nothing in trace vocabulary — `resolve_model_group` and
`resolve_workspace_path` are code-level resolvers.

## Why a trace-only ladder is not the production path

Keeping a forecast out of the answerer's context is the sound part of the
idea. Reading causes off the resulting traces is where it overreaches, in
five specific ways — each of which also constrains the guard designed below.

### It diagnoses after the damage

Resolving a ladder after the message posts cannot help the turn that paid for
it. The operator gets the inaccurate first answer and the useful disagreement
is left in `/assistant`.

### The delivered answer is not ground truth

A forecast landing outside the delivered answer means only that two outputs
disagree. Calling that `answer_wrong` or `forecaster_error` requires an
independent outcome: a deterministic calculation, a cited observation, a
later user correction, a labelled eval case, or a human judgment. A local
model cannot manufacture ground truth by judging two of its own samples.

### Adjacent calls differ even when context does not

Local generation is stochastic. If `cold` and `post_language` disagree, the
language block may have moved the answer — or two samples from effectively
the same prompt may simply differ. Without a same-context repeatability
baseline, “the first divergent rung” does not localize a cause.

Likewise, an unchanged forecast does not prove a tool was worthless. The
observation may have increased confidence, ruled out an alternative,
strengthened a citation, or changed the explanation without changing the
headline answer.

### Likert containment is not calibration

A 1–5 confidence attached to a free-text answer space is inspectable, but it
cannot support calibration statistics. Calibration needs:

- a numerical probability;
- an independently resolved outcome;
- enough comparable cases to test whether, for example, claims made at 70%
  confidence are right about 70% of the time.

Without those three things, the trace shows confidence language, not
calibration.

### More calls can repeat the same error

The same weights, prompt framing, retrieved context, and tool mistakes create
correlated failures. “Ask it twice” is useful only as a disagreement detector.
It is not a correctness proof, and majority vote among correlated local
models is not a substitute for evidence.

These are not reasons to discard the mechanism. They define the mechanism it
needs to become.

## The production pipeline

The forecast runs only when the normal decide loop has produced a `reply`
candidate. It is created **after** the candidate exists in memory, but the
forecast call never receives that candidate. The seal is an information-flow
property, not a wall-clock claim. It is a fresh model request with no shared
conversation or generation state.

```text
1. request + constraints + observations
   └─► normal decide call ─► candidate reply (held, not posted)

2. request + constraints + observations
   └─► fresh answer-forecast call (candidate omitted) ─► independent answer

3. candidate + independent answer + constraints + observations
   └─► reply_audit ─► send
                 └─► revise ─► existing bounded correction loop
```

This reuses the seam implemented by
`2026-07-29-reply-audit-as-its-own-call.md`. It does not add a second audit
system or a new terminal action.

### Why generate the forecast after the candidate

Running before every decide step pays for forecasts when the next action is a
tool call rather than a reply. Running only after `reply` is selected makes
the cost one forecast per candidate, and the call can still be independent
because code controls its inputs.

The forecast sees:

- `current_request`;
- `reply_language_markdown`;
- the current `acceptance_criteria_json`, when enabled;
- relevant user settings and deterministic formatting guidance;
- the bounded profile facts actually supplied to the decide call, with
  code-issued source ids;
- the turn's step observations, with stable observation ids;
- the current local time when the answer depends on it.

It does **not** see:

- the candidate reply;
- decide-step `reason` fields;
- prior forecast text;
- audit verdicts;
- the action catalog, procedural skills, or unselected profile candidates.

The forecast needs the evidence and constraints required to answer. It does
not need the argument that produced the candidate or the capabilities used to
get there.

### What this placement cannot catch

The forecast is blind to the candidate but **not** to the run that produced
it: its evidence is the observations the decide loop chose to gather. If the
loop read the wrong board, converted with the wrong unit, or retrieved a
look-alike fact, the forecast inherits that evidence and independently agrees
with the candidate. Two calls, one poisoned premise, `agrees · sent`.

The guard therefore covers **wrong summary of right evidence**, not **wrong
evidence**. The only rung with the latter property is a `cold` forecast made
before any tool ran, because its prior was formed before the wrong read
existed — and that rung is exactly what this proposal moves out of
production.

That trade is accepted rather than hidden. A `cold` rung is cheap (almost no
context), but its disagreements are dominated by cases where the answer
legitimately depends on data the model could not have known, so in production
it would bounce good replies far more often than it catches bad reads. It stays in the
eval mode, where the label says which of the two happened. The consequence
for the guard's promise is stated plainly: `agrees` means the answer is
stable given the evidence, never that the evidence was the right evidence.

### One forecast per evidence revision

The first forecast is reused across wording-only revisions. Regenerating it
for every bounced draft would let the reference move toward the candidate and
would charge repeatedly for the same evidence.

Code computes an `evidence_revision` from the canonical request, effective
constraints, supplied profile facts, and visible observations, including
their ids and content digests. A new observation or a criteria refresh
changes that revision and permits one new forecast. A rewritten candidate
alone does not.

This gives the forecast a stable target while still allowing a verification
step to change the answer legitimately.

## The forecast is an independent answer, not merely its shape

Forecasting only “a positive quantity of roughly this order” can detect a
unit catastrophe, but it cannot catch the ordinary local-model failure: a
plausible, specific, wrong claim. The terminal guard therefore needs a concise
answer proposal plus checkable claims.

```python
class ForecastClaim(BaseModel):
    claim_id: str = Field(min_length=1, description=(
        "Stable id unique within this forecast, used by later resolution."))
    claim: str = Field(min_length=1, description=(
        "One material factual, numerical, or action-outcome claim in the "
        "independent answer. Keep it short and checkable."))
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
        "A concise independent answer to the request. Substance matters; "
        "polished delivery does not."))
    claims: list[ForecastClaim] = Field(min_length=1)
    unknowns: str = Field(min_length=1, description=(
        "Missing information that could materially change the answer, and "
        "the cheapest check that would resolve it. Say 'none known' only "
        "after checking the supplied evidence."))
```

Every string is required and non-empty. `reason` and `unknowns` remain
pressure valves beside constrained fields, following the lesson in
`2026-07-24-operator-locale-and-language.md`.

`source_refs` are data, not decoration. Code issues every id — `request`,
`criteria`, `observation:4` — and marks unknown ids as invented rather than
silently treating them as support.

`unsupported` is one of those **code-issued ids**, present in the allowlist
the prompt hands the model, not a magic string the validator special-cases.
That distinction is the difference between a typed value and prose parsing: a
sentinel outside the id space invites `none`, `None`, `n/a`, and a validator
that grows a synonym table. It is valid and important — it lets the model
state an explicit prior while admitting the run did not verify it.

Today's `_build_reply_audit_prompt` emits observations as
`<observation action=… status=…>` with **no ids**, so the shared evidence
projection has to grow them. That is a change to the existing audit prompt,
not only to the new forecast prompt — see the switch-off note below.

The 0–100 probability does not make the model calibrated by itself. It makes
calibration measurable later, but only for claims with independent outcomes.

### An evidence ceiling on certainty

A stated probability is a model's opinion about its own opinion, and local
models put 95 on priors they invented. Code owns the ceiling instead: the
strongest `source_ref` class a claim cites bounds how certain that claim is
allowed to be — a deterministic check supports near-certainty, a tool
observation less, an `unsupported` prior much less. A claim asserting
certainty above its ceiling is a bounce the auditor does not have to be
clever enough to notice.

The ceiling constrains the **decision, never the recorded value.** Clamping
the stored probability would make every later reliability plot a measurement
of the clamp: buckets fill at the ceiling, the curve bends to meet it, and
the resulting picture says the model is well calibrated because code made it
so. So both numbers persist — what the model claimed and what its evidence
allowed — and the calibration corpus reads the claimed one.

The specific thresholds are eval configuration, not design. Any number
written here would be invented, and inventing it in a design document is how
it survives unexamined into production.

No worked example belongs in the system prompt. Smaller models copy example
content into unrelated structured output. Field descriptions state the form,
and eval fixtures carry the concrete cases.

## The seal

The candidate-producing model must never receive raw forecast content.

The forecast is available only to:

- its own trace row;
- `reply_audit`;
- post-run evaluation and aggregation.

It is excluded from:

- the scratchpad;
- room messages;
- the run summarizer's narrative input;
- later decide prompts;
- future conversation history.

When the audit bounces a candidate, the answerer receives only a short,
evidence-backed problem statement. It does not receive “the forecast says
X.” This prevents correction by blind copying and keeps the independent
sample independent.

The implementation invariant should be tested by building every downstream
decide prompt and asserting that a unique sentinel placed in
`proposed_answer` and each forecast claim is absent.

## Turn disagreement into an evidence question

The forecast is not an authority. It is a source of hypotheses for the
auditor.

Extend the existing audit problem shape:

```python
class ReplyProblem(BaseModel):
    category: Literal[
        "unsupported_claim", "evidence_mismatch", "calculation",
        "missing_answer", "constraint", "language", "uncertainty",
    ]
    severity: Literal["minor", "major", "critical"]
    problem: str = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)
    repair: Literal["rewrite", "verify", "clarify"]


class ReplyAudit(BaseModel):
    reason: str = Field(min_length=1)
    forecast_relation: Literal[
        "agrees", "material_disagreement", "not_comparable",
    ]
    problems: list[ReplyProblem]
    verdict: Literal["send", "revise"]
```

The audit rules are:

1. Compare candidate and forecast claim by claim, not by wording.
2. Resolve a disagreement from observations or deterministic constraints when
   possible.
3. Never prefer the forecast merely because it is called a forecast.
4. When neither side is supported, report uncertainty and ask for the
   cheapest useful verification; do not invent a winner.
5. Do not bounce stylistic differences unless they violate an established
   formatting or language constraint.
6. A `major` or `critical` evidenced problem requires `revise`.

Rule 6 is the one rule the model does not get to apply. `severity` and
`verdict` are two controls over the same decision, and a model that returns
`verdict="send"` beside a `critical` problem has stated both. **Code
disposes:** the effective verdict is `revise` whenever any problem carries
`major` or `critical` with at least one valid evidence ref, whatever the
model wrote. The stated verdict is kept in the trace as the disagreement it
is, and a run that produces them repeatedly is a finding about the audit
prompt.

Recording the contradiction and shipping anyway would be the prose-parsing
trap wearing a typed field: the type says the values are authoritative, and
then the authority is decided by whichever one the reader happens to trust.

`evidence_refs` are validated against the prompt's source ids. A reference to
the forecast alone is not evidence. The corrective text passed to the decide
loop includes the problem and the underlying request/observation refs, but
not the forecast's proposed answer.

### Keep the auditor's context small

The auditor already carries the request, criteria, language block, settings,
formatting guide, and observations capped at
`REPLY_AUDIT_MAX_OBSERVATION_CHARS = 2_000`. Adding a whole second answer to
that is the "lost in the middle" risk arriving by design, and a local auditor
that drowns rubber-stamps the candidate — the exact failure the guard exists
to prevent.

So the audit prompt receives the forecast's **`claims` only**, not its
`proposed_answer` prose. The claims are already the decomposition a
claim-by-claim comparison needs; the prose is a delivery of the same content
that the auditor is explicitly told not to compare on wording. It stays in
the trace, where the operator can read what the second answer actually said.

The symmetric move — having the candidate emit its own typed claims so the
auditor compares two structured lists — is **rejected**. That means a second
argument on `reply`, and `2026-07-29-reply-audit-as-its-own-call.md` removed
the second argument on `reply` for reasons that apply here unchanged: one
generation producing prose plus a faithful structured decomposition of that
prose has no way to guarantee the decomposition describes what it actually
wrote, and the codebase spent four layers of enforcement learning that. The
auditor decomposing the candidate itself is a checking task with the
candidate in front of it, which is the job it already has.

### Repair means more than rewording

An inaccurate first draft is not always fixed by asking for better prose.

- `rewrite` means the evidence is already present and the candidate stated it
  incorrectly or incompletely.
- `verify` means the loop should perform the smallest read or deterministic
  calculation that can settle the claim, then create a new
  `evidence_revision`, forecast, and candidate.
- `clarify` means the missing fact belongs to the operator and cannot be
  recovered from available context or tools.

The existing audit bounce returns control to the decide loop, so these repair
types are guidance rather than a parallel dispatcher. The decide model still
selects the available capability, but the trace can now distinguish
productive verification from endless rewriting.

### Rejected claims accumulate for the rest of the turn

A bounce hands the model a problem and hopes. Local models answer a "this
claim is unsupported" note by rewording the claim, which satisfies nothing
and costs the next audit. The loop has to remember refusals, not just report
them.

The codebase already does this one rung down: `failed_actions` holds an
`action:sorted-args` signature set, and an exact resubmission is refused
rather than replayed. The same shape applies to claims — every problem the
auditor raises stays attached to the turn and is re-sent with **every**
subsequent bounce, not replaced by the latest audit's findings. Two rewrites
of the same unsupported figure then face both refusals, and a model that
cycles between two bad claims stops being able to alternate its way past a
one-shot memory.

Two limits, stated rather than designed around:

- **A rejected claim cannot be quoted back safely.** Re-injecting the exact
  wrong figure to forbid it puts the wrong figure in the context — trap 1 of
  `2026-07-24-operator-locale-and-language.md`, where naming a thing to
  exclude it is how it gets emitted. The accumulated entry therefore carries
  the problem and its evidence refs, which is what the corrective text
  already carries, and the enforcement stays with the auditor rather than
  with a negative instruction.
- **Claim identity across drafts is fuzzy.** Actions have a canonical
  signature; prose claims do not, so this cannot become a code-side exact
  match the way `failed_actions` is. It accumulates evidence for the auditor,
  and it does not mechanically block anything.

That second limit is why this is a mitigation and not a fix. A hard
mechanical block on repeating a claim needs claim identity the system does
not have, and inventing a fuzzy matcher to get it would be a worse bug than
the one it closes.

## Bounded failure behaviour

There are two different failures and they should not share one policy.

### Infrastructure or parsing failure: fail open

If no forecast model is bound, the call times out, or structured parsing
fails, run the existing reply audit without a forecast. If the auditor also
fails, preserve today's fail-open behaviour and send the candidate. The trace
records `guard_unavailable`; an unavailable helper must not make the
assistant produce no answer.

### A known substantive defect: do not silently call it success

If the auditor repeatedly finds a major or critical problem, the current
“ship after the rejection cap” behaviour may still be needed for liveness,
but it must be explicit:

- record `forced_send=true`;
- retain the last unresolved problems in the terminal trace row;
- disclose the unresolved uncertainty to the operator;
- never repeat a mutating action merely to repair its confirmation message.

The disclosure is **code-composed, not model-composed**. Asking the final
candidate to state its own unresolved uncertainty requires one more
generation, which is precisely the generation the rejection cap just refused
— and it asks the model that failed to fix the problem to describe it
honestly instead. Code appends a deterministic operator-facing line naming
the unresolved problem's category and count, in the room's existing
`kind="notice"` idiom, from the audit's typed fields. Unfakeable, free, and
already the pattern for operational output that is not conversation.

The initial implementation keeps `MAX_AUDIT_REJECTIONS` and the existing
loop. A release gate below limits how often forced sends may occur. If local
models cannot produce an uncertainty-aware fallback reliably, that is a
reason not to enable the guard yet, not a reason to hide the unresolved
defect.

## Model binding and diversity

Add a binding-only `answer_forecast` role on `/agentmodel`, resolved through
the same fallback pattern as `reply_audit` and
`response_language_classifier`.

There is no universally correct default beyond preserving availability:

- **same model group** measures whether the answer is stable under a
  purpose-separated second attempt;
- **different local model family** reduces some correlated errors and is the
  preferred experiment when more than one capable local model is available;
- **stronger local auditor** is usually more valuable than a stronger prose
  generator, because the auditor decides whether disagreement matters;
- **smaller or cheaper model** is acceptable only when evals show that it
  does not rubber-stamp or create false bounces.

Every forecast and audit row records the exact model, prompt revision,
sampling configuration, duration, and usage. Without that provenance,
same-model and cross-model results are not comparable.

Temperature is not a diversity strategy by itself. Raising it can create
more disagreement without creating more truth. Prefer role separation,
evidence access, and model-family diversity; treat sampling changes as an
eval variable.

## Latency, and why parallelism is not free here

Two extra generations in front of a reply is the guard's worst property, and
the obvious cure does not work as cleanly as it looks.

**The seal permits concurrency; the control flow does not.** The forecast may
run beside the candidate — the seal is information flow, not wall clock. But
the decide call returns `{action, args}` in **one** structured response: the
choice to reply and the reply text arrive together. There is no moment where
code knows a reply is coming and has not yet paid for it. Firing the forecast
concurrently therefore means firing it *speculatively*, before knowing the
step terminates — and a forecast fired at a step that turns out to be a tool
call is wasted, because the observation it returns changes
`evidence_revision`. Speculating on every step reproduces exactly the `2 + n`
cost the terminal placement exists to avoid. Parallelism here buys latency
with tokens; it does not create either.

**Bounded speculation is the middle.** Code already knows when the loop is
probably finishing: after any successful write the scratchpad steers toward
`reply`, and a step whose observation resolved the request's last unknown is
the common terminal shape. Speculating only at those points bounds the waste
to roughly one forecast per run while removing the latency from the turns
most likely to pay it. This is a knob, not a default — measure the hit rate
before enabling it.

**On this hardware the ceiling is lower than it looks.** Providers are local
servers (`providers/base.py`: Ollama, Jan, LM Studio), so two concurrent
requests to one instance contend for one GPU: they interleave rather than
overlap, and wall-clock savings approach zero. Concurrency pays only when the
forecast is bound to a model on a *different* server with its own capacity —
which is the same configuration the diversity argument above already prefers,
for unrelated reasons. Note the coincidence, and do not build the scheduler
until a second server exists to schedule onto.

`llm/__init__.py` wraps each call in its own `asyncio.run`, so there is no
multi-call concurrency helper today. Speculation needs one. That is real work
and it is not in the smallest coherent implementation below.

## Not every turn deserves a guard

The cheapest call is the one not made. Before running a forecast at all, code
decides whether the turn carries epistemic risk, using facts it already has:

- did any step produce an observation, or is the reply pure conversation?
- did the run compute (`python_run`), read (`memory_query`,
  `kanban_read`, `workspace_read_command`), or write?
- does the candidate contain a number, a date, a quantity, or a proposal?

"Thanks, that worked" needs no independent answer, and paying a forecast plus
a larger audit for it is the feature's worst look. A deterministic gate is
preferred over a small classifier model for the ordinary reason in this
codebase — **models propose, code disposes** — and for a specific one: a
classifier that decides whether to spend a call costs a call, so it only pays
if it is much cheaper than what it skips, which a local model rarely is
against a narrow forecast. If the deterministic gate proves too blunt in
evals, a classifier is the escalation, not the starting point.

The gate's decision is recorded on the run so a skipped guard is visible
rather than indistinguishable from a disabled one.

## Cost and switch

The production path adds one narrow structured call per evidence revision.
The existing `reply_audit` call remains the adjudicator; there is no new
resolver after the message posts.

Most turns therefore pay:

- normal decide calls;
- one answer forecast when a candidate reply exists;
- the already-existing reply audit;
- an additional decide/forecast/audit cycle only when a material problem
  bounces.

The feature has an immediate per-turn benefit but still needs a default-off
switch while measured:

```python
"assistant.answer_forecast": Setting(
    "assistant.answer_forecast", None, "bool", False,
    description="Before a candidate reply is sent, generate one sealed "
                "independent answer from the request, constraints and "
                "observations, then let reply_audit use disagreements to "
                "request evidence-backed revision or verification. The "
                "candidate-producing model never sees the forecast. Adds "
                "one local-model call per evidence revision. Default off.",
),
```

One switch is enough. The full context ladder belongs in the eval harness,
not behind another production setting.

### What the switch does and does not gate

The switch gates the forecast **call**. It cannot gate two things that reach
the audit model regardless:

- the extended `ReplyProblem` — `category`, `severity`, `evidence_refs` and
  `repair` are schema field descriptions, and a structured call's schema is
  part of what the model is shown;
- the evidence ids added to the shared observation projection.

So a switched-off run is **not** byte-identical to today's behaviour, and a
test asserting that would fail correctly. `assistant.acceptance_criteria`
could promise byte-identity because it only ever *adds* a section; this
feature edits a prompt that already ships.

Two ways out, and the cheap one is right. Gating the extended schema behind
the switch means maintaining two `ReplyProblem` shapes and two prompt paths
for a live call — the exact duplicate-source-of-truth shape that sank an
earlier attempt in `2026-07-24-operator-locale-and-language.md`. Instead,
land the schema extension and the evidence ids as **their own change,
unswitched, measured on their own** against the current audit before the
forecast exists. A richer problem shape is independently useful — `repair`
alone distinguishes productive verification from rewriting — and it makes the
audit-only baseline in the eval plan an honest control rather than a
different codebase. The switch then gates exactly one thing: whether a second
answer gets generated.

The corresponding test is `switch off ⇒ no forecast call, no forecast row,
and an audit prompt containing no forecast section` — a property about the
forecast, which is what the switch actually controls.

## Trace and inspector

For each candidate cycle, record:

1. `answer_forecast` — observed, collapsed by default;
2. `reply_audit` — observed, with relation, problems, and verdict;
3. the bounced candidate or terminal reply using the existing rows.

The run header shows a compact guard result:

- `agreed · sent`;
- `disagreed · verified · revised`;
- `disagreed · revised from existing evidence`;
- `uncertain · clarification requested`;
- `guard unavailable · sent unaudited`;
- `forced send · unresolved major issue`.

This is more actionable than an inside/outside ladder strip. The details
remain in the rows, while the header answers whether the second shot changed
what the operator received.

Markdown export includes the guard summary and rows. `/chat` includes none of
the internal forecast text.

## Resolution and calibration

Keep **comparison** separate from **resolution**.

- `forecast_relation` compares the forecast with the candidate. It can be
  computed on every guarded turn, but says nothing by itself about
  correctness.
- `forecast_resolution` exists only when an independent source settles a
  claim.

```python
class ClaimResolution(BaseModel):
    claim_id: str
    outcome: Literal["correct", "incorrect", "indeterminate"]
    source: Literal[
        "deterministic_check", "tool_observation",
        "user_correction", "eval_label", "human_review",
    ]
    evidence_ref: str = Field(min_length=1)


class ForecastResolution(BaseModel):
    resolutions: list[ClaimResolution] = Field(min_length=1)
    note: str = Field(min_length=1)
```

The delivered reply is deliberately absent from the `source` enum. It is the
thing under evaluation.

Calibration dashboards exclude `indeterminate` claims and show sample counts
beside probability buckets. Brier score or reliability plots become
meaningful only after enough independently resolved claims exist. Until then,
show raw resolved cases and do not label the feature calibrated.

## The full context ladder, demoted to an eval

The original rungs remain useful when investigating prompt construction:

| Rung | Additional context |
|---|---|
| `cold` | request and selected operator transcript |
| `post_language` | ranked reply-language block |
| `post_criteria` | acceptance criteria and settings-derived guidance |
| `step_1` … `step_n` | one additional observation per rung |

The eval must add a **same-context control**. For each rung transition under
study:

- sample the lower rung at least twice;
- sample the higher rung at least twice;
- compare within-rung variation with between-rung variation;
- score both against a labelled or deterministically checked outcome.

Only a between-rung improvement larger than normal same-prompt variation is
evidence that the added context helped. A content change is not automatically
a regression, and textual equality is not the metric; score material claims,
uncertainty, and the resolved answer.

The ladder can still find:

- a language block that improperly changes answer substance;
- criteria that resolve an ambiguity correctly or inject a bad assumption;
- an observation that corrects a prior;
- an observation the model ignores despite containing the answer;
- a late tool result that makes confidence worse rather than better.

Cause attribution is made by the eval label or a human reviewing the
transition. Do not ask a resolver model that lacks the observations to choose
among `context_missing`, `context_misleading`, and `forecaster_error`; that
would be confident guessing about guessing.

## Evaluation plan

The key measurement is not forecast agreement. It is **quality lift from
first candidate to delivered answer**.

Build a corpus with:

- exact calculations and unit conversions;
- factual questions whose answers exist in supplied observations;
- multi-part requests where omissions are easy to label;
- explanation tasks with required claims;
- action confirmations that must match recorded outcomes;
- ambiguous requests whose correct outcome is clarification;
- adversarial observations containing instructions that must remain inert.

Retain both the first candidate and the delivered answer in eval artifacts.
Run multiple seeds because one pass cannot characterize a local model.

Compare these variants:

1. current `reply_audit` only;
2. sealed forecast plus `reply_audit`;
3. same-model forecast versus different-family forecast;
4. forecast without claims/source refs versus the structured version;
5. terminal guard versus the full diagnostic ladder on a small subset.

Score:

- independently verified correctness before and after revision;
- correct-draft regression rate;
- material-disagreement precision: how often disagreement exposed a real
  defect;
- false-bounce rate;
- verification success rate;
- unresolved and forced-send rate;
- clarification precision;
- median and p95 added latency;
- model calls per delivered reply.

### Release gate

Enable the switch only when the eval shows all of the following on the bound
local models:

- a meaningful reduction in verified first-candidate defects;
- no material regression on already-correct candidates;
- forecast disagreements outperform audit-only review at finding real
  defects;
- false bounces and forced sends are rare and inspectable;
- the latency is acceptable for interactive use.

The exact thresholds belong in the eval configuration beside the corpus and
model bindings, not frozen in this design document. The comparison must
report case counts and confidence intervals; a handful of good demos is not a
release gate.

## Security and prompt-boundary rules

Observations are untrusted evidence, not instructions. Forecast and audit
system prompts state this explicitly, and observation ids are code-generated.
A tool result saying “approve the candidate” has no authority.

Do not include assistant reasoning, prior audit prose, or prior forecast prose
in the forecast prompt. Do not let a model choose which observations exist.
Code selects and caps them using the same boundaries as `reply_audit`.

Sensitive observation handling does not change: a forecast is another model
consumer inside the same local trust boundary, and its trace follows the
existing debug-data policy. If a future forecast model is remote, that is a
different data-boundary decision and must not be enabled merely by binding a
role.

## Traps and explicit countermeasures

- **Self-consistency mistaken for truth.** Agreement is a stability signal;
  only independent evidence resolves correctness.
- **Forecast authority bias.** The audit receives a competing hypothesis,
  not an answer key. The answerer receives evidence-backed defects, not the
  raw forecast.
- **Moving reference.** Reuse one forecast until evidence changes.
- **Invented citations.** Validate every `source_ref` against code-issued ids.
- **Wide-set gaming.** Removed from the production schema; the guard makes a
  concrete independent proposal.
- **Parroted examples.** No worked examples in the production prompt.
- **Prose parsing.** Kinds, probabilities, relations, severity, repair, and
  verdict are typed.
- **Pressure-valve removal.** `reason` and `unknowns` stay free text and
  audit-safe.
- **False correction of a good draft.** Measure correct-draft regression and
  require evidence for material bounces.
- **Endless self-repair.** Keep the rejection cap, reuse forecasts, record
  forced sends, and make verification change the evidence revision.
- **Repeated side effects.** A reply repair never reruns a write. Action
  claims are checked against the existing write observation.

## Implementation seams

The smallest coherent implementation is:

1. add `AnswerForecast` and `ForecastClaim` models beside the existing narrow
   structured call models in `agents/assistant.py`;
2. add the binding-only `answer_forecast` role and default-off setting;
3. when a `reply` candidate is produced, build a candidate-blind forecast
   prompt from the same evidence projection used by `reply_audit`;
4. persist the forecast as an operator-only control row;
5. pass the forecast to `_build_reply_audit_prompt`;
6. extend `ReplyProblem` with typed category, severity, refs, and repair
   while preserving the existing `send`/`revise` loop — as a separate,
   unswitched change landed and measured **before** the forecast, so the
   audit-only baseline is a control rather than a different codebase;
7. cache by `evidence_revision` across wording-only bounces;
8. ensure the summarizer, transcript builders, and scratchpad renderers skip
   forecast rows;
9. add inspector labels and the guard summary;
10. extend `evals/profile_guidance.py` with candidate-versus-delivered
    scoring and the ablations above.

Tests must prove:

- the candidate and decide reasoning are absent from the forecast prompt;
- forecast sentinels never enter later decide prompts, room history, or the
  summarizer;
- the audit sees the candidate, forecast, constraints, and observations;
- invalid source refs are exposed rather than accepted;
- wording-only revision reuses the forecast;
- new evidence invalidates it;
- failure falls back to the existing audit;
- mutating actions are not repeated;
- every bounce, forced send, and model identity is traceable;
- a `major` or `critical` evidenced problem bounces even when the model
  wrote `verdict="send"`;
- with the switch off, no forecast call is made, no forecast row is written,
  and the audit prompt carries no forecast section.

## Considered and not taken

- **A hostile-fact-checker persona on the forecast.** The forecast's job is
  to answer the request independently; a persona pushed toward disagreement
  produces disagreement, which inflates the bounce rate and corrupts the
  false-bounce metric that decides whether the feature ships. The adversarial
  role already exists and it is the auditor. Role separation and model-family
  diversity stay; adversarial framing of the answerer does not.
- **A background ladder that corrects the next turn.** Running the diagnostic
  ladder asynchronously after a send and injecting "the last message was
  wrong" into the next turn's scratchpad assumes the ladder can detect a
  hallucination — which is the claim this entire proposal declines to make.
  Model-versus-model disagreement is not ground truth after the message posts
  any more than before it. Sampling production turns into the eval corpus in
  the background is fine and cheap; acting on the result unsupervised is not.
- **Writing unresolved conflicts into persistent memory.** A forced send is a
  real signal and losing it at turn end is a real gap. But the assistant
  writing its own unverified uncertainty into memory is what the memory trust
  model exists to prevent — `memory_remember` deliberately creates an inert
  candidate pending operator confirmation, and a `ConflictedBelief` written
  straight through would be a self-generated claim with no operator in the
  loop. The forced send is already durable in the run trace. Carrying it
  forward is a follow-up that belongs in `memory-architecture.md`'s vocabulary
  and goes through the candidate path, not a field this proposal adds.

## What this proposal does not claim

It does not claim that a model can certify itself, that two local samples equal
ground truth, or that confidence becomes calibrated merely because it is
numeric.

It claims something narrower and testable: a candidate-blind second answer,
adjudicated against evidence before delivery, can give imperfect local models
a useful second shot. RainBox should ship it only if the retained first
candidate proves that the second shot makes the delivered answer better.
