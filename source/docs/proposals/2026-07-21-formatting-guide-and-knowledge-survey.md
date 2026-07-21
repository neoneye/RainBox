# Formatting guide and pluggable surveys

**Status: proposal.** Two coupled features, both derived from the person
profile selected by `profile.current`:

1. **The formatting guide block** — a deterministic prompt section that turns
   the active profile's locale fields (units, date/time formats, timezone,
   language, currency, and a new number-format field) into imperative
   *directives with worked examples*, so replies stop defaulting to imperial
   units, USD, 12-hour clocks, and MM/DD dates for an operator whose profile
   says otherwise.
2. **The survey engine** — surveys as *plugins*: each survey is a definition
   file (shipped with rainbox, or operator-authored in the customize dir and
   therefore never public), its responses live on the person profile, and
   each definition declares its own visibility gate and prompt-injection
   mode. The first shipped survey is the **knowledge survey** — topics with
   a self-assessed level, current practice, and a free-text note — rendered
   into a second prompt block so the assistant pitches each answer at what
   the recipient actually knows. Operator-authored surveys can cover
   territory that must never surface to a demo or friends audience —
   health, relationships, intimate topics — and the engine treats "locked
   means invisible" as a structural property, not a UI courtesy.

They are one proposal because they share the entire delivery path: both read
the `profile.current` profile, both are rendered by code in `user_profile/`,
and both are injected at the same point in prompt assembly. The formatting
guide fixes *how* replies are written; the surveys fix *at what level* and
*with what personal context*.

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
| **`<survey_knowledge>` (new)** | **what the recipient knows per topic** | **the shipped knowledge survey's responses** | **calibration legend + one line per topic** |
| **`<survey_…>` (new, per survey)** | **one survey's responses, when its gate allows** | **that survey's definition + responses** | **definition-declared rendering** |

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

## Part 2 — the survey engine

### Surveys are plugins

A survey is two things: a **definition** (what is asked, how it renders, who
may see it) and the **responses** (one set per person profile). Definitions
are files, loaded from two places with the established shipped-vs-customize
split:

```
data/surveys/<id>.json               # shipped with rainbox: knowledge.json
<customize.dir>/surveys/<id>.json    # operator-authored — never in the repo
```

This is the same plugin pattern as the Q&A overlay and `operators/*.json`:
shipped definitions update with every release and are validated at test time
(a release cannot ship a broken survey); operator-authored definitions are
private *by location* — the customize dir is the operator's own, so a survey
about intimate territory never touches the public repo, and deleting the file
retires the survey without touching its stored responses. Definitions are
validated at load; a broken operator file is reported on `/doctor` and
skipped, never crashing the page.

Every definition declares, besides its questions:

- `id` — stable key; responses live under `data["surveys"][<id>]` on the
  profile, so definitions and responses can evolve independently.
- `title` — the fieldset/wizard heading.
- `style` — `"rows"` or `"questions"` (below).
- `shield` — optional lock alias from the existing shield vocabulary. **A
  survey whose shield is locked does not exist** for the current audience:
  it is absent from the editor, absent from every API response (title
  included — the *name* of an intimate survey is itself sensitive), and
  absent from prompt assembly. Empty `shield` means ungated. When the
  operator-lens work lands, lens presets gate surveys with the same aliases,
  as one unit with the other profile-derived blocks.
- `inject` — `"always"` (every assistant turn, like knowledge),
  `"on_request"` (available to an explicit retrieval action, injected only
  when the conversation calls for it), or `"never"` (an editable record the
  prompt never sees). Sensitive surveys will usually want `on_request` or
  `never` — even to the operator themself, a survey about their intimate
  life does not belong in every "what's the weather" prompt.

### Two styles

**`rows`** — open-ended repeating rows; the *operator* supplies the subjects.
The definition declares the columns (same kinds as the field registry:
`text` with optional datalist, `enum` with choices, `required` flag). The
knowledge survey is rows-style.

