# Acceptance criteria v2 — split calls, code-owned variant rendering

**Date:** 2026-07-24
**Branch:** `acceptance-criteria-v2` (from `main`; the abandoned
`acceptance-criteria-cutover` branch is reference material only — no code
is cherry-picked from it).

## Why a second attempt

The first cutover attempt failed the same way three times: every live
language failure got a reactive patch, and the patches hardened into
exactly the fragility they were fighting:

- **Duplicated variant tables.** `LANGUAGE_VARIANT_RULES` (assistant) and
  `LANGUAGE_VARIANT_CLAUSES` (user_profile), each with hand-written
  contrastive examples, kept in lockstep only by a test assertion.
- **Code policing free-text model output.** `_ensure_language_variant_entry`
  regex-tokenized the model's `response_language` prose to guess whether it
  "named the tag", then patched the criteria after the fact. String-matching
  model prose never converges.
- **Example words leak into replies.** Contrastive examples in the prompts
  ("colour not color, anticlockwise not counterclockwise") get parroted:
  live replies included "colour" and "anticlockwise" in answers that had
  nothing to do with either. The examples meant to *constrain* output
  became *content*.

The root causes v2 removes:

1. `response_language` was free text mixing tag + reasoning, so code had to
   **parse** what it should have **received typed**.
2. One model call did two unrelated jobs — classify the reply language AND
   plan the work/formatting constraints — so the language job (the one that
   kept failing) never got a prompt narrow enough to be reliable.
3. Variant knowledge had no single owner, and it was expressed as example
   words instead of named dialects.

## Design principles

- **The model classifies; code composes.** The model's only language output
  is a typed BCP-47 tag. Code validates it, resolves the variant against
  the profile, and renders the authoritative directive text. No regex over
  model prose, no post-hoc patching, no duplicate tables.
