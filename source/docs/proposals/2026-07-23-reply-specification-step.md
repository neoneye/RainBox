# reply_specification — establish the reply's constraints before any work

**Status:** Proposal, not implemented.
**Date:** 2026-07-23

## Motivation

Three observed failures share one root cause — the assistant discovers the
reply's constraints too late, or never:

- **Ambiguous conversion target.** "convert 1053737172 feet" does not say
  the target unit. The runs picked meters — by coincidence, not by
  decision. Nothing in the trace shows the assistant *choosing* metric
  because the user settings say metric; the python_run step just happened
  to multiply by 0.3048. On another sampling day it could pick yards.
- **Language drift.** Replies switched to Danish because the profile says
  `language: da`, even though the operator writes in English and wants
  English unless Danish is explicitly requested. The formatting guide's
  language directive now encodes the mirroring rule, but it competes with
  everything else in a 4-5k token prompt at every step.
- **Spec-at-reply-time is too late.** The current `1_specification` reply
  argument (which grew out of a misreading of this idea) asks the model to
  state constraints *in the same breath* as the answer, after all the work
  is done. It reads as a rationalization of what was already computed —
  it cannot influence the python_run that already ran with the wrong (or
  lucky) target unit.

The idea: a dedicated **reply_specification step** that runs BEFORE the
assistant starts doing things. Its only job is to find out, concretely for
this request:

1. **What language the response will be in.** The operator's rules:
   - Mirror the conversation: ask in Danish → answer in Danish; ask in
     English → answer in English. Never switch language mid-conversation
     on your own.
   - The profile's language (da) is used only when explicitly requested —
     "answer in Danish" in an English message wins.
   - Spelling follows the profile's English variant (en-US — American
     spelling, "color" not "colour").
2. **Which user preferences are relevant while processing the steps** —
   e.g. the target unit for a conversion (metric → meters), the timezone
   for a reminder, the currency for a price.
3. **Which user preferences are relevant when formatting the final
   result** — separators, date format, temperature unit, spelling.

For "convert 1053737172 feet" the step would establish, before any tool
runs: *target = meters (user settings: metric); number format = dot
decimal, no thousand separators; response language = American English
(the message is English)*. The python_run step then converts to meters
because the specification says so, and the reply formats accordingly.

## Design

### A code-driven step 0, not a model-chosen tool

Two ways to expose it:

- **(a) an action in the catalog** the model may call. Rejected as the
  primary mechanism: a small model that forgets to call it gets no spec
  (exactly the runs that need it most), and enforcing "call this first"
  via validation burns a step teaching it. The house rule is *enforced by
  the loop, not prompt discipline* (same reasoning as the second-opinion
  gate).
- **(b) a code-driven step 0**: `handle()` makes the specification call
  itself, before the decide loop starts — the same pattern as the
  declared-profile blocks (code decides, the model receives). The model
  cannot skip it, the decide loop starts with the spec already
  established, and no catalog entry or decision branching is added — no
  new constraint burden on the small model (the typed-reply lesson).

This proposal is (b).

### The call

One structured call per run, with its own purpose-built system prompt
(like `SECOND_OPINION_SYSTEM_PROMPT` — a separate persona with a narrow
job, not the assistant's 4-5k token working prompt):

```python
class ReplySpecification(BaseModel):
    """The reply's constraints, established before any step runs."""
    response_language: str = Field(description=(
        "The language the reply will be written in, with the reason — "
        'e.g. "en-US (mirrors the current message; profile spelling '
        'en-US)". Mirror the language of the current message; the '
        "profile's preferred language applies only when the message "
        "explicitly asks for it; an explicit request always wins."))
    processing: list[str] = Field(description=(
        "User preferences that steer the WORK — e.g. 'target unit: "
        "meters (settings: metric)' for an ambiguous conversion, the "
        "timezone for a reminder. Empty when none apply."))
    formatting: list[str] = Field(description=(
        "User preferences that steer the FINAL MESSAGE — separators, "
        "date format, temperature unit, spelling. Empty when none "
        "apply."))
    assumptions: list[str] = Field(description=(
        "Ambiguities in the request resolved by a settings-based "
        "assumption, stated so the operator can spot a wrong one — "
        "e.g. 'convert target not stated; assuming meters'."))
```

The system prompt is code-owned and small (~40 lines): the language
rules above (generalized — the profile's languages interpolated through
the existing prompt-boundary validation in `user_profile/formatting.py`),
plus "resolve ambiguity from the user settings and SAY SO in
assumptions". Inputs: the current request, the last few conversation
messages (language continuity needs history), `user_settings_json`, and
the formatting guide. NOT the action catalog — this step plans
constraints, not actions.

Model binding: the assistant's own model group by default; a dedicated
binding (like `SECOND_OPINION_UUID`) is a later option if a smaller or
larger model proves better at it.

### Injection

The result renders as a prompt section for EVERY decide step, placed
directly after `<current_request>` — the request and its constraints
travel together at the top of the prompt:

```xml
<reply_specification authority="context">
{"response_language": "en-US (mirrors the current message)",
 "processing": ["target unit: meters (settings: metric)"],
 "formatting": ["numbers: dot decimal, no thousand separators"],
 "assumptions": ["convert target not stated; assuming meters"]}
</reply_specification>
```

`authority="context"` (the content is model-generated, so it cannot
carry instruction authority — same rule as every other model-derived
block); the system prompt gains one code-owned sentence with the actual
authority: *"reply_specification is the established plan for this turn's
reply: follow it during steps and when composing the message, unless the
operator's request overrides it."* `source_priority` lists it directly
below `current_request`.

### Trace

The call is recorded as a step row (`action="reply_specification"`,
a code-driven phase like the context markers — not a model decision), so
the inspector shows the spec, its prompts, and its latency like any other
step, and the operator can spot a wrong assumption at a glance.

### Failure and cost

Best-effort, fail-open: a failed spec call logs a warning, injects no
section, and the run proceeds exactly as today (the formatting guide and
settings blocks still apply). Cost: one extra structured call per run —
a few seconds on the local model; acceptable for a personal assistant,
and the step-0 result is reused by every subsequent step in the run.

### Relationship to the existing reply args

The `1_specification` reply argument becomes redundant once the run-level
spec exists — the constraints are established before the work instead of
restated after it. Follow-up (gated on the evals below): shrink the reply
args back to `{"1_message", "2_audit"}`, with the audit checking the
message against the run-level `reply_specification` (the audit's
"re-read and check" gains a concrete, pre-committed yardstick instead of
one invented in the same response).

## Rollout

House pattern — ship dark, gate, enable:

1. `assistant.reply_specification` switch (default off) in
   `db/settings.py`.
2. Unit tests: the spec call is made once per run before step 0; the
   section renders after `current_request`; a failed call is fail-open;
   the language rules render the profile languages through the prompt
   boundary.
3. Extend `evals/profile_guidance.py` with ambiguity cases:
   - "convert 1053737172 feet" → expected: meters in the reply,
     `assumptions` names the metric default.
   - "hvor langt er 100 km?" → Danish reply (mirroring).
   - "explain X" in English → English reply, no Danish.
   - "answer in danish: how far is 100 km?" → Danish reply (explicit
     request wins).
   A/B the suite with the switch off/on; the spec must not regress the
   locale cases it doesn't touch.
4. Flip the switch; watch traces for wrong `assumptions` — they are the
   new failure surface (a wrong assumption stated openly beats a silent
   coincidence, but it still needs the operator's eye during rollout).
