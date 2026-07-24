# Modelling language preferences — written for operators who are not American

**Status:** Proposal. Not implemented.
**Date:** 2026-07-24
**Evidence:** `2026-07-24-step0-language-findings.md` (live measurements).
**Touches:** `profile_fields.py`, a new `db/profile_languages.py`,
`user_profile/formatting.py`, `agents/assistant.py` (resolution + prompt),
`/profile` editor, `evals/profile_guidance.py`.

## The premise

Language settings in most software are designed by and for monolingual
American English speakers, and it shows: one language field, defaulting to the
system locale, on the assumption that where you live tells you what you speak
and what you want to read. That assumption fails for most of the planet, and
it fails concretely here — the operator is Danish, lives in Denmark, and wants
American English replies, which the current schema can express only by
accident.

Rainbox already stores `language` and `language_2`: two ordered free-text
BCP-47 fields. This proposal replaces them with a model that can state what a
multilingual operator actually means.

## What the two-slot model cannot say

Observed while debugging the acceptance-criteria work, with the operator's own
profile:

1. **More than two languages.** The operator lists four (en-US, Danish,
   German, Portuguese), and the languages in their life are set by family and
   friends, not by their own study. Two slots capture the majority and miss
   reality.
2. **The difference between what you speak and what you want back.** The field
   labelled "Language (primary)" held `da`, while the operator's primary
   *response* language is en-US. One slot was carrying two meanings, which is
   why its ordering felt arbitrary.
3. **Competence.** "I'm not fluent in English" has nowhere to live, so nothing
   can act on it.
4. **Which variant.** A bare `en` is not a preference, it is an unanswered
   question — and an unanswered question gets answered by the model's training
   bias, which is American.

## What non-American operators need

These are the requirements the design is built from. Each is a thing the
current schema cannot express.

**Multilingual is the default case, not an edge case.** The schema must hold
an open-ended list, not a primary and a spare.

**Speaking a language is not wanting replies in it.** A native Dane may want
English replies because Danish technical vocabulary is thin and Danish
software UIs are poorly localized. Nationality does not imply reply language,
and locale must not be allowed to infer it.

**Passive competence is real and common.** A Dane reads Swedish and Norwegian
without effort but does not write them. "Don't answer me in Swedish" and
"don't bother translating this Swedish quote" are different statements, and a
single skill number cannot make both.

**Pluricentric languages need a variant, always.** English is the obvious one,
but the same problem exists for Portuguese (pt-PT vs pt-BR), Spanish (es-ES vs
es-419), Chinese script (zh-Hans vs zh-Hant), Norwegian (nb vs nn) and Serbian
(sr-Cyrl vs sr-Latn). A system that treats "Portuguese" as one language is
making the same mistake as one that treats "English" as one language — it just
makes it about someone else.

**Internationally neutral English must be a first-class choice.** Many
non-native speakers do not want American *or* British idiom; they want plain
English that a Dane, a Pole and a Brazilian can all read. Today a bare `en`
reads as "under-specified" and gets resolved to a variant. It should instead
be a declarable preference in its own right, rendering a directive to avoid
region-specific idiom and slang.

**Language is not locale.** Choosing American English as a reply language must
never drag in feet, `$`, `mm/dd/yyyy` or AM/PM. Rainbox already keeps units,
currency, date format and time format in separate fields — this proposal makes
that separation an explicit, tested invariant rather than an accident of
layout, because it is exactly the coupling that produces "the assistant thinks
I'm American".

**Foreign content is not a language request.** Non-Americans paste foreign
text constantly — a message from a Portuguese relative, a German error
message. The reply language must not follow the quoted material.

**Register matters to non-natives.** Someone who reads English fluently but
writes it hesitantly may want plainer vocabulary. Declared competence should
be able to say so.

## The model

A `languages` subtree, mirroring the existing calibration subtree
(`data["calibration"]["topics"]`) in shape, validation, storage discipline and
editor pattern — the house pattern for "a list of self-declared rows".

```
data["languages"]["rows"] = [
  {"id": …, "tag": "en-US", "level": "intermediate", "stance": "prefer",
   "note": "Primary response language. Danish computing UIs are half-arsed.",
   "updated_at": …},
  {"id": …, "tag": "da",    "level": "native",   "stance": "neutral"},
  {"id": …, "tag": "sv",    "level": "fluent",   "stance": "avoid",
   "note": "I read it fine; don't answer in it."},
  {"id": …, "tag": "de",    "level": "beginner", "stance": "avoid"},
  {"id": …, "tag": "pt",    "level": "beginner", "stance": "avoid"}
]
```

**Two orthogonal axes**, deliberately reusing calibration's `level` × `stance`
vocabulary so the operator meets one concept, not two:

- **`level`** — `native` | `fluent` | `intermediate` | `beginner`. How well the
  operator knows the language. Drives register, not routing.
- **`stance`** — `prefer` | `neutral` | `avoid`. Whether they want *replies* in
  it. `prefer` is the primary response language and at most one row may hold
  it; `neutral` means mirroring into it is fine; `avoid` means never reply in
  it unless the message explicitly asks.

The two axes are independent on purpose, and the Swedish row above is why:
`fluent` + `avoid` is a coherent, common statement that no single field can
make.