**`questions`** — a fixed interview bank; the *author* supplies the
questions. The definition declares `areas`, each with `id`, `title`,
`intro`, and typed questions: `single_choice`, `multi_choice`, `scale` (one
willingness/preference scale, declared once per definition, applied to a
question's list of items — a compact matrix), `year`, `country`, `text`,
plus an optional `show_if` (`question_id` + exactly one of
`equals`/`not_equals`) so follow-ups appear only when relevant. This shape
is chosen deliberately so an existing interview-style question bank ports
into a definition file with a rename and a header stanza. A fictional
example, abridged:

```json
{
  "id": "taste",
  "title": "Food & taste",
  "style": "questions",
  "shield": "taste",
  "inject": "on_request",
  "scale": ["favorite", "enjoy", "tried-mixed", "curious", "dislike"],
  "areas": [
    {"id": "cuisines", "title": "Cuisines", "intro": "How do these sit with you?",
     "questions": [
       {"id": "cuisine_ratings", "type": "scale", "label": "Rate each",
        "items": [{"id": "sichuan", "label": "Sichuan"},
                  {"id": "smorrebrod", "label": "Smørrebrød"}]},
       {"id": "spice_ceiling", "type": "single_choice", "label": "Spice ceiling",
        "options": ["mild", "medium", "hot", "extreme"],
        "show_if": {"question_id": "cuisine_ratings.sichuan", "not_equals": "dislike"}}
     ]}
  ]
}
```

Response documents store per-question entries keyed by question id (scale
items as `qid.itemid`), each `{"value": …}` or `{"skipped": true}` — an
explicit skip is an answer ("prefer not to say") and stops the wizard from
re-asking, which matters doubly for sensitive surveys.

### The knowledge survey — the first shipped definition

`data/surveys/knowledge.json`: rows-style, no shield, `inject: "always"`.

```json
{
  "id": "knowledge",
  "title": "Knowledge",
  "style": "rows",
  "inject": "always",
  "columns": [
    {"key": "topic", "kind": "text", "label": "Topic", "datalist": "topic",
     "required": true,
     "hint": "Anything — a language, a tool, a domain: “PostgreSQL”, “sailing”, “tax law”."},
    {"key": "level", "kind": "enum", "label": "Level", "required": true,
     "choices": ["expert", "intermediate", "beginner", "none"]},
    {"key": "practice", "kind": "enum", "label": "Practice",
     "choices": ["daily", "sometimes", "rarely", "avoiding"]},
    {"key": "note", "kind": "text", "label": "Note",
     "hint": "The nuance the enums can't carry — preferred alternatives, adjacent experience, what to skip."}
  ]
}
```

Responses on a profile:

```json
"surveys": {
  "knowledge": [
  {"topic": "Mathematics", "level": "expert", "practice": "daily",
   "note": "prefers rigorous definitions over analogies",
   "updated": "2026-03-02"},
  {"topic": "Python", "level": "beginner", "practice": "sometimes",
   "note": "knows the concepts from other languages; wants idiomatic examples, not theory",
   "updated": "2026-07-21"},
  {"topic": "Woodworking", "level": "intermediate", "practice": "rarely",
   "updated": "2025-11-14"}
  ]
}
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
- **Topics are unique per profile** (case-insensitive, whitespace-trimmed):
  a duplicate topic is a 400 naming both row indices. Two rows about the
  same topic can only contradict each other, and uniqueness is what lets the
  freshness stamping below key rows by topic.
- **Each row carries a server-owned `updated` date.** Same ownership pattern
  as `dynamic`: GET returns it, the human PUT rejects it if submitted, the
  client strips it before saving. The server stamps it — see "A living
  survey" below.
- **Row order is priority order.** Rows render into the prompt in stored
  order and truncate from the end, so the operator's ordering *is* the
  drop order. The editor gets ↑/↓ buttons per row; no drag machinery.
- **Bounded.** `MAX_SURVEY_ROWS = 100` per rows-style survey at the
  validator (400 beyond) — far above any real survey, low enough that the
  blob and the editor stay sane.

The uniqueness, required-column, blank-canonicalization, and stamping rules
above are engine rules stated through the knowledge survey: they apply to
every rows-style survey (uniqueness keys on the first required text column).

### Storage and API — surveys leave the flat snapshot

Responses live under `data["surveys"][<id>]` on the profile row, but they do
**not** ride the flat-field data PUT. Two reasons, one structural: a locked
survey is absent from the client's view, so a whole-snapshot PUT could not
round-trip data the client never saw without either leaking it or deleting
it; and per-survey saves keep an autosave in one fieldset from 400-failing an
unrelated one. So, following the precedent that `data` already contains
server-merged subtrees (`dynamic`):

- The flat data PUT continues to cover registry fields only; a submitted
  `surveys` key is rejected read-only, and the server preserves the stored
  subtree on every flat save — same merge discipline as `dynamic`.
- `GET /profile/api/surveys` → the *visible* definitions (locked ones
  absent), for building the editor.
- `GET /profile/api/profiles/<uuid>/surveys/<id>` → that survey's responses;
  404 for unknown ids **and** for locked ids (indistinguishable by design —
  a 403 would confirm the survey exists).
- `PUT /profile/api/profiles/<uuid>/surveys/<id>` — complete response
  snapshot for one survey, validated against its definition (rows-style:
  the column rules above; questions-style: typed per-question validation,
  unknown keys and dangling ids rejected, `skipped` honoured), stamped, and
  merged into `data["surveys"]` preserving sibling surveys. Locked → 404.
  Built-in profile templates → 400 read-only, as everywhere.

New module `db/survey.py`: definition loading/validation/caching (shipped +
customize dirs, id collision → the operator file wins is **wrong** here —
colliding with a shipped id is a load error, so a release can never silently
shadow an operator's private survey or vice versa), response validation per
style, and the merge-preserving update. Duplicate-profile keeps copying the
whole `data` blob, surveys included.

### Survey editor

No new page. The `/profile` form pane renders one section per *visible*
survey after "Contact & location", in definition order (shipped first, then
operator files alphabetically). A locked survey renders nothing — no
fieldset, no heading, no "unlock to view" placeholder (the placeholder would
itself disclose the survey's existence).

- **Rows-style** renders inline as a fieldset: one row per entry — the
  definition's columns as inputs/selects, ↑ / ↓ / ✕ — plus an add-row
  button. Edits autosave through that survey's own PUT with the same 400 ms
  debounce/retry/unload-guard lifecycle as the flat fields, tracked per
  survey.
- **Questions-style** is too large for an inline fieldset (a real interview
  bank runs to a dozen areas). Its section shows the title, an answered/total
  progress line, and an **Open** button that swaps the form pane to a
  wizard: one area per step, the area's `intro` above its questions, a
  Skip control per question (recording an explicit `{"skipped": true}`),
  `show_if` follow-ups appearing live, autosave per step through the same
  per-survey PUT, and a Back-to-profile link. No modal, no new route —
  `?id=<uuid>&survey=<id>` deep-links a wizard, same URL discipline as the
  tree.

Built-in profile templates render survey sections disabled like everything
else.

Two built-in templates gain three example rows each (the ones above for
Germany/Weierstraß; a matching trio for Japan/Yukawa — "Physics: expert,
daily", "Go (board game): intermediate, sometimes", "JavaScript: none"), so
the templates keep being the living documentation of a filled-in profile.
All template details stay fictional per the no-real-PII policy; only the
dead-namesake names are historical.

### A living survey

A survey answered once and never revisited quietly becomes wrong: knowledge
grows, tools fall out of favour, a language someone was avoiding gets
replaced by a newer one they now prefer. The design treats change as the
normal case, not an afterthought, with three mechanisms in increasing order
of activeness:

- **Editing is the baseline, and it is cheap.** Every row is mutable in
  place through the same form — change a level, rewrite a note, reorder,
  delete — with autosave; updating one opinion costs one select change, not
  a re-take of the survey. There is no versioned history and deliberately so:
  the profile is current-state by contract (a superseded preference is not
  worth preserving in the record that exists to describe *now*; the note can
  say "switched from X in 2026" when the transition itself is signal).
- **The server stamps freshness, so staleness is visible.** On each survey
  PUT the server diffs incoming entries against stored entries — rows-style
  keyed by normalized topic, questions-style keyed by question key: a new
  or changed entry gets `updated` = today; an untouched entry keeps its
  stamp (reordering alone restamps nothing — position is not content).
  Renaming a topic reads as delete + add and starts a fresh stamp, which is
  honest: it *is* a new answer. The editor renders the age unobtrusively per
  row (and per area in the wizard's progress line), so an answer that has
  not been touched in two years announces itself when the operator scrolls
  past. Nothing nags; stamps stay out of the prompt blocks (budget, and the
  model needs the answer, not its changelog).
- **The assistant proposes updates when it hears drift.** Conversation is
  where change surfaces first — "I've stopped using X, Y is faster" is a
  survey edit spoken aloud. When the assistant notices a statement
  contradicting or extending the survey, it proposes the row change through
  the existing write-intent flow (proposed → operator confirms → applied
  through the same validator, stamped like any edit). Same machinery covers
  the interview wizard — the assistant asking topic by topic and filling
  rows. Neither is built in v1; both are why the data model, uniqueness
  rule, and validator are shaped so that *every* writer — form, wizard,
  drift proposal — goes through one gate.

### Survey prompt blocks

`user_profile/guide.py` also provides `build_survey_blocks()`: for each
loaded definition whose shield is unlocked *and* whose `inject` is
`"always"`, it renders one `<survey_<id>>` block from the active profile's
responses. The knowledge survey renders with a fixed calibration legend,
then one line per row in stored order, under
`MAX_SURVEY_BLOCK_CHARS = 1500` per survey with an honest truncation line
(no silent caps):

```
<survey_knowledge>
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
</survey_knowledge>
```

The knowledge legend lives in the shipped definition (a `legend` string), not
in code — any survey may declare one. A questions-style survey renders
generically: area titles as sub-headers, one `- <label>: <value>` line per
answered question (`skipped` entries and unanswered questions omitted),
scale items inline under their question. That generic rendering plus the
legend hook covers v1; a definition-supplied template language is explicitly
not offered (an operator wanting bespoke prose can put it in the legend).

`inject: "on_request"` surveys are not rendered here. They surface through
an explicit retrieval step instead — the designed seam is an assistant
action (`read_survey`, sibling of `query_memory`) whose listed choices are
only the unlocked on-request surveys, so the model can pull the taste survey
when dinner planning comes up and the block never rides along on unrelated
turns. The action ships in a later phase; until then `on_request` behaves
like `"never"` (editable, retrievable by nobody), which is the safe default
direction.

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
  models, and slots in behind the same `build_survey_blocks()` signature.
  It is not built now: it adds an embedding lifecycle (repopulate on edit)
  for data that fits in 1500 chars.

Injection: the always-blocks render after `<formatting_guide>`, same
best-effort seam. Identity → guide → surveys → memory digest reads as: who
they are, how to write for them, what they know, what we've learned.

## Sensitivity

Storage and disclosure are deliberately different questions. Responses are
stored at full fidelity in the profile row — privacy here is *who may see
what, when*, enforced in layers, never "store less":

- **The repo never sees a private survey.** An operator-authored definition
  lives only in the customize dir; its questions — often as revealing as any
  answer — stay off the public record entirely.
- **Locked means non-existent.** A locked survey is absent from the editor,
  absent from `GET /profile/api/surveys`, 404 (not 403) on direct response
  fetch, and absent from prompt assembly. Its title never renders, because
  the existence of a survey named after an intimate topic is itself a
  disclosure.
- **`inject` bounds exposure to the model.** Even fully unlocked, a
  sensitive survey defaults away from `"always"`: `on_request` surveys reach
  the prompt only through an explicit retrieval step on turns that need
  them, and `"never"` surveys are records the prompt never sees.
- **Honest limits, same as the lens proposal:** shields are not a security
  boundary until the auth work lands (any local caller can flip settings) —
  they protect against *accidents in front of a trusted audience*. And
  nothing is retroactive: once survey content has entered a chat, the
  transcript has it; the recipe for an untrusted audience remains a separate
  database. The knowledge survey ("what the operator is bad at") is milder
  but still personal — when the operator-lens work lands per-audience
  suppression, all profile-derived blocks gate as one unit, surveys by
  their own shields.

## Phasing

1. **Engine + knowledge survey + editor.** `number_format` field;
   `db/survey.py` (definition loading from both dirs, id-collision load
   error, per-style response validation, stamping, merge-preserving update);
   the per-survey API; the shipped `knowledge.json`; rows-style editor
   sections with add/remove/reorder and per-survey autosave; shield gating
   end to end (locked → invisible everywhere); datalist seeding; template
   updates (number formats everywhere, example knowledge rows on two).
   *Acceptance:* survey rows round-trip through their own PUT; validator
   names the offending row on bad input; row order survives save/reload;
   editing one row restamps only that row and the editor shows the new age;
   a flat-field autosave never touches the surveys subtree; with a shield
   locked, the survey's title appears nowhere in any API response or page;
   ↑/↓/✕ verified in a real browser per the tree doc's §8 rule; templates
   and shipped definitions validate at test time.
2. **Prompt blocks.** `user_profile/guide.py`, the formatting builder and
   `build_survey_blocks()`, assistant injection after `<operator_identity>`,
   deterministic tests below.
   *Acceptance:* with the Germany template active, the assembled prompt
   contains the directives above verbatim-modulo-values; with
   `profile.current` unset, no new block appears; a locked or
   non-`always` survey never reaches the prompt.
3. **Questions-style wizard + `read_survey`.** The area-stepped wizard
   (skip, `show_if`, per-step autosave, deep link), generic questions-style
   rendering, and the `read_survey` assistant action listing only unlocked
   on-request surveys.
   *Acceptance:* a definition file dropped into the customize dir appears in
   the editor on reload and ports an interview-style bank without code
   changes; skipped questions are never re-asked; the action's choice list
   provably excludes locked surveys.
4. **Measure.** Eval cases per `docs/eval-loop.md`: a metric/24h/EUR profile
   answer scored with `must_not_include` markers (`"°F"`, `" mph"`,
   `" PM"`, `"$"`) and `must_include` counterparts; a beginner-topic answer
   must define its jargon; an `avoiding`-topic prompt must not propose that
   technology unprompted; a scripted turn under a locked shield must not
   leak survey content (extend the existing forbidden-memories style of
   case). Existing chat-reply cases must not regress. If the knowledge
   block measurably hurts small models, the fallback order is: shrink the
   legend → cap rows harder → per-query retrieval (the escape hatch above).

## Tests (deterministic, no live LLM)

1. **Definitions:** shipped `knowledge.json` validates; a customize-dir
   definition loads and lists; an id colliding with a shipped id → load
   error surfaced on `/doctor`, page still serves; a malformed operator file
   is skipped with the error reported; questions-style definition rules
   enforced (duplicate question/area ids, dangling `show_if`, choice
   questions need options).
2. **Response validator:** `number_format` out-of-enum → 400; rows-style —
   unknown row key → 400 naming it; missing required column → 400 naming the
   row; duplicate topics (case-insensitive, trimmed) → 400 naming both rows;
   a submitted `updated` → 400 (server-owned); blank optional values
   canonicalized away; all-blank rows dropped; > `MAX_SURVEY_ROWS` → 400.
   Questions-style — unknown question key → 400; out-of-scale token, bad
   year, non-option choice each → 400; `{"skipped": true}` accepted
   anywhere. Flat data PUT: a submitted `surveys` key → 400 read-only; a
   flat save preserves the stored surveys subtree byte-for-byte.
3. **Lock gating:** with a shield locked — definition absent from
   `GET /profile/api/surveys`; response GET/PUT → 404 identical to an
   unknown id; `build_survey_blocks()` output empty for that survey;
   unlocking restores all three without restart.
4. **Freshness stamping:** a new entry gets today's `updated`; an edited
   entry restamps that entry and only that entry; an untouched entry keeps
   its stamp across saves; pure reordering restamps nothing; a renamed topic
   starts a fresh stamp; GET returns stamps and a subsequent client snapshot
   (stamps stripped) round-trips them unchanged.
5. **Formatting builder:** full profile → every directive present with
   examples derived from the actual enum values (`DD.MM.YYYY` + `24h` +
   `1.234,56` produce exactly those sample strings); sparse profile → only
   matching directives; `en-GB` adds the British-spelling clause, bare `en`
   does not; empty profile → `""`; block under its cap for the maximal
   profile.
6. **Survey blocks:** rows render in stored order; over-budget survey
   truncates from the end with the "(N more topics omitted)" line; empty
   survey → `""`; legend appears exactly once; `updated` stamps never appear
   in any block; a questions-style survey renders answered questions only,
   skipped omitted; `on_request`/`never` surveys are never in the output.
7. **Prompt assembly:** with a scripted fake model, the user prompt carries
   `operator_identity`, then `formatting_guide`, then `survey_knowledge`;
   unset `profile.current` → none of the three.
8. **Templates:** every shipped template still passes
   `validate_profile_data`, including the new `number_format` values, and
   its knowledge rows pass the shipped definition's validator (a release
   cannot ship a broken template).
9. **Views:** marker tests for the survey sections and the `topic`
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
- **A separate `/survey` page** — rejected: survey responses belong to a
  person, and a second tree page duplicates the whole chrome to hold
  sections the form pane can carry (the questions-style wizard is a pane
  view, not a page).
- **Surveys hardcoded in `profile_fields.py`** — rejected the moment
  privacy entered: a Python registry ships with the repo, and a private
  survey's *questions* are as sensitive as its answers. Definitions must be
  data files so the private ones can live where private data lives — the
  customize dir.
- **A standalone private webapp per sensitive survey** — the strongest
  competitor, because it is absolute isolation and needs nothing from
  rainbox. Rejected as the destination (kept as a valid staging ground): a
  separate app's responses are invisible to the assistant, unlocked by no
  shield, rendered by no lens, and stamped by no shared lifecycle. The
  customize-dir definition gives the same "never in the repo" property
  *with* the integration — and an interview bank built in such an app ports
  into a definition file by design.
- **Per-survey encryption at rest** — out of scope: disclosure control here
  is audience layering (shields, inject modes, lens ceilings), not reduced
  or scrambled storage; at-rest encryption is a whole-database operational
  decision and belongs to the security work.
- **Embedding-based topic retrieval in v1** — deferred, not rejected; see
  the knowledge-block section for the trigger and the seam.
- **Richer per-row schemas** (years of experience, interest, last-used
  dates, certifications) — rejected for v1: every added column multiplies
  form friction across all rows, and the note field carries the long tail at
  zero schema cost. Revisit only if notes are observed straining to encode
  the same structure repeatedly.
- **Versioned survey history** (keep superseded rows, or an audit trail of
  level changes) — rejected: the profile is a current-state record by
  contract, and a history table for self-assessments is machinery without a
  consumer. The `updated` stamp answers the one historical question that has
  a consumer ("how stale is this answer?"); a transition worth remembering
  belongs in the note or in memory claims, which already carry provenance.
- **A periodic "re-take the survey" nudge** (cron reminder, working-context
  line) — rejected for v1: cadence-based nagging assumes knowledge decays on
  a schedule, which it doesn't. The visible per-row age plus
  drift-triggered proposals (the assistant reacting to what the operator
  actually says) target the rows that *are* stale instead of interrupting on
  the ones that aren't.

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
