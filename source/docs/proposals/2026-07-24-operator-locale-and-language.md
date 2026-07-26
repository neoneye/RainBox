# Operator locale and language — execution semantics, not output formatting

**Status:** Partially implemented on `main`. The multilingual profile model
and editor are complete; response-language intent resolution is the next
unsolved stage. The `acceptance-criteria-v2` branch remains **parked**.
**Date:** 2026-07-24
**Last updated:** 2026-07-25
**Supersedes:** `2026-07-24-step0-language-findings.md` and
`2026-07-24-modelling-language-preferences.md` (folded in here).

## The Problem

I often encounter that LLMs assume `inch` instead of metric, prefers AM/PM over 24 hour.
Making the LLM understand that I'm not an american, that's my struggle.

I'm the developer of RainBox. Most of the text I read/write is american english.
I'm danish. A minority of the text I write daily is in danish. So I use the metric system. 
I live in Copenhagen. I use DKK and EUR. I use 24 hour system.
The code that I'm writing is en american english. I feels cringy seeing code written in danish.
Danish is rarely a supported language in apps, so I prefer to use english, otherwise I see an inconsistent mess of danish/english non-sense.
I don't want thousand separators in numbers, since that makes a mess when I copy paste it into my code.

If I type something in one language, then I expect a reply in the same language.
If I type something in danish, then I expect a reply in danish.

For now my focus is on identifying what language that the assistant is supposed to respond in.
Later focus can be on figuring out what other user settings are relevant for the task.


## The goal, stated plainly

An assistant that knows the operator is European and multilingual — so it
converts to metric, writes 24-hour times and ISO dates, prices in DKK, and
answers in the language and dialect the operator actually wants. The
requirement is that these settings apply **while the assistant reasons**, not
as a cosmetic pass over the final message. A step that estimates in miles and
gets converted afterwards has already made the wrong decision.

## Implementation status — 2026-07-26

| Area | Status | What that means |
|---|---|---|
| Deterministic locale guide | **Implemented** | Metric/Celsius, clock, date, timezone, number and currency directives are code-rendered and available at every reasoning step behind the existing release switch. |
| `languages.rows` storage and editor | **Implemented on `main`** | Ordered BCP-47 rows, `level`, `stance`, notes, validation, autosave, duplication identities, summaries and built-in templates are complete. |
| Flat `language` / `language_2` fields | **Removed** | Clean single-operator cutover: no templates, migration, fallback, API compatibility or prompt reads remain. Existing profiles use only `languages.rows`. |
| Current prompt bridge | **Implemented** | The classifier's reason, score-free language list and audit are rendered as Markdown immediately after the current request for acceptance criteria, second opinion and every decide step. Languages are ordered by descending score with stable ties. |
| `level` and `stance` execution semantics | **Partial** | `prefer` now refines a compatible broad target to the exact profile variant. `avoid` does not yet redirect replies and `level` does not yet change register. |
| Response-language intent classifier | **Implemented on `codex/response-language-classifier`; live results promising** | The assistant's first model-facing activity is a narrow typed call returning `reason`, BCP-47 candidates with 1–5 Likert confidence, and `audit`. It scores every `languages.rows` candidate plus explicit request languages, omits assistant history, persists a trace row and fails open. Broad targets are refined by a compatible preferred profile variant (`English` + preferred `en-GB` → `en-GB`); any deterministic broad-tag repair is disclosed in `audit`. Manual use so far indicates that the classifier is very close to the required behaviour. |
| Deterministic resolver + response-language directive | **Partial** | Later model calls receive ranked Markdown derived from the classifier, with numeric scores omitted. No score threshold or smaller code-composed single/multilingual directive exists yet. |
| English/dialect resolution | **Partial** | `valid_language_tag()` and the existing en-US/en-GB spelling clauses are present. A single general variant table, bare-`en` international-English wording and non-English variant resolution are pending. |
| Structured-result hardening | **Implemented** | The shared structured-call path re-validates final provider text and has dedicated tests. |
| General acceptance-criteria step | **Implemented but default-off; not the chosen language solution** | The broad step-0 mechanism still exists behind `assistant.acceptance_criteria`. Measurements in this proposal do not justify enabling it for language/locale routing. |
| Task-scoped locale | **Not implemented** | Explicit project/document locale still needs a separate design after the response-language path is stable. |
| Post-cutover evals and reply-model selection | **Manual validation promising; systematic eval pending** | Exploratory use covers multilingual-content and translation-intent cases well, including exact `en-GB` refinement. A repeatable corpus for short messages, avoided languages, dialect conflicts and genuinely multilingual replies is still needed before calling it stable. |