- **`tag`** — BCP-47, canonicalized through the existing prompt-boundary
  validation (`user_profile.valid_language_tag`). A bare primary tag is a
  legitimate declaration meaning "this language, no regional variant".
- **`note`** — free text for the operator's own reasoning. Stored and shown in
  the editor; **never injected into a prompt**, because model-written and
  operator-written free text must not become instructions.

Constraints: at most one `prefer` row; canonical tags unique; a row cap (20 —
languages are not topics, and 100 would be theatre); a note cap of 400 chars;
a byte cap on the subtree. Server-owned `id` and `updated_at` never reach a
prompt.

## Resolution — entirely in code

The model's only language output is a tag for the *message*. Everything below
is deterministic, so no code ever parses model prose.

1. Canonicalize the classifier's tag. Unusable → no directive (fail open).
2. **A variant-bearing tag wins as-is.** If the message asked for British
   English and the model returned `en-GB`, that is an explicit request and the
   profile does not override it.
3. **A bare primary tag resolves against the rows.** Take rows sharing that
   primary subtag: one match wins; several resolve by `prefer`, then by
   `level`, then by declaration order. No match leaves the bare tag standing.
4. **Apply the stance of the resolved row.** `avoid` substitutes the `prefer`
   row instead, and the substitution is disclosed in the criteria so the
   operator can see the reply language was redirected rather than mirrored.
   An explicit request in the message always overrides `avoid` — it is a
   default, never a prohibition.
5. **Compose the directive** from the single `LANGUAGE_VARIANTS` table:
   - variant known → name the dialect and its contrast, never example words;
   - bare tag with a row declared → the neutral-register directive ("plain
     international English; avoid region-specific idiom and slang");
   - `level` of `intermediate` or `beginner` → append the plain-vocabulary
     clause;
   - nothing known → state the language and stop.

`LANGUAGE_VARIANTS` is the single extension point, and grows to cover the
pluricentric cases named above: en-US/en-GB, pt-PT/pt-BR, es-ES/es-419,
zh-Hans/zh-Hant, nb/nn, sr-Cyrl/sr-Latn. Names only — the entries carry
dialect names and their contrast, never sample words.

## What reaches the prompt

One line: the preferred language, for messages too short to classify. That is
all.

Every other row, level, stance and note is applied in code *after*
classification. A Portuguese message classifies as `pt`, code sees `avoid`,
and the reply goes out in en-US — the model is never told Portuguese exists.
**Declaring a language therefore costs zero prompt tokens per turn**, which is
what makes an honest, complete list affordable. The list is storage, not
prompt.

## Invariants worth testing

- **Language never implies locale.** A test asserts that changing the language
  rows leaves units, currency, number format, date format and time format
  untouched in the rendered formatting guide. This is the "stop assuming I'm
  American" guarantee, in executable form.
- **No example words in any prompt surface** — the existing guard test extends
  to the new directives.
- **One variant table.** A test asserts the assistant and the formatting guide
  render dialect text from the same table.
- **Resolution is table-driven and total**: every combination of (classifier
  tag, rows) maps to a defined outcome, including no rows and unusable tags.

## Migration

`language` → a row with `stance: prefer`; `language_2` → a row with
`stance: neutral`; `level` omitted, because the old fields never asked. The
built-in country templates gain equivalent rows, and `SUMMARY_KEYS` derives
its language column from the `prefer` row. The flat fields are then removed —
keeping them would leave two places answering the same question, which is the
duplication that sank the first acceptance-criteria attempt.

## What this does not fix

Stated plainly, because the measurements are unambiguous: **this proposal
improves what the operator can express, not whether the model obeys it.** With
the profile and directive both set to en-GB, `gemma4:e4b` still answers "the
shopping cart contains nappies" — a mixed dialect — while `ornith:9b` and
`qwen3.5:9b` produce "trolley". Dialect fidelity is bounded by the reply
model, and no schema change moves that boundary. Expect this work to make the
preference sayable and correctly resolved; expect the model binding to
determine whether the reply honours it.

## Deferred, with reasons

- **Formality (T–V distinction).** `du`/`De`, `du`/`Sie`, `tu`/`vous` is a real
  per-language preference that English-designed schemas ignore entirely, and it
  belongs on this row eventually. Deferred because rendering it correctly needs
  a table of which languages make the distinction and how, and that table is
  the kind of hardcoded linguistic knowledge that has already gone wrong once
  here. The `note` field carries it in the meantime.
- **Per-person languages.** Family and friends have their own languages, but
  modelling that needs a contacts model. Operator-level `avoid` rows already
  handle the case that matters: pasted foreign text gets an answer in the
  operator's own language.
- **Code-side language detection.** Tempting for cost, but the motivating case
  (`translate to english: <Danish text>`) is mostly Danish tokens asking for
  English output — statistical detection gets it exactly wrong. Intent needs a
  model; identity does not.
- **Translation quality.** Separate concern, and model-bound.

## Open decisions

1. Do the flat fields get removed in the same change, or deprecated for a
   release first? Removing is cleaner; deprecating is safer for stored
   profiles.
2. Is `avoid` the right default for a `beginner` language, or should stance
   always be explicit? Defaulting is convenient and slightly presumptuous.
3. Should a bare-tag row and a variant row for the same language be allowed
   simultaneously (`en` neutral-register plus `en-GB` for one context)? Simpler
   to forbid; the resolution rules above already handle it if allowed.
