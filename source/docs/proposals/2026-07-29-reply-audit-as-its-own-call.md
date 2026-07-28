# The reply audit is its own call, not an argument

**Status:** Implemented on `reply-audit-own-call`. The measurement below —
whether a separate auditor catches anything the self-audit missed — has not
been run; that is what decides whether the second call earns its latency.
**Date:** 2026-07-29

## Naming

The concept is a check on a message that already exists. Candidates
considered: `proofread` (connotes spelling and grammar; too narrow for a
check that includes "did you answer the second question"), `reply_review`
(collides with `second_opinion`, whose trace rows and UI already own the word
*review*), `acceptance_test` (accurate against `acceptance_criteria`, but
binds a default-on mechanism to a default-off one). Chosen: **`reply_audit`**
— it keeps the vocabulary the operator already reads in traces, and it names
the object it audits, which distinguishes it from `second_opinion` at a
glance: second opinion reviews a **program before it runs**, reply audit
reviews a **message before it posts**.

The word *self* is dropped deliberately. It stops being self-review, and
that is the point of the change.

## The problem

`reply` carries two arguments, `1_message` and `2_audit`, and the contract is
that they are WRITTEN in that order — the audit composed after the message
exists, so it is a re-read rather than a reflex "OK". A single generation has
no way to guarantee that, so the codebase enforces it three times:

1. `_validate_args` on the parsed dict — insertion order, when the parser is
   faithful (`assistant.py`, the `REPLY_ARG_ORDER` check).
2. `_audit_order_error` on the raw response text — the authority when the
   parser normalizes.
3. `_audit_rejection` again at the last gate before posting, carrying the
   comment *"validation's raw-text check has been escaped live in ways not
   yet reproduced offline"*.

Plus a forensic `logger.info` recording what each check saw, because reversed
replies have shipped live while every offline reproduction bounces.

Four layers of defence around a property that two calls make impossible to
violate. That is the whole diagnosis: **the ordering contract is a simulation
of sequencing inside one generation, and the simulation leaks.**

Three further problems ride along:

- **The auditor is the author.** Same weights, same context, same reasoning
  chain that produced the mistake, asked to find the mistake. The reasoning
  that led to a wrong answer is a bias toward ratifying it.
- **The verdict is parsed prose.** `2_audit` passes only as the literal
  `"OK"`; a narration ending in OK is rejected by a string check. Trap #3 in
  `2026-07-24-operator-locale-and-language.md`: code that parses model prose
  never converges. The classifier already proved the alternative — emit a
  typed value, let code decide.
- **The audit has no cost, model or duration of its own.** It is invisible in
  the trace, so nobody can ask whether it earns its place.

## The design

`reply` shrinks to one argument. A separate call audits the message.

```python
# The terminal reply carries the answer and nothing else. No prefixes: the
# number prefixes existed to encode a writing order, and there is no longer
# an order to encode. `message` matches `question`, the other terminal arg.
required_args=("message",)
```

```python
class ReplyProblem(BaseModel):
    problem: str    # what is wrong, stated so a later step can fix it
    evidence: str   # the phrase in the message, or the part of the
                    # request it left unanswered


class ReplyAudit(BaseModel):
    reason: str                     # brief, audit-safe; the pressure valve
    problems: list[ReplyProblem]    # empty when nothing was found
    verdict: Literal["send", "revise"]
```

`verdict` is typed, so the `"OK"`-versus-narration string check disappears.
`reason` is free text beside the constrained fields on purpose — trap #6:
removing a model's free-text field makes it reason *inside* the constrained
one.

A `revise` verdict with an empty `problems` list still bounces; the loop
substitutes a placeholder complaint. `second_opinion` already resolves that
same case this way.

### What the auditor is shown, and what it is not

Shown:

- `current_request` — the thing the message must actually answer.
- the message itself.
- the established constraints (`acceptance_criteria_json` when the switch is
  on), `user_settings_json`, `formatting_guide`, `reply_language_markdown`.
- the turn's step **observations** — the fresh evidence a read produced.

Not shown:

- the decide loop's `reason` fields — the rationalization chain.
- the action catalog, the skills block, the profile digest. The auditor
  chooses no action and needs no capabilities.

The observation/reasoning split is the substance of the design. Dropping the
observations too would be simpler, but then a message that misreports what a
tool returned sails through — the auditor would have nothing to check the
claim against. Keeping the reasoning would reintroduce the bias the separate
call exists to remove. So: **the evidence, not the argument.**