The profile slice is merged on local `main` through commit `1385d93`.
Verification at merge: 185 profile/assistant integration tests, Python/JS
syntax checks, template JSON validation and a rendered browser check passed.

The classifier experiment lives on
`codex/response-language-classifier`. It has its own binding-only
`response_language_classifier` role on `/agentmodel`, falling back to the
assistant's model group when unbound. Its trace action is
`response_language_classifier`; the stored model request, response, concise
reason, score list, audit, model identity and duration make model comparisons
inspectable. Later assistant calls receive a Markdown projection with the
reason, ranked language tags and audit, but not the numeric scores.

### Current assessment — 2026-07-26

The classifier is now close to the operator's target in live use. It correctly
distinguishes the language of the reply from foreign-language content inside
the request, handles translation intent, and uses the compatible preferred
profile dialect rather than collapsing it to a broad language tag. The observed
`English` → `en` failure was traced to the output contract and corrected:
preferred `en-GB` now remains `en-GB`.

Its downstream representation is intentionally small:

```xml
<reply_language_markdown authority="context" format="markdown">
## Reason
...

## Languages - highest confidence first
- `en-GB`
- `da`

## Audit
OK
</reply_language_markdown>
```

The structured trace retains the Likert scores for evaluation. The Markdown
does not show them; descending list order carries confidence and equal scores
retain the classifier's original order. Acceptance criteria, second opinion
and every decide step see this block immediately after `current_request`.

What remains is evidence rather than another prompt redesign: build the
repeatable edge-case corpus, confirm that ranked Markdown produces reliable
downstream language delivery, and decide whether a score threshold is needed
for genuinely multilingual replies.

## Part 1 — what is already solved (do not rebuild it)

Verified by dumping a **mid-loop** prompt (step 3, two tool steps already
taken) from the live profile:

```
sections: current_request, conversation_history, current_turn_steps,
          decision_request, user_settings_json, formatting_guide,
          current_local_time
metric YES · Celsius YES · 24-hour YES · YYYY-MM-DD YES · DKK YES ·
Europe/Copenhagen YES
```

- **The deterministic formatting guide is the thing that makes the assistant
  European.** `user_profile/formatting.py` renders locale directives from the
  profile with no model involved, and they are injected into *every* decide
  step, not only the final one.
- **The second-opinion reviewer already enforces it during reasoning**: it
  rejects a program when "the profile shows a European operator, but the
  reasoning treats the request as a US-units question… a correct final answer
  does not excuse wrong reasoning here."
- **Context is resolved once per turn and propagated immutably** —
  `user_profile.ProfileContext`, under an explicit one-snapshot-per-turn
  invariant.

Measured behaviour with the guide alone (catalog restricted to `reply`, 4
replies per case):

| Case | Guide only |
|---|---|
| "half past eleven at night" → 23:30 | 4/4 |
| last day of the year → 2026-12-31 | 3/4 |
| `convert 1053737172 feet` → metric | 4/4 |
| price with a currency code → DKK | pass |

**So "user settings are execution semantics" is implemented, not aspirational.**
A fresh attempt that rebuilds this will spend its effort on a solved problem.

## Part 2 — what is not solved

1. **Stored language declarations do not yet drive a per-turn resolver.**
   The profile can now describe a multilingual operator, but `avoid` does not
   redirect an implicit choice, `level` does not alter register, and no narrow
   classifier determines response-language intent before reasoning.
2. **Dialect fidelity is bounded by the reply model, not only by the design.**
3. **Task-scoped locale is unrepresented.** A "2,000 sq ft office in Texas"
   should stay in square feet and USD; nothing in the current model can say
   that the *task* has a locale different from the operator's.

## Part 3 — the measurements, so nobody re-derives them

All on `gemma4:e4b` (ollama), the operator's first group member. Local latency
varies ±30% run to run; treat orderings as solid, absolutes as indicative.

