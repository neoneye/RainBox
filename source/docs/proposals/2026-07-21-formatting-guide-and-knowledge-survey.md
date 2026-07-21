# Formatting guide and knowledge survey

**Status: proposal.** Two coupled features, both derived from the person
profile selected by `profile.current`:

1. **The formatting guide block** — a deterministic prompt section that turns
   the active profile's locale fields (units, date/time formats, timezone,
   language, currency, and a new number-format field) into imperative
   *directives with worked examples*, so replies stop defaulting to imperial
   units, USD, 12-hour clocks, and MM/DD dates for an operator whose profile
   says otherwise.
2. **The knowledge survey** — a per-profile list of topics with a
   self-assessed level, current practice, and a free-text note, edited on the
   `/profile` form pane and rendered into a second prompt block, so the
   assistant pitches each answer at what the recipient actually knows instead
   of guessing (and guessing the same for every topic).

They are one proposal because they share the entire delivery path: both read
the `profile.current` profile, both are rendered by registry-driven code in
`user_profile/`, and both are injected at the same point in prompt assembly.
The formatting guide fixes *how* replies are written; the survey fixes *at
what level*.

## Problem

The assistant already knows the operator's formats and still gets them wrong.
The `<operator_identity>` block (`user_profile/identity.py`) injects the
active profile's fields as JSON facts — `"units": "metric"`,
`"time_format": "24h"` — and leaves the model to *infer* the behavioural
consequences. Local models mostly don't: trained overwhelmingly on
US-convention text, they fall back to miles, Fahrenheit, dollars, "3:00 PM",
and 07/21/2026 (which a DD/MM reader parses as a different day). A fact is
the wrong prompt shape for a behaviour; an instruction with an example is the
shape instruction-tuned models are built to follow.

Two gaps are not expressible in the profile at all today:

- **Number formatting.** `1,234.56` vs `1.234,56` is not a field, yet it
  changes the *meaning* of every number containing a separator — the one
  formatting error that silently corrupts information rather than just
  looking foreign.
- **The recipient's knowledge.** Nothing tells the assistant what the
  operator knows. So it explains fundamentals to an expert (patronising,
  wastes the context budget on boilerplate) and answers a novice in
  unexplained jargon (useless). Worse, it cannot know that competence and
  preference diverge: someone can be expert in a technology and still avoid
  it — proposing solutions built on it is technically-correct and wrong.
  Memory claims accrue fragments of this over time, but accrual is slow,
  fuzzy, and unreviewable at a glance; a declared, structured survey is the
  authoritative source the operator can read and fix in one screen.

## Relationship to the existing prompt blocks

Third and fourth members of the "about the operator" family; no overlap:

| Block | Content | Source | Shape |
|---|---|---|---|
| `<operator_identity>` | who the operator is | `profile.current` fields | JSON facts |
| `<operator_profile>` | what memory has accrued | active `memory_claim` rows | provenance-tagged digest |
| **`<formatting_guide>` (new)** | **how to format replies** | **`profile.current` locale fields** | **imperative directives + examples** |
| **`<operator_knowledge>` (new)** | **what the recipient knows per topic** | **`profile.current` knowledge rows** | **calibration legend + one line per topic** |