- **No contrastive example words in any prompt.** Variant directives name
  the dialect ("British English spelling and vocabulary throughout; never
  American English words or phrasing") — never sample words. Sample words
  (colour/color, …) live only in tests and evals, as *detection markers*
  on output.
- **One narrow call per job.** Step 1 answers exactly one question: *what
  language must the reply be in?* Step 2 plans the remaining criteria
  (units, separators, assumptions) with the language already settled.

## Architecture

### Step 1 — the reply-language call

A new narrow persona, `REPLY_LANGUAGE_SYSTEM_PROMPT`, with the existing
language rules (code-owned sentences; profile languages admitted only
through the prompt-boundary validation in `user_profile.formatting`):

- The reply's language is the language of the operator's CURRENT message —
  that message alone decides.
- Earlier operator messages matter only when the current message is too
  short to tell. The assistant's own earlier replies are never a language
  reference.
- An explicit language request in the current message always wins.
- The operator's preferred language(s) (validated tags) apply only when
  the message explicitly asks for them.
- Emit the most specific tag you can justify; a bare primary tag ("en")
  is fine when the message doesn't reveal the variant — the variant is
  resolved from the profile by code.

Structured output (both fields required — the no-defaults rule):

```python
class ReplyLanguage(BaseModel):
    language_tag: str   # BCP-47 tag, e.g. "en", "en-GB", "da"
    reason: str         # e.g. "mirrors the current message"
```

User prompt: `current_request`, the operator-only history tail
(`ACCEPTANCE_CRITERIA_MAX_MESSAGES`), and the ask. No formatting guide, no
settings block — the profile languages the call may use are already in the
system prompt as validated tags.

### Code-owned resolution and rendering

`user_profile.formatting` becomes the **single owner** of variant
knowledge. `ENGLISH_SPELLING` is replaced by one table, example-free:

```python
# tag -> (language name, variant name, contrasting variant name)
LANGUAGE_VARIANTS: dict[str, tuple[str, str, str]] = {
    "en-GB": ("English", "British English", "American English"),
    "en-US": ("English", "American English", "British English"),
    "nb": ("Norwegian", "Norwegian Bokmål", "Norwegian Nynorsk"),
    "nn": ("Norwegian", "Norwegian Nynorsk", "Norwegian Bokmål"),
}
```

(The formatting-guide clause renders from the same table:
"Write {language} in {variant} — spelling and vocabulary; never
{contrast}." — replacing the spelling-only sentence, still example-free.)

Resolution, in code (`_resolve_reply_language`):

1. Canonicalize the model's tag through the same safe BCP-47 subset the
   profile fields use (a new public `valid_language_tag()` wrapping the
   existing `_valid_language`). Invalid → fail-open: no language directive
   (the formatting guide's language line still applies downstream).
2. If the tag is bare-primary ("en") and a validated profile language
   shares that primary subtag ("en-GB"), upgrade to the profile tag —
   the settings-based default the rules point at. An explicit variant tag
   from the model ("en-US" because the message asked for American) is
   kept as-is: explicit wins.
3. Compose the directive:
   - variant known: `The reply must be in en-GB: British English spelling
     and vocabulary throughout; never American English words or phrasing.`
   - no variant entry: `The reply must be in da.`
   The model's `reason` is appended in parentheses as disclosure, e.g.
   `… (mirrors the current message)`.

### Step 2 — the work-criteria call

The existing criteria persona minus everything language:

```python
class WorkCriteria(BaseModel):
    processing: list[str]   # steer the WORK (target unit, timezone, …)
    formatting: list[str]   # steer the FINAL MESSAGE (separators, dates, …)
    assumptions: list[str]  # settings-resolved ambiguities, disclosed
```

Its system prompt keeps the resolve-from-settings-and-disclose rules and
the revision rules, and adds: *the reply's language is already established
and provided as data; do not restate or revise it.* Its user prompt keeps
the current inputs (request, operator-only history, `user_settings_json`,
formatting guide; on revision also the prior criteria and the run's steps)
plus the established `<reply_language>` directive as data.

### Composition and injection

`AcceptanceCriteria` stops being an LLM response model and becomes the
composed container. The injected section keeps its exact shape — one
`<acceptance_criteria_json>` directly after `<current_request>`, replaced
never appended — so `source_priority`, the audit, and the second-opinion
prompt are untouched in structure:

```json
{"response_language": "The reply must be in en-GB: British English spelling and vocabulary throughout; never American English words or phrasing. (mirrors the current message)",
 "processing": ["target unit: meters (settings: metric)"],
 "formatting": ["numbers: dot decimal, no thousand separators"],
 "assumptions": ["convert target not stated; assuming meters"]}
```

Per-call fail-open (code composes, so partial rendering is safe):

- language call fails → the `response_language` key is omitted; the work
  criteria still render.
- work call fails → the language directive renders with empty lists.
- both fail → no section; the run proceeds exactly as before the feature.

### Trace and revision

- Each call is its own step row. The work call keeps
  `action="acceptance_criteria"`; the language call records
  `action="reply_language"` — a new `AssistantActionName` member with NO
  `Capability` entry, so it exists for trace rows only and can never be
  dispatched or cataloged. (The action-surface lock test gains the value.)
- Step 0 (both calls) and code-driven refreshes stay outside `step_limit`,
  sharing the surrounding decide index, as today.
- **Code-driven refresh** (after a `revises_acceptance_criteria` write):
  re-runs BOTH calls from the fresh snapshot — a settings write is exactly
  what can change the language.
- **Model-requested `acceptance_criteria` revision**: re-runs ONLY the
  work call. The language rules anchor on the operator's current message,
  which cannot change mid-run; the only mid-run language trigger is a
  settings write, which is the code-driven path. The established language
  directive is passed to the revision call as data.

## The cutover ideas v2 re-implements from scratch

1. **Always on.** The `assistant.acceptance_criteria` switch is removed
   from `db/settings.py`; `_acceptance_criteria_switch`, the
   `_criteria_enabled` gating, the catalog pop, and the turn-log entry go
   with it. `ASSISTANT_SYSTEM_PROMPT` bakes in the criteria-aware
   source-priority block as a single literal (the swap machinery and the
   baseline literal are deleted).
2. **Retire `1_specification`.** Reply args become
   `{"1_message", "2_audit"}`. The audit checks the message first against
   the operator's current message (every sentence answered), then against
   `acceptance_criteria_json`, the user settings, and the formatting
   guide — hunting for a dropped sentence, wrong separators, the wrong
   language or language variant, or an answer copied from an earlier
   reply. The bare-verdict/"OK" contract, prefix-order enforcement, and
   arg hints all update to the two-key shape. **No example words** in the
   description (the cutover's "counterclockwise in a British reply" list
   is exactly the parroting hazard this design bans).
3. **A repeated request drops old assistant replies.** The verbatim-repeat
   omission (normalized whitespace+casefold compare, operator messages
   kept, `assistant_messages="omitted_repeated_request"`) — deterministic
   code, re-implemented.
4. **"An earlier reply is never evidence."** The system-prompt and
   decision-request additions: redo the work fresh from `current_request`
   and `acceptance_criteria_json`; a near-duplicate is a different
   request; the environment may have changed. Rewritten lean.

## Testing

- Rewrite `test_assistant_acceptance_criteria.py` for the two-call flow:
  step 0 makes both calls in order (language first); the section renders
  after `current_request`; per-call fail-open (each of the three
  combinations); only the latest criteria render; refresh re-runs both,
  model revision re-runs only the work call and consumes a decide step;
  step rows carry the right actions and no `step_limit` consumption.
- Resolution unit tests: bare "en" upgrades to the profile's "en-GB";
  explicit "en-US" survives an en-GB profile; invalid tag fails open;
  a no-variant tag ("da") renders the plain directive; the directive and
  formatting-guide clause render from the same table.
- **A no-example-words guard:** assert the rendered system prompts and
  capability descriptions never contain the marker words the evals use
  (colour, color, anticlockwise, counterclockwise, …) — the test owns the
  marker list, the prompts must not.
- Mechanical sweep: every test using `1_specification`/`2_message`/
  `3_audit` reply args moves to `1_message`/`2_audit`; the action-surface
  lock test gains `reply_language`; fakes gain a `ReplyLanguage` seam next
  to the existing criteria seam.
- Evals (`evals/profile_guidance.py`): add the proposal's ambiguity cases
  as live cases with marker-word scoring — "convert 1053737172 feet" →
  meters; Danish question → Danish reply; English question → no Danish;
  "answer in danish: …" → Danish; a translate-to-English case scored on
  variant markers (must_include/must_not_include colour/color class
  words). If the live harness needs the criteria stage wired into
  `build_turn_prompts`, that wiring is part of this work; if it balloons,
  the eval extension lands as the immediate follow-up commit.

## Out of scope

- The work contract (side effects, premortem) — parked in
  `2026-07-23-work-contract.md` as before.
- A dedicated model binding for the criteria calls (`SECOND_OPINION_UUID`
  pattern) — still a later option.
- Capabilities that set `revises_acceptance_criteria` — the flag ships
  with zero flags set, as on main.