**Dialect delivery, target en-GB, profile and directive agreeing:**

| Model | Result |
|---|---|
| `gemma4:e4b` | 0/3 — "the shopping **cart** contains **nappies**" (mixed) |
| `nemotron-3-nano:4b` | 0/3 — cannot translate Danish ("purchase boat") |
| `ornith:9b` | 2/3 — "shopping **trolley** … **nappies**" |
| `qwen3.5:9b` | 3/3 trolley |

The same 4B model targeting en-US was **4/4 fully American**. The dialect was
*decided* correctly in every failing case; it was lost while writing the reply.

**Cost of the step-0 calls:**

| Component | Latency | Language accuracy |
|---|---|---|
| Dialect resolution (en-US vs en-GB) | 0s — code, from the profile | n/a |
| Language classification call | 3.9s (2.7–5.0) | 12/12 |
| Work-criteria call | 10.5s (N=2) | n/a |
| Both (step 0 as built) | ~14.4s | |
| One merged call | 6.9s | 12/12 |
| One lean call (language + assumptions) | 8.6s | 9/9 |

**Does the criteria step improve locale behaviour?** No measured case. Time
4/4 → 2/4 (two replies led with "11:30 PM"), date 3/4 → 3/4, conversion 4/4 →
4/4. On a pure locale question it returned `processing: []`, `formatting: []`,
`assumptions: []` — nothing about 24-hour clocks — while occupying rank 3 in
`source_priority`, above the formatting guide at rank 4. On its motivating
case (`convert 1053737172 feet`) it produced exactly the right criteria
(`target unit: meters (settings: metric)`) and changed no outcome, because the
guide had already settled it.

## Part 4 — the traps, which cost the most time

These are failure patterns, not bugs. Each was observed live.

1. **Example words in a prompt get parroted into unrelated replies.** Telling
   a model "British English — colour not color, anticlockwise not
   counterclockwise" produces replies containing those words on unrelated
   topics. Name the dialect; never exemplify it. Keep marker words in tests
   and evals only, and guard it with a test over every prompt surface.
2. **Field descriptions get mirrored as output format.** A prompt saying
   *"language_tag (the BCP-47 tag) and reason (one short sentence)"* produced
   the prose `english (The user explicitly requests…)` **3 times in 4**. With
   the fields as bullets: 4/4 valid JSON, then 8/8 in production. A small model
   copies the *shape* of its prompt.
3. **Code that parses model prose never converges.** The first attempt
   regex-tokenized a free-text language field and patched it afterwards. Make
   the model emit a typed value and let code compose the prose.
4. **Duplicate sources of truth rot.** Two dialect tables kept in lockstep by
   a test is the shape that sank the first attempt. One table, one owner.
5. **A structured-output object can violate its own schema.** llama-index's
   streaming partial parser constructs the response *without* validation, so a
   required `str` arrived as `None` and the error surfaced far away as
   "unusable language tag None". Always re-validate before returning, and
   recover the payload between the first `{` and last `}` when a provider
   wraps JSON in fence remnants or prose.
6. **Do not drop a model's free-text field to save tokens.** Removing `reason`
   made the model reason *inside* the constrained field:
   `en-US/da-DK:en-US-fallback-required-by-user-request-…`. **0 of 6** were
   usable tags. A cheap free-text field beside a constrained one is a pressure
   valve.
7. **An empty high-priority block may displace the block holding the answer.**
   Suspected, not proven (N=4): the empty criteria section outranks the
   formatting guide, and the time case dropped 4/4 → 2/4.
8. **Language experiments confound easily.** The formatting guide and the
   injected directive both render from the profile; vary them *together* or
   the model simply follows the guide and the experiment measures nothing.
9. **Never restate the system prompt in the user prompt.** The job belongs in
   the system prompt, the shape in the schema; a "classify the language" line
   in the user prompt is billed every turn to say nothing.

## Part 5 — the model to build

**Status:** the schema, validator, editor, APIs, templates and prompt-boundary
reader described in this section are implemented. The deterministic resolver
described below is not. Until that resolver lands, rows are durable operator
declarations rather than complete reply-routing behavior.

