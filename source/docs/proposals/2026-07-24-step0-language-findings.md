# Step 0 — what the language decision actually costs, and what it gets wrong

**Status:** Findings from live measurement on 2026-07-24, written for
assessment. No decision taken; the open questions are at the end.
**Branch:** `acceptance-criteria-v2`
**Model under test:** `gemma4:e4b` (ollama), the assistant's first group
member. Profile `Simon` (`language: da`, `language_2: en-US`).

## Why this document exists

Two live runs failed with `ValueError: unusable language tag None`, and the
question behind them — "how do I make it clear I want American English?" —
turned out to touch three separable things: two real bugs (fixed), the cost
of the step-0 calls (measured below), and the profile schema's inability to
say what the operator means (still open).

## Part 1 — the two bugs, and why the error was misleading

Both are fixed and pushed; recorded here because the symptom pointed away
from the cause.

**1. The prompt was a template the model copied.** The language call's
system prompt described its fields as *"language_tag (the BCP-47 tag) and
reason (one short sentence naming what decided it)"*. The model mirrored
that surface form and answered in prose:

```
english (The user explicitly requests a translation into English.)
```

Replaying the stored prompts against the bound model reproduced it **3 times
in 4**. With the fields stated as bullets instead, **4 of 4** returned valid
JSON, and **8 of 8** after the fix shipped. This is the same hazard as
contrastive example words: a small model copies the shape of its prompt.

**2. The structured seam hid it.** llama-index's streaming partial parser
builds the response object *without* pydantic validation, so a required
`str` field can arrive as `None` — something a validated model can never
produce. `_settle_structured_result` returned that object, and the failure
surfaced far from its cause, inside the criteria code, as "unusable language
tag None". It now validates the fallback and raises an honest parse error, so
the model-group loop falls through to the next candidate. This guard covers
every structured call, not just this one.

## Part 2 — measured cost of step 0

Method: the real prompt builders and the real model seam, 3 repetitions per
case unless noted. Latency on a local model varies a lot run to run (a lean
schema measured *slower* than a fatter one); treat the ordering as solid and
the absolute numbers as ±30%.

| Component | Latency | Language accuracy |
|---|---|---|
| Dialect selection (en-US vs en-GB) | **0s** — pure code from the profile | n/a |
| Language call (`ReplyLanguage`) | 3.9s avg (2.7–5.0) | 12/12 |
| Work-criteria call (`WorkCriteria`) | 10.5s avg (N=2) | n/a |
| **Step 0 today (both calls)** | **~14.4s** | |
| One merged call (language + all criteria) | 6.9s avg | 12/12 |
| One lean call (language + reason + assumptions) | 8.6s avg | 9/9 |

Cases used: `translate to english: indkøbsvognen indeholder bleer og
viskelæder` (expect en), `hvor langt er 100 km i miles?` (da), `How many
meters are there in a kilometer?` (en), `svar på dansk: how far is 100 km?`
(da), `convert 1053737172 feet` (en).

### Finding A — the dialect is already free

Choosing en-US over en-GB costs nothing and involves no model. The classifier
emits a bare `en`; code validates it and upgrades it to whichever variant the
profile declares. Probes confirmed the switch tracks the profile: `en-GB`
while the profile read `(da, en-GB)`, `en-US` after it changed to
`(da, en-US)`. Whatever else is expensive, this part is not.

### Finding B — the language call is the cheap half

The work-criteria call costs **2.7× the language call**. If step 0 feels like
overkill, the language classification is the wrong target: it is 3.9s of a
14.4s step.

### Finding C — splitting buys no measured accuracy

Language was correct in 12/12 split, 12/12 merged, 9/9 lean-merged. The split
was requested to fix dialect selection, but what actually fixed that was the
typed BCP-47 tag plus code-owned variant resolution — both of which survive a
merge unchanged. On this evidence the second call costs ~7s per turn and buys
nothing measurable. Caveat: 4 cases, 3 reps; not a broad sample.

### Finding D — the `reason` field is load-bearing, not decoration

Dropping it to save output tokens backfired completely. With no field to think
in, the model reasoned *inside the tag*:

```
en-US/da-DK:en-US-fallback-required-by-user-request-but-current-message-is-in-
da-DK-and-asks-for-en-US-translation-so-use-en-US-as-the-target-language-…
da-DK, da-AT, da-SE, da-NO, da-FO, da-IS, da-RS, da-HR, da-SL, …
en-US-Latn:English, script=Latin
```

**0 of 6** were usable tags — every one would be rejected at the prompt
boundary, so the feature would fail open and produce no directive on every
turn. It was not even faster (4.7s). A cheap free-text field beside a
constrained one appears to act as a pressure valve.

### Finding E — the work call largely restates deterministic data

Its `formatting` output ("numbers: dot decimal, no thousand separators") and
much of `processing` are the formatting guide in the model's own words — data
code already holds exactly. Its genuinely non-deterministic output is
`assumptions`: disclosing an ambiguity the settings resolved. In the lean
probe those assumptions were mediocre ("Source unit provided: km (100)";
"assuming metric units (km, kg)" for a feet conversion), so the field that
justifies the call is also its weakest output.

## Part 3 — the profile cannot say what the operator means

Separate from cost. The operator's stated situation:

- **en-US** — preferred response language; not fluent in it.
- **Danish** — native and fluent, but not wanted for programming/computing.
- **German, Portuguese** — beginner; they appear because family and friends
  speak them.

The schema has two ordered text fields, `language` and `language_2`. Three
things it cannot express:

1. **More than two languages.** Real life has four or five.
2. **Skill.** "I'm not fluent in English" has nowhere to live, so nothing can
   act on it.
3. **The difference between what you speak and what you want back.** The
   field labelled "primary" holds `da` while the operator's primary *response*
   language is en-US. Two different meanings share one slot, which is why the
   ordering felt ambiguous.

### The proposed shape (not yet built)

A `languages` subtree mirroring the existing calibration subtree
(`data["calibration"]["topics"]`): validated rows with stable ids, caps, its
own editor, and one renderer.

```json
{"tag": "en-US", "level": "intermediate", "reply": "preferred",
 "note": "Primary response language."}
{"tag": "da",    "level": "native",       "reply": "acceptable"}
{"tag": "de",    "level": "beginner",     "reply": "avoid"}
{"tag": "pt",    "level": "beginner",     "reply": "avoid"}
```

Two orthogonal axes, the same split calibration already uses for
`level` × `stance`: **`level`** is how well the operator knows the language;
**`reply`** is whether they want answers in it. At most one row may be
`preferred`, enforced in code.

**Extra languages cost zero prompt tokens.** The model classifies only what
language the *message* is in; code looks the tag up in the rows and applies
the policy. A Portuguese message classifies as `pt`, code sees `avoid`, and
the reply goes out in en-US — the model is never told Portuguese exists. So
the list should be complete rather than pruned: it is storage, not prompt.
Only one line reaches the prompt (the preferred language, for messages too
short to classify).

What the rows would drive, all in code: variant resolution (`en` → `en-US`);
the tie-break for short messages; never replying in an `avoid` language
unless explicitly asked; and — where `level` is intermediate or beginner — a
plain-vocabulary clause in the directive, which is what would make "not
fluent in English" mean something.

Out of scope: per-person languages for family and friends. That needs a
contacts model, and the operator-level `avoid` rows already handle the case
that matters (pasted foreign text gets an English answer).

## Open questions

1. **Step-0 shape.** Merge back to one call (~halves step 0, no measured
   accuracy loss, reverses the requested split); keep the split but bind a
   fast model such as `nemotron-3-nano:4b` to the language call (~1s for that
   call, step 0 still ~11s because the work call dominates); or leave it.
2. **Whether the work-criteria call earns 10.5s** given Finding E — or
   whether `assumptions` alone justifies it, possibly folded into the
   language call.
3. **Whether to build the `languages` subtree**, and if so whether the flat
   `language`/`language_2` fields are migrated into it (one source of truth,
   but requires migrating the built-in country templates) or kept alongside
   (cheaper, two sources of truth — the shape that broke the first attempt).

## Recommendation

Merge step 0 into one call, keep `reason`, and build the `languages` subtree
with the flat fields migrated into it. That halves the per-turn cost, keeps
every mechanism that actually fixed dialect selection, and makes the profile
able to state what the operator means. The weakest part of the evidence is
the sample size behind Finding C; a wider eval run over the language family
cases would settle it before committing to the merge.
