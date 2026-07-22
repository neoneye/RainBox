# acceptance_criteria — establish the reply's constraints before any work

**Status:** Proposal, not implemented.
**Date:** 2026-07-23

## Naming

The concept is the contract for the deliverable — the hand-off, the
SMART criteria the reply must satisfy. Candidates considered:
`reply_specification` (the working title while the idea formed; bespoke
jargon a model has never seen), `reply_contract` (a contract connotes
two negotiating parties; this is derived unilaterally from settings +
request), `definition_of_done` (about when to stop, not what binds the
work). Chosen: **`acceptance_criteria`** — the conditions the reply must
satisfy to be accepted. Two properties the others lack: it pairs
structurally with the self-audit (the audit becomes an acceptance TEST
against pre-committed criteria instead of a vague "re-read and check"),
and it is a term small models have seen correctly used across agile and
PM training data — a familiar concept is cheap for a 4B model where a
bespoke one is load.

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

The idea: a dedicated **acceptance_criteria step** that runs BEFORE the
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
2. **What side effects the request demands** — a state change like
   "kanban task X is in the DONE column" is the deliverable itself; the
   reply is only the receipt (see Side-effectful requests below).
3. **Which user preferences are relevant while processing the steps** —
   e.g. the target unit for a conversion (metric → meters), the timezone
   for a reminder, the currency for a price.
4. **Which user preferences are relevant when formatting the final
   result** — separators, date format, temperature unit, spelling.

For "convert 1053737172 feet" the step would establish, before any tool
runs: *target = meters (user settings: metric); number format = dot
decimal, no thousand separators; response language = American English
(the message is English)*. The python_run step then converts to meters
because the criteria say so, and the reply formats accordingly.

## Design

### A code-driven step 0, not a model-chosen tool

Two ways to expose it:

- **(a) an action in the catalog** the model may call. Rejected as the
  primary mechanism: a small model that forgets to call it gets no criteria
  (exactly the runs that need it most), and enforcing "call this first"
  via validation burns a step teaching it. The house rule is *enforced by
  the loop, not prompt discipline* (same reasoning as the second-opinion
  gate).
- **(b) a code-driven step 0**: `handle()` makes the specification call
  itself, before the decide loop starts — the same pattern as the
  declared-profile blocks (code decides, the model receives). The model
  cannot skip it, the decide loop starts with the criteria already
  established, and no catalog entry or decision branching is added — no
  new constraint burden on the small model (the typed-reply lesson).

This proposal is (b) for the initial criteria — and (a) returns in a
supporting role for mid-run revision (next section): code guarantees the
criteria exist, the model may ask to revise it when the situation changes
in ways only it can see.

### The call

One structured call at the start of every run (plus mid-run revisions —
see below), with its own purpose-built system prompt (like
`SECOND_OPINION_SYSTEM_PROMPT` — a separate persona with a narrow job,
not the assistant's 4-5k token working prompt):

```python
class AcceptanceCriteria(BaseModel):
    """The reply's constraints, established before any step runs."""
    response_language: str = Field(description=(
        "The language the reply will be written in, with the reason — "
        'e.g. "en-US (mirrors the current message; profile spelling '
        'en-US)". Mirror the language of the current message; the '
        "profile's preferred language applies only when the message "
        "explicitly asks for it; an explicit request always wins."))
    side_effects: list[str] = Field(description=(
        "State changes the request demands — the measurable outcome, "
        "e.g. 'kanban task X is in the DONE column'. ONLY the named "
        "changes: anything beyond them is out of scope for this "
        "request. Empty for a pure question."))
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

### Side-effectful requests

Not every deliverable is a formatted answer. For "move kanban task X to
DONE" the deliverable IS the state change; the reply is only the
receipt. `side_effects` names the measurable outcome up front, which
buys three things:

- **The audit gets a second dimension.** Beyond formatting, the
  acceptance test becomes: does the message claim exactly the side
  effects whose steps succeeded (`ok=True`)? This turns the existing
  anti-fabrication rule ("never claim a write that didn't run") from a
  general principle into a per-run, named checklist.
- **Write tiers are visible in the criteria.** A confirm-tier effect
  ends the run as a PROPOSAL, not a completed change — the criteria for
  "delete board Y" would read "a confirm card for deleting board Y is
  proposed", so the reply says "awaiting your confirmation", never
  "deleted". The criteria call knows the capability tiers from a short
  code-owned summary in its system prompt (not the full catalog).
- **Scope is bounded.** "ONLY the named changes" gives the second
  opinion and the summariser an explicit yardstick for
  over-reach — moving task X must not also touch task Y.

Example criteria for "move kanban task X to DONE":

```json
{"response_language": "en-US (mirrors the current message)",
 "side_effects": ["kanban task X is in the DONE column"],
 "processing": ["resolve 'X' to a task uuid before moving (find_uuid)"],
 "formatting": [],
 "assumptions": ["'DONE' matched to the board's Done column by name"]}
```

Mid-run revision applies here too: if a read reveals task X is already
in DONE, the revised criteria record the no-op ("task X already in
DONE; no move needed") so the reply reports the true state instead of
claiming a move that never ran.

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
<acceptance_criteria_json>
{"response_language": "en-US (mirrors the current message)",
 "processing": ["target unit: meters (settings: metric)"],
 "formatting": ["numbers: dot decimal, no thousand separators"],
 "assumptions": ["convert target not stated; assuming meters"]}
</acceptance_criteria_json>
```

A bare tag, no attributes, `_json`-suffixed like `<user_settings_json>`:
everything in the user prompt is context, and the tag name names both
the content and its format. The semantics live in the system prompt as
one code-owned sentence: *"acceptance_criteria_json is the established
plan for this turn's reply: follow it during steps and when composing
the message, unless the operator's request overrides it."* (The content
is model-generated, so the authority stays in that code-owned sentence —
same rule as every other model-derived block.) `source_priority` lists
`acceptance_criteria_json` directly below `current_request`.