The implemented `languages` subtree replaces the two flat fields and mirrors
the existing calibration subtree (`data["calibration"]["topics"]`) in shape,
validation and editor pattern.

```
data["languages"]["rows"] = [
  {"tag": "en-US", "level": "intermediate", "stance": "prefer",
   "note": "Primary response language."},
  {"tag": "da",    "level": "native",   "stance": "neutral"},
  {"tag": "sv",    "level": "fluent",   "stance": "avoid"},
  {"tag": "de",    "level": "beginner", "stance": "avoid"},
  {"tag": "pt",    "level": "beginner", "stance": "avoid"}
]
```

**Two orthogonal axes**, reusing calibration's `level` × `stance` vocabulary:

- **`level`** — `native` | `fluent` | `intermediate` | `beginner`: how well the
  operator knows it. Drives register, not routing.
- **`stance`** — `prefer` | `neutral` | `avoid`: whether they want *replies* in
  it. At most one `prefer` row (the primary response language); `avoid` is a
  default, never a prohibition — an explicit request always wins.

The Swedish row is why the axes must be independent: `fluent` + `avoid` — "I
read it effortlessly, never answer me in it" — is ordinary for a Scandinavian
and unsayable with one field.

**Extra languages cost zero prompt tokens.** The model classifies only the
message's language; code looks the tag up and applies stance and level
afterwards. A Portuguese message classifies as `pt`, code sees `avoid`, the
reply goes out in en-US — the model is never told Portuguese exists. One line
reaches the prompt: the preferred language, for messages too short to
classify. **The list is storage, not prompt**, so it should be complete.

**Resolution, entirely in code:** canonicalize the tag through the existing
prompt boundary; a variant-bearing tag wins as-is (explicit request); a bare
tag resolves against the rows (`prefer`, then `level`, then declaration
order); the resolved row's stance may redirect the language, and the
redirection is disclosed; the directive is composed from one variant table.

**Non-American specifics that a naive design misses:**

- **Pluricentric languages beyond English** — pt-PT/pt-BR, es-ES/es-419,
  zh-Hans/zh-Hant, nb/nn, sr-Cyrl/sr-Latn. All pass the existing BCP-47
  validator unchanged. Treating "Portuguese" as one language is the same
  mistake as treating "English" as one.
- **Internationally neutral English is a real preference**, not an
  under-specified one: a bare `en` row should render "plain international
  English; avoid region-specific idiom", which is what many non-native
  speakers actually want.
- **Language must never imply locale.** Choosing en-US must not import feet,
  `$`, `mm/dd/yyyy` or AM/PM. Rainbox keeps these in separate fields already;
  make it an executable test, because that coupling is exactly what makes
  software decide you are American.
- **Register**: `intermediate`/`beginner` on the reply language should add a
  plain-vocabulary clause. "I'm not fluent in English" must do something, or
  storing it is decoration.

**Deferred:** formality (du/De, du/Sie, tu/vous) — a real non-English axis, but
rendering it needs a table of which languages make the distinction, which is
the hardcoded linguistic knowledge that already went wrong once; the `note`
field carries it meanwhile. Also deferred: per-person languages (needs a
contacts model) and code-side language detection (the motivating case,
`translate to english: <Danish text>`, is mostly Danish tokens asking for
English — statistical detection gets it exactly backwards; intent needs a
model, identity does not).

## Part 6 — precedence, and the one thing worth adding

An explicit ladder, highest first:

1. Explicit values in the current request — `Bake at 350°F` stays Fahrenheit.
2. **Explicit project or document context** — a 2,000 sq ft office in Texas
   stays imperial and USD; metric equivalents may be *added*, never
   substituted.
3. Resolved per-turn settings.
4. Persistent operator defaults.
5. System fallback.

Level 2 is the genuine gap, and it is the only job that justifies a
model-driven criteria step: the deterministic guide cannot know that *this
task* lives in another region's frame, and no amount of profile modelling can
tell it. Everything else the criteria call currently emits is the guide
restated.

Good eval cases for it: `Bake at 350°F` stays Fahrenheit; `budget is USD
50,000` stays USD; `Schedule at 3 PM New York time` keeps the zone;
`Schedule at 15:00` defaults to Europe/Copenhagen; `Estimate heating for a
120 m² home` reasons in m², Celsius and kW throughout.