The identity block keeps rendering the raw fields (they answer "what is my
timezone?"); the guide is the behavioural reading of the same fields. Both
new blocks follow `profile.current`, so switching the active person profile
switches formatting and calibration in the same action — the demo templates
already carry per-locale formats, so a profile switch is also the test rig.

## Part 1 — the formatting guide block

### New registry field: `number_format`

One addition to the "Locale & formats" group in `profile_fields.py`:

```python
Field("number_format", "Locale & formats", kind="enum", label="Number format",
      choices=("1,234.56", "1.234,56", "1 234,56", "1'234.56")),
```

The choices are their own preview (same trick as the date enums: the sample
value 1234.56 makes decimal separator and grouping readable at a glance), and
four shapes cover the conventions the template countries use. The existing
form preview line grows the number sample. The built-in templates each gain
the value typical for their locale (US `1,234.56`, Germany `1.234,56`,
France `1 234,56`, …) — same duplicate-and-adjust story as every other
locale field.

### Directives, not facts

New module `user_profile/guide.py`, sibling of `identity.py`:
`build_formatting_block()` reads `current_profile()` and renders one
directive per *filled* locale field. Every directive is imperative, carries a
worked example computed from the field's actual value, and where the failure
mode is known, names it. Rendered from the Germany template
(Karl Weierstraß — metric, 24h, DD.MM.YYYY, de/en, EUR, Europe/Berlin,
`1.234,56`):

```
<formatting_guide>
Format replies for this operator as follows:
- Dates: DD.MM.YYYY — e.g. 31.12.2026. Never month-first.
- Times: 24-hour clock — e.g. 23:59, not 11:59 pm. Give times in
  Europe/Berlin; name the zone when another zone is in play.
- Units: metric (km, kg, °C). When a source uses imperial, convert and give
  metric first.
- Numbers: decimal comma, point grouping — e.g. 1.234,56.
- Currency: EUR, formatted per the number rule — e.g. 1.234,56 EUR. Keep
  foreign amounts in their own currency; add an approximate EUR figure when
  useful, marked as approximate.
- Language: reply in the language the operator writes in. Primary de,
  secondary en.
</formatting_guide>
```

Rendering rules:

- **Sparse, like everything else.** A directive renders only when its source
  field is set; no fields set → empty string, no header — callers inject
  unconditionally, same contract as `build_identity_block()`.
- **Derived, never free-typed.** Each example is computed from the enum value
  (the date example re-uses the form preview's fixed unambiguous samples:
  31 December, 23:59, 1234.56), so the guide can never contradict the
  profile.
- **Spelling variant comes from the language tag.** When a language field
  carries a regioned English tag, the language directive appends the spelling
  rule: `en-GB` → "use British spelling (colour, organise)", `en-US` →
  American. A bare `en` says nothing — the profile page's
  fields-never-constrain-each-other rule applies; nothing is inferred from
  `country`.
- **Currency uses the ISO code, not the symbol.** Symbol placement and
  spacing vary by locale in ways a fourth enum would have to carry;
  `1.234,56 EUR` is unambiguous everywhere and needs no new field. (A
  `currency_symbol_position` field is an explicit non-goal until someone
  actually wants it.)
- **Timezone directive is presentation-only.** It tells the model which zone
  to *speak in*; actual current-time answers still come from the runtime
  context / tools, which already carry the real clock.
- **Small and capped.** All directives together stay under
  `MAX_FORMATTING_BLOCK_CHARS = 900`; the renderer asserts this in tests
  rather than truncating (the block is bounded by construction — seven
  directives of fixed shape).

### Injection

`agents/assistant.py` injects `<formatting_guide>` immediately after
`<operator_identity>` (identity says who, the guide says how to write for
them), via a `_build_formatting_block()` mirroring `_build_identity_block()`
— best-effort, `""` on failure, no LLM calls, one indexed read that
`current_profile()` already performs. The identity block is assistant-only
today; the guide follows it, and both migrate to the chat agents together via
`agents/chat_context.py` when that happens (out of scope here, one seam).

No new off-switch: an unset `profile.current` or empty locale fields already
mean "no block", which is the same lever every other profile-driven feature
uses.

## Part 2 — the knowledge survey

### Data model

Knowledge rows live in the profile's existing `data` JSONB under a new
`knowledge` key — an ordered list of row objects, validated by a sub-registry
in `profile_fields.py`:

```python
# profile_fields.py
KNOWLEDGE_LEVELS = ("expert", "intermediate", "beginner", "none")
KNOWLEDGE_PRACTICE = ("daily", "sometimes", "rarely", "avoiding")

KNOWLEDGE_COLUMNS = [
    Field("topic",    "Knowledge", kind="text", label="Topic",
          datalist="topic",
          hint="Anything — a language, a tool, a domain: “PostgreSQL”, "
               "“sailing”, “tax law”."),
    Field("level",    "Knowledge", kind="enum", label="Level",
          choices=KNOWLEDGE_LEVELS),
    Field("practice", "Knowledge", kind="enum", label="Practice",
          choices=KNOWLEDGE_PRACTICE),
    Field("note",     "Knowledge", kind="text", label="Note",
          hint="The nuance the enums can't carry — preferred alternatives, "
               "adjacent experience, what to skip."),
]
```

```json
"knowledge": [
  {"topic": "Mathematics", "level": "expert", "practice": "daily",
   "note": "prefers rigorous definitions over analogies"},
  {"topic": "Python", "level": "beginner", "practice": "sometimes",
   "note": "knows the concepts from other languages; wants idiomatic examples, not theory"},
  {"topic": "Woodworking", "level": "intermediate", "practice": "rarely"}
]
```

Design decisions baked in:

- **Two enums plus a note, not a competence matrix.** `level` is what they
  know; `practice` is whether they currently use it — the two axes that
  change assistant behaviour independently (an expert who is `avoiding` a
  technology should not be handed solutions built on it; a beginner using
  something `daily` wants working recipes now and theory later). Everything
  subtler — preferred alternatives, transferable experience, taste — goes in
  `note`, which the model reads verbatim. More enums would add form friction
  without adding machine-legible signal beyond what the note already carries.
- **`topic` and `level` required per row; `practice` and `note` optional.**
  A topic without a level is not a survey answer. Blank optional values are
  canonicalized away per key like every other field; a row that is entirely
  blank is dropped; a row with content but no topic is a 400 naming the row
  index.
- **Topics are free text with a datalist**, never a closed set: the shipped
  `topic` datalist seeds common programming languages, databases, and
  operating systems *plus* deliberately non-technical entries (cooking,
  gardening, personal finance, first aid) so the survey reads as "what do you
  know", not "which stacks do you use". An unlisted topic is one keystroke
  away, per the datalist convention.
- **Row order is priority order.** Rows render into the prompt in stored
  order and truncate from the end, so the operator's ordering *is* the
  drop order. The editor gets ↑/↓ buttons per row; no drag machinery.
- **Bounded.** `MAX_KNOWLEDGE_ROWS = 100` at the validator (400 beyond) —
  far above any real survey, low enough that the blob and the editor stay
  sane.

### Survey editor

No new page. The `/profile` form pane grows a fourth fieldset, "Knowledge",
after "Contact & location": one row per entry — topic input (datalist),
level select, practice select, note input, ↑ / ↓ / ✕ — plus an "Add topic"
button. Edits ride the existing 400 ms debounced whole-snapshot autosave and
the existing per-profile PUT; no new endpoints, no summary change, no tree
payload change. Duplicate already deep-copies `data`, so it copies the survey
for free. Built-in templates render the fieldset disabled like everything
else.

Two built-in templates gain three example rows each (the ones above for
Germany/Weierstraß; a matching trio for Japan/Yukawa — "Physics: expert,
daily", "Go (board game): intermediate, sometimes", "JavaScript: none"), so
the templates keep being the living documentation of a filled-in profile.
All template details stay fictional per the no-real-PII policy; only the
dead-namesake names are historical.

A conversational alternative — the assistant interviewing the operator topic
by topic and writing rows — is deliberately *not* part of v1, but the data
model is designed for it: a future skill or write-intent flow fills the same
`knowledge` list through the same validator. The survey is the contract; a
wizard is sugar on top.

### The knowledge block

`user_profile/guide.py` also provides `build_knowledge_block()`: a fixed
calibration legend, then one line per row in stored order, under
`MAX_KNOWLEDGE_BLOCK_CHARS = 1500` with an honest truncation line (no silent
caps):

```
<operator_knowledge>
The operator's self-assessed knowledge. Calibrate each answer to the topic's
level:
- expert: skip fundamentals; be dense; jargon is welcome.
- intermediate: normal depth; explain only the unusual parts.
- beginner: define terms; go step by step; check assumptions.
- none: assume nothing; start with what it is and why it matters.
Practice "avoiding" means: do not propose solutions built on this unless
asked — the note usually names what they prefer instead.
Topics not listed carry no signal either way; when depth matters and the
topic is unlisted, ask.
- Mathematics: expert, daily — prefers rigorous definitions over analogies.
- Python: beginner, sometimes — knows the concepts from other languages;
  wants idiomatic examples, not theory.
- Woodworking: intermediate, rarely.
(3 more topics omitted for space)
</operator_knowledge>
```

- The legend renders once and only when at least one row exists; empty survey
  → empty string, no header.
- **The "not listed" rule is load-bearing.** Without it the model
  extrapolates ("expert in one language → expert in all"); with it, absence
  is explicitly no-signal, and the note is where the operator declares
  transfer ("knows the concepts from other languages") when it is real.
- **Whole-survey injection, not retrieval.** At ≤ 100 one-line rows this is
  a budget question, not a search question: the cap plus operator-ordered
  truncation keeps it bounded, and every influence stays trivially
  explainable (the block *is* the survey). Per-query relevance selection via
  pgvector — embed rows, retrieve topically like the skills block — is the
  designed escape hatch if evals show the flat block distracting small
  models, and slots in behind the same `build_knowledge_block()` signature.
  It is not built now: it adds an embedding lifecycle (repopulate on edit)
  for data that fits in 1500 chars.

Injection: after `<formatting_guide>`, same best-effort seam. Identity →
guide → knowledge → memory digest reads as: who they are, how to write for
them, what they know, what we've learned.

## Sensitivity

The survey is exactly as exposed as the rest of the profile: it rides
`profile.current`, so whatever audience sees the identity block sees the
knowledge block. That is today's contract, not a new leak — but a survey
("what the operator is bad at") can feel more personal than a timezone, so
the operator lens/audience work (`2026-07-07-operator-profiles-and-working-
context.md`) should treat profile-derived blocks as one unit when it gains
per-audience suppression. Nothing here blocks that; a demo profile simply
carries a fictional survey.

## Phasing

1. **Registry + editor.** `number_format` field, `knowledge` sub-registry and
   validator growth, the Knowledge fieldset with add/remove/reorder riding
   the existing autosave, datalist seeding, template updates (number formats
   everywhere, example surveys on two).
   *Acceptance:* survey rows round-trip through the PUT; validator names the
   offending row on bad input; row order survives save/reload; ↑/↓/✕ verified
   in a real browser per the tree doc's §8 rule; templates validate at test
   time.
2. **Prompt blocks.** `user_profile/guide.py`, both builders, assistant
   injection after `<operator_identity>`, deterministic tests below.
   *Acceptance:* with the Germany template active, the assembled prompt
   contains the directives above verbatim-modulo-values; with `profile.current`
   unset, neither block appears.
3. **Measure.** Eval cases per `docs/eval-loop.md`: a metric/24h/EUR profile
   answer scored with `must_not_include` markers (`"°F"`, `" mph"`,
   `" PM"`, `"$"`) and `must_include` counterparts; a beginner-topic answer
   must define its jargon; an `avoiding`-topic prompt must not propose that
   technology unprompted. Existing chat-reply cases must not regress. If the
   knowledge block measurably hurts small models, the fallback order is:
   shrink the legend → cap rows harder → per-query retrieval (the escape
   hatch above).

## Tests (deterministic, no live LLM)

1. **Validator:** `number_format` out-of-enum → 400; `knowledge` must be a
   list of objects; unknown row key → 400 naming it; missing `topic`/`level`
   → 400 naming the row; blank optional values canonicalized away; all-blank
   rows dropped; > `MAX_KNOWLEDGE_ROWS` → 400; existing flat-field behaviour
   unchanged.
2. **Formatting builder:** full profile → every directive present with
   examples derived from the actual enum values (`DD.MM.YYYY` + `24h` +
   `1.234,56` produce exactly those sample strings); sparse profile → only
   matching directives; `en-GB` adds the British-spelling clause, bare `en`
   does not; empty profile → `""`; block under its cap for the maximal
   profile.
3. **Knowledge builder:** rows render in stored order; over-budget survey
   truncates from the end with the "(N more topics omitted)" line; empty
   survey → `""`; legend appears exactly once.
4. **Prompt assembly:** with a scripted fake model, the user prompt carries
   `operator_identity`, then `formatting_guide`, then `operator_knowledge`;
   unset `profile.current` → none of the three.
5. **Templates:** every shipped template still passes
   `validate_profile_data`, including the new `number_format` values and
   example surveys (a release cannot ship a broken template).
6. **Views:** marker tests for the Knowledge fieldset and the `topic`
   datalist (presence, not behaviour — §8 covers behaviour); any inline
   script in the non-raw Python template contains no bare `\n`-style escapes.

## Alternatives considered

- **Keep facts-only and prompt harder elsewhere** — rejected by evidence:
  the identity block already states the facts and the misformatting is the
  observed behaviour. Directives-with-examples is the cheapest intervention
  that matches how instruction-tuned models actually generalize.
- **One locale code (`da-DK`) deriving everything via CLDR** — rejected: it
  re-couples exactly what the profile page decoupled (a European profile
  choosing ISO dates is first-class), drags in a CLDR dependency for seven
  directives, and hides the operator's choices behind a code they'd have to
  decode. The explicit per-field enums *are* the design.
- **A hand-written style prompt on `/prompt`** — available today and kept as
  the free-form escape hatch, but rejected as the mechanism: it doesn't
  derive from the structured fields (drift), can't be validated, and doesn't
  follow `profile.current` when the active person changes.
- **Skill levels as memory claims** — rejected as the source of truth:
  memory accrues slowly, ranks fuzzily, and can't be reviewed in one screen.
  The survey is declared and authoritative; memory remains free to *cite*
  survey topics in accrued claims, and a future deriver could propose survey
  rows as candidates — through the validator, like the wizard.
- **A separate `/survey` page** — rejected: knowledge belongs to a person,
  and a second tree page duplicates the whole chrome to hold one fieldset
  the form pane can carry.
- **Embedding-based topic retrieval in v1** — deferred, not rejected; see
  the knowledge-block section for the trigger and the seam.
- **Richer per-row schemas** (years of experience, interest, last-used
  dates, certifications) — rejected for v1: every added column multiplies
  form friction across all rows, and the note field carries the long tail at
  zero schema cost. Revisit only if notes are observed straining to encode
  the same structure repeatedly.

## See also

- [`2026-07-14-user-profile-page.md`](2026-07-14-user-profile-page.md) — the
  `/profile` page and registry this extends; its Phase 2 ("formats steering
  assistant output") is Part 1 of this proposal.
- [`2026-06-20-phase3-user-profile.md`](2026-06-20-phase3-user-profile.md) —
  the memory-derived `<operator_profile>` digest these blocks sit beside.
- [`2026-07-07-operator-profiles-and-working-context.md`](2026-07-07-operator-profiles-and-working-context.md)
  — the audience lens that should eventually gate all profile-derived blocks
  as one unit.
- `docs/profile-design.md`, `docs/assistant-design.md` — the page and the
  prompt assembly being extended; `docs/eval-loop.md` — the measurement path
  for Phase 3.
