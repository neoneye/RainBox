# Sealed answer forecasts — calibrating the assistant against itself

**Status:** Proposal. Nothing implemented. The mechanism is pure
instrumentation: it changes no reply, blocks no step, and its entire output
is trace rows nobody in the loop can read.
**Date:** 2026-07-29

## Naming

The concept is a guess about the final answer, made before the answer exists,
kept out of sight until it does, then scored against it.

`calibration` is the natural word and it is **taken**: `user_profile/
calibration.py` renders the operator's self-declared knowledge rows, injected
as the `knowledge_calibration` block behind its own switch. A second thing
called calibration in the same prompt assembly would be a maintenance trap,
not a naming quibble. `prediction` is accurate and says nothing about when it
was made — the timing is the whole mechanism. `guess` reads as an admission
of sloppiness in a trace the operator reads while troubleshooting.

Chosen: **`answer_forecast`** for the sealed guess, **`forecast_resolution`**
for the comparison afterwards. *Resolution* is the term of art for settling a
forecast against the outcome, and it does not collide with anything here —
`resolve_model_group`, `resolve_workspace_path` are code-level resolvers, not
trace vocabulary.

The word *answer* in the name is load-bearing. See
[One rung, one target](#one-rung-one-target): every forecast in a run
predicts the same thing — the final answer — and never the next action.

## The idea, in this codebase's vocabulary

Before the assistant knows anything, ask it what the answer will look like.
Not the answer — its **shape**:

- a quantity gets a lower and an upper bound, with units;
- a place gets two to four named candidates;
- a computation gets the class of outcome — sign, order of magnitude, units,
  whether it is finite at all.

Seal it. The forecast goes to the trace and nowhere else. Ask again at each
point where the assistant has learned something, so the guesses form a
ladder. When the reply is finally posted, compare the whole ladder to it and
find **the earliest rung that was already wrong**.

That rung is the answer to a question this system currently cannot ask:
*where did it start going wrong?* Today a bad reply is a bad reply. The trace
shows six steps and a message; nothing separates "the request was ambiguous
from the first character" from "the profile block pointed the wrong way" from
"the third tool read said something surprising and it never recovered." The
ladder separates them, because each rung sees exactly one more class of
context than the rung below it.

## The context ladder

The rungs are placed at **context boundaries, not time intervals**. This is
the design's one non-negotiable rule: if two adjacent rungs see the same
context, the higher one is worthless, because a divergence between them
names nothing.

| Rung | Fires | What it has that the rung below does not |
|---|---|---|
| `cold` | before the response-language classifier, before skill retrieval, before step 0 | the operator's message and the room transcript — nothing else |
| `post_language` | after the classifier, before acceptance criteria | the ranked `reply_language_markdown` |
| `step_1` | before the first decide call | user settings, formatting guide, knowledge calibration, the profile block, the skill block, the acceptance criteria, the action catalog |
| `step_2` … `step_n` | before each subsequent decide call | one more step's observation in the scratchpad |

Two of these earn their place before a single run has been recorded.

**`cold` → `post_language` should never diverge.** Language is delivery, not
content: which language the reply is written in cannot change what the
correct answer *is*. If that rung moves the forecast's substance, the
language block is steering content, which is a defect in a block whose whole
justification is that it does not. The mechanism makes that claim falsifiable
on day one, for the price of one extra call.

**`step_n` → `step_n+1` not moving is a tool-value metric, for free.** A read
that leaves the forecast exactly where it was is a read that told the
assistant nothing. Six steps of unchanged forecast followed by a reply is a
run that could have been one step. Nobody has to instrument for this
separately; it falls out of the ladder.

### One rung, one target

Every rung forecasts **the final answer**. Not the next action, not the next
observation, not the plan. This is what makes rungs comparable, and a ladder
whose rungs measure different things cannot localize anything — the
divergence you find is just the point where the question changed.

It is tempting to have `step_n` predict the next tool result, because that is
the thing it is about to learn and the prediction would be sharper. Resist
it: sharper rungs that measure different quantities are strictly worse than
blunt rungs that measure one.

## The forecast

```python
class AnswerForecast(BaseModel):
    """A guess at the final answer, made before it exists and sealed until
    it does. Never enters a prompt the deciding model reads."""

    reason: str = Field(min_length=1, description=(
        "Brief, audit-safe note on the evidence this forecast rests on. "
        "This is not hidden chain-of-thought."))
    kind: Literal[
        "quantity", "place", "computation",
        "explanation", "action", "refusal",
    ] = Field(description=(
        "The form the true answer will take, which decides how "
        "answer_space is written."))
    answer_space: str = Field(min_length=1, description=(
        "The set of answers the true answer should fall inside, written in "
        "the form this kind takes. A quantity: a lower and an upper bound "
        "with units, wide enough that you would be surprised to be outside "
        "and narrow enough that being inside means something. A place: two "
        "to four named locations. A computation: the shape of the outcome — "
        "its sign, its order of magnitude, its units, whether it is finite. "
        "An explanation: the claims the answer must contain to be correct. "
        "An action: the capability you expect to be used and what it will "
        "change. A refusal: what makes the request unanswerable."))
    most_likely: str = Field(min_length=1, description=(
        "The single answer you would give if you had to answer right now, "
        "inside answer_space."))
    confidence: int = Field(ge=1, le=5, description=(
        "Confidence the true answer falls inside answer_space: 1=strong "
        "negative, 2=weak negative, 3=neutral, 4=weak positive, 5=strong "
        "positive."))
    unknowns: str = Field(min_length=1, description=(
        "What you do not know that would move this forecast, and what would "
        "resolve it. Never empty — when nothing is missing, say that."))
```

Every field required, every string non-empty. This is the argument
`AcceptanceCriteria` already settled: an optional field next to a filled one
gets left blank, and a blank field cannot be told apart from an oversight. A
forecast with nothing to say about its unknowns must **say** that, which is a
statement the operator can check.

`answer_space` plus `most_likely` is the whole design compressed: **a
forecast is a set, and a point inside it.** The set is what resolution checks
containment against; the point is what makes a wide set embarrassing. A model
that hedges by returning "somewhere between zero and the heat death of the
universe" scores `inside` every time and is caught by `confidence` against
the width — a `5` on a set that admits everything is the signature of a
forecaster gaming its own metric, and it is visible in the trace without any
extra machinery.

`confidence` reuses the 1–5 Likert scale the response-language classifier
already uses, for the same reason it does: one scale across the trace, no new
vocabulary for the operator to learn.

`reason` and `unknowns` are free text sitting beside constrained fields
deliberately — trap 6 of `2026-07-24-operator-locale-and-language.md`:
removing a model's free-text field makes it reason *inside* the constrained
one. Two pressure valves here, because the forecast is the one call in the
system explicitly asked to speculate.

**No worked example in the system prompt.** Trap 1: example words get
parroted. A forecast prompt that illustrates a bound with "between 200 and
400 km" will produce distances in forecasts about payroll. The field
descriptions above name the *form* and never fill it in, which is the same
line `ACCEPTANCE_CRITERIA_SYSTEM_PROMPT` holds.

## The sealing rule

**A forecast reaches the trace and nothing else.** Not the scratchpad, not an
`observation.data` the model can see, not the room, not the summarizer.

This is architecturally free rather than a discipline to maintain. The decide
prompt's scratchpad renders from an in-memory `list[AssistantTurnEvent]`
built inside `handle()`, not from `self._steps` or from the persisted rows —
which is exactly why the `response_language_classifier` and
`acceptance_criteria` rows already sit in the trace without leaking into the
loop. A forecast recorded the same way inherits the same isolation. The one
piece of new discipline is the run summarizer, which reads step rows and
must skip `answer_forecast` rows; its digest describes what the run did, and
a sealed guess is not something the run did.

Two things break if the seal leaks, and both are fatal rather than
degrading:

- **The deciding model anchors on its own guess.** This is precisely the bias
  that justified splitting `reply_audit` out of the reply into its own call
  — the reasoning that produced a wrong answer is a bias toward ratifying it.
  A forecast in the prompt is that bias, injected before the work instead of
  after.
- **The ladder collapses.** Rung *n+1* shown rung *n* copies it. Every rung
  agrees, no rung diverges, and the mechanism reports perfect calibration
  forever.

The second failure is silent and looks like success. That is why the seal is
stated as a rule with a named owner rather than left as an implementation
detail.

## Resolution

One call per run, **after** the terminal message is posted, off the critical
path — the shape `assistant_run_summarizer` already established: enqueued at
the terminal state, posts no chat, enqueues nothing, so it can never resolve
itself. A sibling agent, `assistant_forecast_resolver`, rather than another
field on the summarizer: the summarizer's job is a human-readable digest of
what happened, and this one compares structured rows and returns structured
rows.

One call, not one per rung. The resolver must see the ladder *whole*, because
the finding is which rung came first.

```python
class ForecastVerdict(BaseModel):
    rung: str = Field(min_length=1, description=(
        "The rung id, copied exactly from the ladder you were given."))
    landed: Literal["inside", "outside", "unresolvable"] = Field(description=(
        "inside when the delivered answer falls within this rung's "
        "answer_space; outside when it does not; unresolvable when the run "
        "produced no answer to compare against."))
    note: str = Field(min_length=1)


class ForecastResolution(BaseModel):
    reason: str = Field(min_length=1)
    verdicts: list[ForecastVerdict] = Field(min_length=1, description=(
        "One verdict per rung, in the order given."))
    first_divergence: str = Field(min_length=1, description=(
        'The rung id of the earliest "outside" verdict, or "none".'))
    cause: Literal[
        "request_ambiguous", "context_missing", "context_misleading",
        "forecaster_error", "answer_wrong", "none",
    ]
    lesson: str = Field(min_length=1, description=(
        "What a change to the prompts, the retrieval, or the settings would "
        "have to do to move first_divergence later. Never empty — when there "
        "is nothing to learn, say that."))
```

`cause` is the payload. A wrong guess is not actionable; a wrong guess with
an attributed cause is:

- **`request_ambiguous`** — the operator's message genuinely admitted both
  readings. The fix is `ask_clarifying_question`, not a prompt change.
- **`context_missing`** — the answer depended on something no block carried.
  The fix is retrieval.
- **`context_misleading`** — a block was present and pointed the wrong way.
  The fix is that block, and the rung names which one.
- **`forecaster_error`** — everything needed was present at that rung and the
  forecast blew it anyway. The fix is the forecast prompt or its model
  binding. Nothing upstream is implicated.
- **`answer_wrong`** — see below.
- **`none`**.

### `answer_wrong`, and why it is the interesting one

The mechanism is framed as calibrating the guess against the answer. But a
divergence is **symmetric evidence**, and nothing about the arithmetic says
the answer wins.

A `cold` forecast that survives every rung unchanged, made before any
context arrived, and then disagrees with the delivered reply, is a
disagreement between the model's prior and six steps of work. Sometimes the
work is right and the prior was naive — that is the ordinary case. Sometimes
the work went somewhere strange and the prior is the only thing in the run
that noticed. A unit conversion off by a factor of a thousand is outside a
sane order-of-magnitude forecast, and the ladder catches it after the fact
with no assertion, no test, and no reviewer.

This is not `reply_audit` doing its job late. The auditor checks a message
against the request and the constraints — internal consistency. The forecast
checks it against what the model believed before it started, which is a
different and independent signal. They can disagree, and a reply the auditor
passed that a stable cold forecast rejects is the single most interesting row
this mechanism can produce.

It cannot block anything — it arrives after the message is posted. What it
can do is be counted, and if `answer_wrong` shows up with any regularity,
that is the argument for a forecast rung the auditor gets to see, which is a
different proposal and should stay one.

### When the run produced no answer

A `ask_clarifying_question` terminal, a stop, or a step-limit exit leaves
nothing to resolve against, and every rung comes back `unresolvable`. One
check still runs, and it is worth the call on its own:

**did the `cold` rung's `unknowns` already name the thing that was eventually
asked about?** If it did, the assistant knew at message-read time that it
needed clarification, and spent steps before asking. That is a directly
actionable finding about the decide prompt's bias toward acting, and it is
invisible in the trace today.

### The two languages problem

The `cold` rung fires **before** the response-language classifier — it has to,
or `post_language` measures nothing. So a cold forecast is written in
whatever language the request was in, while the delivered answer is written
in the classified reply language, and those routinely differ: that is the
entire point of the classifier.

The resolver is therefore told plainly that the forecast and the answer may
be in different languages and that **a language difference is never a
divergence**. Containment is about the answer, not its wording. This is the
one place the mechanism has to reach across into the language machinery, and
it reaches with a single sentence rather than a shared table.

## Model binding

A binding-only `answer_forecast` role on `/agentmodel`, resolved through
`resolve_model_group([(ANSWER_FORECAST_UUID, "answer_forecast"),
(self.agent_uuid, "own")])` — the pattern `second_opinion`, `reply_audit` and
`response_language_classifier` already use. Same for the resolver, which is
an ordinary agent with its own group.

The binding exists, and the default of leaving it unbound is the *correct*
setting rather than a lazy one, which is unusual enough to state:

**the forecaster should be the same model as the answerer.** A cheap
forecaster's misses tell you about the cheap model, not about the prompt —
every divergence resolves to `forecaster_error` and the ladder measures the
capability gap between two models instead of the context gap between two
rungs. Unbound gives same-model for free.

Binding a different model is a legitimate experiment — "would a stronger
model have flagged this earlier?" is a real question — but the moment the
binding differs, `forecaster_error` verdicts stop being interpretable and the
operator has to remember that. Worth a line in the setting's description, and
worth surfacing the binding in the run's per-step debug `log` block beside
the active profile, where the other "why is this run weird" answers already
live.

## Cost, stated plainly

`2 + n` extra calls per run, where `n` is the number of decide steps, capped
by `STEP_LIMIT = 6`. A six-step run goes from six decide calls plus the
classifier plus the criteria plus the audit, to eight of those plus eight
forecasts. That is not a rounding error; on a bad turn it roughly doubles the
model spend and adds a forecast's latency in front of every step.

And it buys the operator **nothing on that turn**. The reply is byte-identical
whether the ladder ran or not. Everything this mechanism produces is read
later, by a person deciding what to change.

Three things make that defensible:

1. The forecast prompt is narrow by construction. It carries no action
   catalog (it chooses no action), no skills block, and — at the `cold` and
   `post_language` rungs — no profile digest, because the rung is *defined* by
   not having seen it. Only the `step_n` rungs pay for the full decide
   context, and they can reuse the exact prompt the decide call is about to
   send.
2. A `step_n` forecast and the `step_n` decide call see identical context, so
   they are independent and could in principle be issued concurrently. The
   model plumbing (`structured_llm_call`) is synchronous today, so this
   proposal specifies the sequential version and notes the concurrency as the
   obvious optimization once the mechanism has earned it.
3. It is off by default and it is instrumentation. Nobody pays this on a turn
   they are not studying.

### The switch, and why this one gets a switch

`2026-07-29-reply-audit-as-its-own-call.md` argued *against* a flag and for a
clean cutover, on the grounds that the return was deleting the ordering
machinery and a flag would keep it alive. Nothing about that argument
transfers here, and the opposite conclusion is right for the opposite
reasons: this mechanism deletes nothing, changes no reply, and has a real
per-turn cost with no per-turn benefit. That is the exact profile a default-off
switch is for.

```python
"assistant.answer_forecast": Setting(
    "assistant.answer_forecast", None, "bool", False,
    description="Record sealed answer forecasts at each context boundary "
                "(before the language classifier, after it, and before each "
                "decide step) and resolve them against the delivered reply "
                "after the run finishes. Instrumentation only: forecasts "
                "never enter a prompt the deciding model reads and never "
                "change a reply. Costs one extra model call per decide step "
                "plus two. Default off.",
),
```

One switch, not two. A "cheap ladder" setting that drops the `step_n` rungs
is the obvious economy and it is the wrong first move: a truncated ladder
cannot localize a late divergence, which is most of what there is to find. Run
the full ladder, find out whether the tail carries signal, and cut it on
evidence rather than on the assumption that it does not.

## Where it shows

- **`/assistant`, the run inspector.** Each forecast is its own timeline row
  at the index of the call it precedes, distinguished by `action`, collapsed
  by default — the treatment the classifier row already gets. Above the
  timeline, a **ladder strip**: one cell per rung, marked inside / outside /
  unresolvable, with the divergence rung highlighted and the `cause` and
  `lesson` beside it. That strip is the feature. The rows are the evidence
  behind it.
- **The markdown export** carries both. Unlike the live "in flight" card,
  there is nothing live about a resolved ladder.
- **Not `/chat`.** Nothing about this is conversation.

The aggregate view — `cause` counts across runs, which is where "so prompts
can get better calibrated" actually pays off — is deliberately **not** in
this proposal. It needs the rows to exist first, and a page built before its
data is a page built against a guess about the data. It is the immediate
follow-up, not the first commit.

## How this gets measured

The gate for instrumentation is not a pass rate. It is whether the output is
actionable, and it has a specific failure mode worth naming in advance.

1. **Does the ladder localize?** Over the first several dozen turns, what
   fraction produce a `first_divergence` other than `none`, and does the rung
   vary? A mechanism that always blames `cold` has discovered only that
   guessing before reading is hard.
2. **Is `cause` non-uniform?** This is the kill criterion. If 90% of
   divergences come back `forecaster_error`, the resolver is a rubber stamp,
   the attribution is noise, and the ladder is an expensive way to learn that
   models are bad at guessing. The distribution across the five causes is
   what decides whether the mechanism survives.
3. **Does a divergence survive a fix?** The only end-to-end proof: take a
   `context_misleading` finding, change the block it named, rerun the same
   case, and check that `first_divergence` moved later or disappeared. One
   confirmed instance of this is worth more than any amount of aggregate
   statistics.
4. **Does `cold` → `post_language` stay quiet?** The standing assertion. A
   divergence there is a finding about the language block, reportable
   immediately.

`evals/profile_guidance.py` already runs cases through the live turn
construction via `build_turn_prompts`, so the harness for (3) exists; a
forecast variant beside the existing `classifier` and `audit` variants is the
natural home, scored on rung agreement rather than on the delivered reply.

## Relationship to the neighbouring mechanisms

- **`reply_audit`** reviews the finished message against the request and the
  constraints, before it posts, and can bounce it. The forecast ladder
  compares the message to what the model believed before it started, after
  it posts, and can bounce nothing. Different evidence, different timing,
  different powers. Their disagreements are the interesting rows.
- **`second_opinion`** reviews a program before it runs. No overlap.
- **`acceptance_criteria`** states what the reply must satisfy; a forecast
  states what the reply will probably *be*. Criteria are normative and enter
  the prompt; forecasts are predictive and never do. A rung fired after the
  criteria step sees them as context, which is exactly what makes `step_1`'s
  jump measurable.
- **`response_language_classifier`** is the rung boundary that
  `post_language` exists to bracket, and the source of the two-languages
  wrinkle above.
- **`assistant_run_summarizer`** is the architectural precedent for the
  resolver, and the one existing consumer of step rows that must learn to
  skip a new action.

## Traps this walks straight into

Named up front because each has already cost this codebase time:

- **Trap 1, parroted examples.** No worked example anywhere in the forecast
  prompt. A bound illustrated is a bound copied.
- **Trap 2, field descriptions mirrored as output.** `kind` is a `Literal`
  and `confidence` is a bounded int precisely so neither can come back as
  prose. `answer_space` cannot be typed and is the field most at risk;
  its description names forms, never contents.
- **Trap 3, parsing model prose.** `landed`, `cause` and `kind` are typed
  values. Nothing about containment is decided by a string check — the
  resolver decides, code records.
- **Trap 6, the missing pressure valve.** `reason` and `unknowns` on the
  forecast, `reason` and `note` and `lesson` on the resolution.
- **A new one this mechanism invents: the wide-set gambit.** A forecaster
  scored on containment is rewarded for uselessly wide sets. `most_likely`
  and `confidence` are the countermeasure, and the operator reading the strip
  is the enforcement. If gaming shows up anyway, the answer is a resolver
  field for set width, not a cleverer prompt.

## Open questions

- **Does `cold` need the room transcript at all?** A forecast made from the
  bare message is the purest baseline and the cheapest rung; a forecast made
  with the transcript is the one that reflects what the assistant actually
  starts from. Both are defensible and they measure different things. Ship
  with the transcript (it matches what the classifier already sees) and let
  the divergence rate say whether the purer version would be sharper.
- **Should `step_n` forecasts stop once a write has executed?** After a
  log-and-undo write the scratchpad already steers the model toward `reply`;
  a forecast at that point is close to a forecast of the message it is about
  to write, which is `reply_audit`'s territory arriving early and sealed.
  Cheap to include, and possibly redundant.
- **Does the resolver need the run's observations, or only the ladder and the
  answer?** Observations would let it distinguish `context_missing` from
  `context_misleading` with real evidence rather than inference — but they
  are also the bulk of the prompt, and the resolver drawing its own
  conclusions from raw tool output is a second reviewer with a different job.
  Start without them; the `cause` distribution will say whether the
  attribution is guessing.