## Part 7 — what to salvage from the parked branch

Independent of any architecture, and worth keeping:

- **Implemented — `agents/base.py` structured-result fix.** Re-validates before
  returning and recovers a payload wrapped in fence remnants or prose. This is
  a general reliability fix for every structured call.
- **Partial — language variant ownership.** Public `valid_language_tag()` is
  implemented. The single example-free `user_profile.LANGUAGE_VARIANTS` table
  is not.
- **Pending — the no-example-words guard test**, which pins trap 1.
- **Pending — repeated-request history omission.** A verbatim-repeated message
  should drop the assistant's earlier replies, which are otherwise a decoding
  attractor.
- **Implemented — reply args `{1_message, 2_audit}`.** The pre-work
  specification argument was a rationalization written after the fact.

What to reconsider rather than port: the two-call step 0 (no measured benefit,
~14.4s), and the work-criteria call in its current form (returns empty lists
on locale questions).

## Where to continue

1. Finish the branch-independent language primitives: one example-free
   variant table, bare-`en` international-English wording and the
   no-example-words guard.
2. Implement the narrow typed response-language-intent call and deterministic
   resolver. Inject the resulting code-composed directive before the first
   reasoning step; do not route it through the broad acceptance-criteria call.
3. Run the classification, translation-intent, short-message,
   avoided-language and dialect-delivery evals, then choose the reply-model
   order from evidence.
4. Revisit task-scoped locale only after the language path is stable, and
   scope any model-driven criteria step to precedence level 2.

## Part 8 — independent assessment: the actual problems and solutions

This section is an independent reading of the problem after reviewing the
proposal and both parked implementation attempts. It does not replace the
measurements above; it turns them into an implementation contract.

### Problem 1 — "European" is an identity, not an executable locale

Europe is not one language, currency, date format or set of spelling rules.
The useful fact is not merely that the operator is European, but that this
operator has concrete and independent defaults:

- metric units and Celsius;
- a 24-hour clock and `Europe/Copenhagen`;
- ISO dates;
- DKK, with EUR relevant when the task involves it;
- decimal points without thousands separators;
- English, normally American English, as the primary working and response
  language;
- Danish when the operator writes or explicitly asks in Danish.

These choices do not have to resemble a national preset. In particular,
`en-US` describes a language variant; it does **not** mean the operator is
American and must never select feet, Fahrenheit, USD, month-first dates or
AM/PM. Conversely, a European locale does not imply `en-GB`.

**Solution:** keep language, language variant, units, temperature, clock, date,
number format, currency and timezone as orthogonal settings. Treat country or
"European" as context, never as a shortcut that silently overwrites those
settings. Add a cross-product test proving that an `en-US` reply retains the
operator's metric/Copenhagen/DKK/ISO/no-grouping defaults.

### Problem 2 — the decision is response-language intent, not text detection

"What language are most of these tokens?" is not the question Rainbox needs to
answer. The question is "what language should the assistant use for this
turn?" Those differ in ordinary requests:

- `translate to English: <Danish text>` contains mostly Danish but asks for an
  English result;
- `answer in Danish: what is ...` explicitly overrides an English profile;
- `ok`, a URL, a stack trace or a code block may contain too little natural
  language to classify;
- an earlier assistant reply may be in the wrong language and must not become
  evidence for continuing the mistake.

This is an intent-resolution problem. A statistical language detector alone
will confidently answer the wrong question.

**Solution:** use one narrow model decision that returns a typed
`language_tag` plus a short `reason`. Do not ask that call to plan units,
formatting or general acceptance criteria. Re-validate its structured result,
canonicalize the BCP-47 tag in code, and compose the directive in code. The
free-text `reason` is retained as a pressure valve and an audit explanation;
it never becomes the value code must parse.

The response-language precedence should be deterministic:

1. An explicit response or translation language in the current request.
2. An explicit language established by the current document or project.
3. The language intent of the current operator message.
4. For language-poor messages, recent **operator** language and then the
   profile's preferred response language.
5. The system fallback.

Assistant-authored history is never a language authority.

### Problem 3 — language knowledge and reply preference are different facts

