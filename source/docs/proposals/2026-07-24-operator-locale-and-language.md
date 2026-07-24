# Operator locale and language — execution semantics, not output formatting

**Status:** Groundwork for a fresh attempt. The `acceptance-criteria-v2`
branch is **parked**; this document is what it learned, so the next attempt
starts from evidence instead of from scratch.
**Date:** 2026-07-24
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

1. **The profile cannot describe a multilingual operator.** Two ordered
   free-text fields (`language`, `language_2`) cannot hold four or five
   languages, cannot record competence, and cannot separate *what you speak*
   from *what you want back*. The field labelled "primary" held `da` while the
   operator's primary response language is en-US.
2. **Dialect fidelity is bounded by the reply model, not by the design.**
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

A `languages` subtree replacing the two flat fields, mirroring the existing
calibration subtree (`data["calibration"]["topics"]`) in shape, validation and
editor pattern.

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

- **`agents/base.py` — the structured-result fix.** Re-validates before
  returning and recovers a payload wrapped in fence remnants or prose. This is
  a general reliability fix for every structured call.
- **`user_profile.LANGUAGE_VARIANTS` + `valid_language_tag()`** — one
  example-free table of dialect names, and a public BCP-47 boundary.
- **The no-example-words guard test**, which pins trap 1.
- **The repeated-request history omission** — a verbatim-repeated message drops
  the assistant's earlier replies, which are otherwise a decoding attractor.
- **Reply args `{1_message, 2_audit}`** — the pre-work specification argument
  was a rationalization written after the fact.

What to reconsider rather than port: the two-call step 0 (no measured benefit,
~14.4s), and the work-criteria call in its current form (returns empty lists
on locale questions).

## Where to start

1. Build the `languages` subtree — it is the only unsolved piece the operator
   actually asked for, and it needs no model call.
2. Re-run the dialect canary against a 9B and decide the group order. That is
   the sole measured lever on whether a chosen dialect survives to the page.
3. Only then decide whether a criteria step exists at all, and if it does,
   scope it to precedence level 2 — task-scoped locale — and nothing else.