### Model binding

A binding-only `reply_audit` role on `/agentmodel`, falling back to the
assistant's group when unbound — the pattern `second_opinion` and
`response_language_classifier` already use. This is what makes the latency a
choice rather than a tax: auditing is a checking task, not a generation task,
and it can be pointed at a small fast model independently of the reply model.

### Failure behaviour

Fails **open**: no group bound, or the call raises, and the message posts,
with the reason recorded on the step. This differs from `second_opinion`'s
justification — there the gated thing is side-effect-free compute, here it is
the operator's actual answer — but the conclusion is the same and stronger. A
turn that produces nothing because a checker was unreachable is worse than a
turn that produces an unaudited answer.

### The rejection loop is unchanged

`MAX_AUDIT_REJECTIONS`, the corrective text appended to the scratchpad as a
rejected step, and "past the cap the reply ships anyway" all stay exactly as
they are. Only the origin of the rejection moves. This is deliberately not
the place to redesign the bounce policy.

## What gets deleted

- `REPLY_ARG_ORDER` and `AUDIT_ORDER_ERROR`
- `_audit_order_error` and its raw-text scan
- the dict-order check in `_validate_args`
- the third enforcement layer inside `_audit_rejection`
- the forensic `logger.info("reply order check: …")`
- the `"OK"`-literal-versus-narration parsing
- the arg-order hint text in the validation error messages

An unreproduced live bug's entire class goes with them. That is the return on
the change; everything else is upside.

## Cutover, not a switch

Clean cutover: `2_audit` is removed, not kept behind a flag.

A switch would mean both paths coexist, which means the ordering machinery
above must **stay** — and deleting it is the point. A gated rollout here buys
nothing and costs the entire benefit until the switch flips. This is a
single-operator system with git; if the separate audit turns out worse, the
remedy is reverting a commit, not flipping a setting.

The rename `1_message` → `message` touches roughly thirty test files
mechanically. Stored traces keep whatever key they were written with, and the
inspector renders the JSONB it finds, so historical runs stay readable
without a migration.

## Cost

One extra call per **turn**, not per step — only terminal decisions are
audited. The prompt is narrow by construction (no catalog, no skills, no
profile digest, no reasoning), and the model is chosen independently. Against
that: the current self-audit already costs a whole decide step whenever it
bounces, and those steps carry the full decide prompt.

The honest risk is a small auditor that rubber-stamps everything — latency
for nothing. That is a measurement, not an argument, and the harness for it
exists.

## How this gets measured

`evals/profile_guidance.py` now scores delivered replies, so add an `audit`
variant beside `classifier` and compare over the same cases. Three questions,
in order:

1. Does the separate auditor catch anything the self-audit missed? Compare
   pass rates on the `language` family, where a delivered-variant failure is
   exactly the kind of thing an audit should catch and the current one does
   not.
2. What is the bounce rate, and how many bounces are wrong? A `revise` on a
   correct message is the failure mode that burns steps.
3. What does it cost in wall-clock at the operator's bound models?

Question 1 is the one that decides whether this ships. If a separate auditor
scores the same as the self-audit, the ordering machinery is still worth
deleting, but the second call is not worth its latency — in that case keep
the split and bind the audit to the cheapest model that holds the pass rate.

## Relationship to the neighbouring mechanisms

- **`second_opinion`** reviews a program before it runs; this reviews a
  message before it posts. Different objects, different failure modes, no
  overlap to resolve. A turn can legitimately pay for both.
- **`acceptance_criteria`** (default off) supplies what the message is
  checked *against*. The audit consumes the criteria when they exist and
  falls back to the request plus settings when they do not — it never
  requires the switch.
- **The completeness clause** proposed for the `2_audit` argument
  description ("does it answer ALL of it, every sentence and sub-question?",
  from the parked `acceptance-criteria-v2` branch) belongs here instead. It
  has a far better chance as the explicit job of a narrow call than as a
  clause buried in an argument description the model reads while it is busy
  composing the answer.

## Open questions

- Should a `revise` verdict be shown to the operator, or stay in the trace?
  Current behaviour hides bounces; a persistently bouncing auditor is
  something the operator would want to see without opening `/assistant`.
- Does the auditor need the conversation history at all? The current request
  plus the message may be enough, and less context is the design's thesis.
  Start without it and let the eval say.