The two flat fields cannot express "native Danish, fluent Swedish, usually
work in English, but do not answer me in Swedish." Declaration order cannot
reliably mean both competence and preference.

**Solution:** use the proposed `languages.rows` model with independent `level`
and `stance` axes. After the model identifies the response-language intent,
code applies the profile:

- `prefer` supplies the default for language-poor messages;
- `neutral` permits normal mirroring;
- `avoid` redirects an implicit choice to the preferred language;
- an explicit request overrides `avoid`;
- `level` changes register, never routing;
- a bare tag resolves to a declared variant deterministically;
- a variant-bearing explicit tag wins as written.

If code redirects an `avoid` language, the final directive should disclose the
redirect so the operator can see why the response does not mirror the input.
A bare `en` row must remain a first-class choice for plain international
English rather than being silently upgraded to `en-US` or `en-GB`.

### Problem 4 — operator defaults and task-local reality can conflict

The operator's defaults are correct for an underspecified task, but not for a
task whose subject already establishes another frame. Replacing a Texas
building's square feet and USD with metric and DKK destroys source meaning.
Likewise, converting `350°F` before reasoning about an American recipe can
change the task rather than help with it.

**Solution:** represent task-scoped locale separately from the persistent
profile and use the precedence ladder in Part 6. Preserve explicit source
notation and add conversions when useful; do not substitute the operator's
defaults for the task's facts. If a model-driven criteria step is retained,
this is its one justified job: identify explicit project/document locale that
the deterministic profile guide cannot know.

### Problem 5 — choosing a dialect and delivering it are separate failures

The experiments show that a classifier can select the correct tag while the
reply model still mixes dialects. More instructions cannot create linguistic
competence that the reply model does not have. Example vocabulary in prompts
also contaminates unrelated output.

**Solution:** keep one example-free, code-owned variant table; name dialects
without listing marker words; keep marker words only in tests. Select the reply
model using a dialect-and-translation canary. Language classification accuracy
and reply-language fidelity must be reported as separate metrics.

### Problem 6 — the acceptance-criteria mechanism became broader than the gap

The two-call v2 pipeline paid for language classification and then paid again
for a work-criteria call that repeated deterministic profile settings, often
returned empty lists, and could outrank the guide that already held the right
answer. This added latency and a second authority surface without a measured
benefit.

**Solution:** keep the narrow language-intent call and remove the general
work-criteria call from this feature. Do not inject an empty high-priority
criteria block. Locale defaults continue to come from the deterministic guide
on every reasoning step. Revisit a model-driven criteria call only for
task-scoped locale, with evals showing that it improves that exact case.

### Recommended implementation sequence

- [x] Add `languages.rows`, validation, editor support, templates and
  prompt-boundary reads; remove the two flat language fields in the same
  clean cutover.
- [ ] **Partial:** port the branch-independent reliability fixes listed in
  Part 7. Structured-result re-validation, `valid_language_tag()` and reply
  audit arguments are present; the shared variant table, no-example guard and
  repeated-request omission remain.
- [x] Implement one typed response-language-intent call as the assistant's
  first model-facing activity. Persist the prompt, model, reason, per-language
  Likert scores and audit.
- [x] Render score-free Markdown for all later assistant calls. Sort language
  rows by descending score and preserve classifier order for ties.
- [ ] Build a classifier eval corpus and measure explicit-language,
  multilingual-content, translation-intent, dialect, short-message,
  repeated-request and avoided-language cases. Manual exploratory results are
  promising; this item is the repeatable release gate.
- [ ] Evaluate whether ranked Markdown is sufficient downstream; if not, add
  a deterministic score threshold and smaller single/multilingual directive.
- [ ] Add the language-versus-locale cross-product tests while keeping the
  existing deterministic locale guide unchanged.
- [ ] Run classification, translation-intent, dialect, short-message,
  repeated-request and avoided-language evals independently.
- [ ] Choose the reply-model order from the dialect canary.
- [ ] Address task-scoped locale as a separate follow-up only after the
  language path is stable.

The minimal successful outcome is not "the assistant knows the operator is
European." It is that an English request receives the requested English
variant while reasoning in metric/Celsius/Copenhagen/DKK/ISO conventions, a
Danish request receives Danish, explicit task notation remains authoritative,
and none of those decisions silently changes another axis.