### Mid-run revision — the criteria are current state, not a step-0 snapshot

The situation can change halfway through the steps, and the criteria must
change with it. The sharpest case: the request itself mutates the
preference the criteria were built from —

> "change my preferred response language to en-US"

Step 0 reads the OLD settings (say en-GB) and produces criteria for a British reply. The
assistant then executes the preference write. The confirmation reply must
already be in en-US — replying "Certainly, colour noted" in British
English about the switch to American English is exactly the class of
mistake this feature exists to kill.

Two revision triggers, mirroring who can see the change:

- **Code-driven refresh** for changes code can see: `Capability` gains a
  `revises_acceptance_criteria: bool` flag, set on any write that can mutate
  preferences (today `memory_remember` of a preference-shaped fact;
  future profile/settings write capabilities). After such a write
  succeeds, the loop re-runs the specification call against the FRESH
  settings snapshot and replaces the injected section for all subsequent
  steps. Loop-enforced — the model cannot forget it.
- **A `acceptance_criteria` catalog action** for changes only the model
  can see: an observation reveals something that invalidates an
  assumption (a recalled fact says the operator wants altitude in feet;
  the operator's message redefines the target mid-request). The action
  takes no args, re-runs the same specification call, and its observation
  is the new criteria. Read-tier, no undo needed — the criteria are derived state.

Only the LATEST criteria are injected (`<acceptance_criteria_json>` is replaced,
never appended — two sets of criteria in one prompt is a contradiction machine);
every criteria call remains in the trace as its own step, so the operator
can see the revision history: what step 0 assumed, what changed, what
the reply actually followed.

### Trace

Every specification call is recorded as a step row
(`action="acceptance_criteria"` — code-driven for step 0 and refreshes,
a normal decision for model-requested revisions), so the inspector shows
each criteria call, its prompts, and its latency like any other step, and the
operator can spot a wrong assumption at a glance.

### Failure and cost

Best-effort, fail-open: a failed criteria call logs a warning, injects no
section, and the run proceeds exactly as today (the formatting guide and
settings blocks still apply). Cost: one extra structured call per run —
a few seconds on the local model; acceptable for a personal assistant,
and the step-0 result is reused by every subsequent step in the run.

### Relationship to the existing reply args

The `1_specification` reply argument becomes redundant once the run-level
criteria exist — the constraints are established before the work instead of
restated after it. Follow-up (gated on the evals below): shrink the reply
args back to `{"1_message", "2_audit"}`, with the audit checking the
message against the run-level `acceptance_criteria` (the audit's
"re-read and check" gains a concrete, pre-committed yardstick instead of
one invented in the same response).

## Rollout

House pattern — ship dark, gate, enable:

1. `assistant.acceptance_criteria` switch (default off) in
   `db/settings.py`.
2. Unit tests: the criteria call is made once per run before step 0; the
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
   - a preference-mutating turn (once a preference write capability
     exists): "change my preferred response language to en-US" → the
     confirmation reply is already en-US, and the trace shows two criteria
     steps (step 0 with the old language, the refresh with the new).
   - a side-effect turn: "move task X to DONE" → `side_effects` names
     the move; the reply claims it only after the kanban_move step
     returned ok (scripted-seam test: a failed move must yield a reply
     that does NOT claim completion).
   A/B the suite with the switch off/on; the criteria step must not regress the
   locale cases it doesn't touch.
4. Flip the switch; watch traces for wrong `assumptions` — they are the
   new failure surface (a wrong assumption stated openly beats a silent
   coincidence, but it still needs the operator's eye during rollout).
