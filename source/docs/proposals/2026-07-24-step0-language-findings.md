# Step 0 — what the language machinery costs, and where the dialect is actually lost

**Status:** Findings from live measurement on 2026-07-24, written for
assessment. No decision taken; the open questions are at the end.
**Branch:** `acceptance-criteria-v2`
**Models under test:** the assistant's group, in fallback order —
`gemma4:e4b`, `nemotron-3-nano:4b`, `ornith:9b`, `qwen3.5:9b` (all ollama).
**Canary case:** `translate to english: indkøbsvognen indeholder bleer og
viskelæder` — three dialect pairs in one sentence (cart/trolley,
diapers/nappies, eraser/rubber).

## Why this document exists

Two live runs failed with `ValueError: unusable language tag None`, and the
question behind them — "how do I make it clear I want American English?" —
turned out to touch four separable things: two bugs (fixed), the cost of the
step-0 calls, **where the dialect is actually lost** (the important one), and
the profile schema's inability to say what the operator means.

## Part 1 — the two bugs, and why the error was misleading

Both fixed and pushed; recorded because the symptom pointed away from the cause.

**1. The prompt was a template the model copied.** The language call's system
prompt described its fields as *"language_tag (the BCP-47 tag) and reason (one
short sentence naming what decided it)"*. The model mirrored that surface form
and answered in prose: `english (The user explicitly requests a translation
into English.)`. Replaying the stored prompts reproduced it **3 times in 4**;
with the fields stated as bullets, **4 of 4** returned valid JSON, and **8 of
8** after the fix shipped. Same hazard as contrastive example words: a small
model copies the shape of its prompt.

**2. The structured seam hid it.** llama-index's streaming partial parser
builds the response object *without* pydantic validation, so a required `str`
field can arrive as `None`. `_settle_structured_result` returned that object,
so the failure surfaced far from its cause. It now validates the fallback and
raises an honest parse error, and the model-group loop falls through to the
next candidate. The guard covers every structured call.

## Part 2 — where the dialect is actually lost

**This corrects an earlier claim in this document that "the dialect is free".**
That was measured on the wrong half of the chain: it is free to *decide* and
unreliable to *deliver*.

Method: the real prompt builders, the real profile blocks, the catalog
restricted to `reply` so action choice could not pollute the sample. Target
dialect en-GB, with the profile and the injected directive agreeing.

| Model | en-GB output | Verdict |
|---|---|---|
| `gemma4:e4b` | "The shopping **cart** contains **nappies** and wipes" ×3 | 0/3 — mixed |
| `nemotron-3-nano:4b` | "The purchase boat contains bleers and viskelayers" | 0/3 — cannot translate Danish |
| `ornith:9b` | "The shopping **trolley** contains **nappies** and dusters" | 2/3 |
| `qwen3.5:9b` | "The shopping **trolley** contains sponges and dusting cloths" | 3/3 trolley |

The same probe against `gemma4:e4b` with the profile and directive set to
en-US gave **4/4 fully American** ("shopping cart … diapers").

### Finding A (corrected) — deciding the dialect is free; delivering it is not

Choosing en-US over en-GB genuinely costs nothing: the classifier emits a bare
`en`, and code upgrades it to whichever variant the profile declares. Probes
confirmed that switch tracks the profile exactly. **But that was never where
the failure was.** The directive reaches the model and is partly obeyed —
`gemma4:e4b` under en-GB switched diapers→nappies while leaving the American
"cart" — which is the mixed-variant reply the first attempt suffered from.

An extra LLM call spent on *deciding* the dialect would not have helped: en-GB
was decided correctly and injected correctly, and the reply still came back
half American. The failure is downstream, in the reply model's ability to hold
a dialect across a whole sentence.

### Finding B — it is a model-capability limit

4B models cannot hold a dialect; 9B models mostly can. Nothing in the prompt
architecture distinguishes the two cases — same directive, same guide, same
criteria section. The highest-leverage change available today is therefore a
model binding, not a prompt or a step-0 redesign.

### Finding C — a methodology trap worth remembering

The first end-to-end run showed the directive having *no* effect: en-US, en-GB
and no-directive all produced identical American output. That was a confounded
experiment — the formatting guide is built from the profile (`da, en-US`) and
was saying "American English" while the injected directive said en-GB, so the
model followed the guide. Production can never hit this (both render from the
same profile snapshot), but it shows how easily a language experiment
misleads: **the guide and the directive must be varied together.**

## Part 3 — cost of step 0

Latency on a local model varies a lot run to run (a lean schema measured
*slower* than a fatter one). Treat the ordering as solid, the absolutes ±30%.

| Component | Latency | Language accuracy |
|---|---|---|
| Dialect resolution (en-US vs en-GB) | 0s — code, from the profile | n/a |
| Language call (`ReplyLanguage`) | 3.9s avg (2.7–5.0) | 12/12 |
| Work-criteria call (`WorkCriteria`) | 10.5s avg (N=2) | n/a |
| **Step 0 today (both calls)** | **~14.4s** | |
| One merged call (language + all criteria) | 6.9s avg | 12/12 |
| One lean call (language + reason + assumptions) | 8.6s avg | 9/9 |

**The language call is the cheap half.** The work-criteria call costs 2.7× it.
If step 0 feels like overkill, the language classification is the wrong target.

**Splitting buys no measured accuracy.** Language was correct 12/12 split,
12/12 merged, 9/9 lean-merged. What fixed dialect *selection* was the typed
BCP-47 tag plus code-owned resolution — both survive a merge unchanged.
Caveat: 4 cases × 3 reps, not a broad sample.

**The `reason` field is load-bearing.** Dropping it to save output tokens
backfired: with no field to think in, the model reasoned *inside the tag* —
`en-US/da-DK:en-US-fallback-required-by-user-request-…`,
`da-DK, da-AT, da-SE, da-NO, …`, `en-US-Latn:English, script=Latin`. **0 of 6**
were usable tags; every one would be rejected at the prompt boundary and the
feature would fail open on every turn. It was not even faster (4.7s). A cheap
free-text field beside a constrained one acts as a pressure valve.

**The work call largely restates deterministic data.** Its `formatting` output
("numbers: dot decimal, no thousand separators") and much of `processing` are
the formatting guide in the model's own words. Its genuinely
non-deterministic output is `assumptions` — and in the lean probe those were
mediocre ("Source unit provided: km (100)"; "assuming metric units (km, kg)"
for a feet conversion), so the field that justifies the call is also its
weakest output.

## Part 4 — does step 0 actually make the assistant European?

This is the original goal: the assistant assumed feet, USD, AM/PM and
mm/dd/yyyy, and the criteria step exists so the loop knows "I have to convert
to metric" before it starts working. Measured A/B on the operator's real
profile and live settings (`assistant.formatting_guide` **on**, calibration
off), catalog restricted to `reply`, 4 replies per condition.

| Case | Baseline (guide only) | With step-0 criteria |
|---|---|---|
| Half past eleven at night → 23:30 | **4/4** | **2/4** (two led with "11:30 PM, or 23:30") |
| Last day of the year → 2026-12-31 | 3/4 | 3/4 |
| Ambiguous `convert 1053737172 feet` → metric | **4/4** | **4/4** |
| Price with a currency code → DKK | pass | pass |

### Finding D — the formatting guide is what makes the assistant European

Every locale expectation the operator listed is already carried
deterministically by `user_profile/formatting.py`, rendered from the profile,
and it is enabled. The guide alone answers in 24-hour time, ISO dates, DKK and
metric. There is no measured case where the criteria step rescued a locale
answer the guide had missed.

### Finding E — on locale-only questions the criteria step returns nothing

For "write half past eleven at night as a clock time" the 10.5s work call
produced:

```json
{"response_language": "The reply must be in en-US: …",
 "processing": [], "formatting": [], "assumptions": []}
```

Empty lists — no "times: 24-hour clock", which is exactly the criterion the
request needed. The section still occupies rank 3 in `source_priority`, above
the formatting guide at rank 4. In the same sample the time case dropped from
4/4 to 2/4. With N=4 that is not conclusive on its own, but the mechanism is
plausible: an empty high-priority block displacing the block that holds the
actual answer.

### Finding F — on its motivating case it works, and changes nothing

For the ambiguous `convert 1053737172 feet` the criteria were exactly right —
`processing: ["target unit: meters (settings: metric)"]`, `assumptions:
["convert target not stated; assuming meters"]`. Both conditions answered in
metres 4/4, because the guide's "Units: metric" had already settled it. The
feature produced correct output and no measurable improvement.

(Arithmetic was wrong in every reply of this case, in both conditions — a
side effect of restricting the catalog to `reply`, which is precisely what
`python_run` exists to prevent. Not a locale finding.)

## Part 5 — the profile cannot say what the operator means

Separate from cost and compliance. The operator's stated situation: **en-US**
preferred for responses, not fluent in it; **Danish** native but unwanted for
programming; **German, Portuguese** beginner, present because family and
friends speak them.

The schema has two ordered text fields. Three things it cannot express:

1. **More than two languages.** Real life has four or five.
2. **Skill.** "I'm not fluent in English" has nowhere to live.
3. **What you speak vs what you want back.** The field labelled "primary"
   holds `da` while the primary *response* language is en-US.

### The proposed shape (not built)

A `languages` subtree mirroring the calibration subtree
(`data["calibration"]["topics"]`): validated rows, stable ids, caps, its own
editor and renderer.

```json
{"tag": "en-US", "level": "intermediate", "reply": "preferred",
 "note": "Primary response language."}
{"tag": "da",    "level": "native",       "reply": "acceptable"}
{"tag": "de",    "level": "beginner",     "reply": "avoid"}
{"tag": "pt",    "level": "beginner",     "reply": "avoid"}
```

Two orthogonal axes, the split calibration already uses for `level` ×
`stance`: **`level`** is how well the operator knows it, **`reply`** is
whether they want answers in it. At most one row may be `preferred`.

**Extra languages cost zero prompt tokens.** The model classifies only the
message's language; code looks the tag up and applies the policy. Portuguese
classifies as `pt`, code sees `avoid`, the reply goes out in en-US — the model
is never told Portuguese exists. The list should be complete, not pruned: it
is storage, not prompt. One line reaches the prompt (the preferred language,
for messages too short to classify).

Out of scope: per-person languages for family and friends — that needs a
contacts model, and operator-level `avoid` rows already cover pasted foreign
text.

## What the problem turned out to be

The goal — an assistant that knows the operator is European and multilingual —
splits into three problems with different owners, and only one of them is the
one the criteria step was built to solve.

1. **"Understands I'm European" is already solved, deterministically.** The
   formatting guide holds metric, DKK, 24-hour, ISO dates and Copenhagen time,
   renders from the profile with no model involved, and is switched on. It
   scores 4/4, 3/4 and 4/4 on the locale probes above. Nothing needs building
   here; if a European expectation is missed, the guide is where to look.
2. **"I don't speak only one language" is not solved, and it is the part that
   genuinely needs a model** — which language a given message should be
   answered in, plus a profile able to hold four or five languages with skill
   levels. The language call does the first half correctly today
   (12/12, 8/8); the second half is unbuilt (Part 5).
3. **The dialect reaching the page is a model-capability problem** (Part 2),
   not a decision or a prompt problem.

The work-criteria half of step 0 fits none of these: it restates the guide,
returns empty lists on locale questions, costs 10.5s per turn, and outranks
the guide in the prompt while doing so.

## Open questions

1. **Should the work-criteria call exist?** It has no measured benefit. The
   options: drop it (step 0 becomes the language call alone, ~4s, and the
   guide keeps doing the locale work); keep only `assumptions` (the one output
   the guide cannot produce); or merge it into the language call (~7s).
2. **Reply model binding.** Should the group lead with a 9B (`ornith` or
   `qwen3.5`) instead of `gemma4:e4b`? It is the only measured lever on
   dialect fidelity. Cost: 10-18s per decide step against ~8s.
3. **Does the audit catch a mixed dialect?** Unmeasured. If `2_audit` catches
   "cart" in a British reply, one bounce makes the small model viable.
4. **Build the `languages` subtree?** And do the flat `language`/`language_2`
   fields migrate into it (one source of truth, needs the country templates
   migrated) or sit alongside it (cheaper, two sources of truth — the shape
   that broke the first attempt).
5. **Does an empty criteria block hurt?** N=4 suggests the time case dropped
   from 4/4 to 2/4. Worth a proper eval run before acting on it.

## Recommendation

Stop investing in the work-criteria call and start with the two things that
have evidence behind them: build the `languages` subtree, so the profile can
finally state that the operator wants American English replies, is Danish, and
has beginner German and Portuguese; and re-run the dialect canary against a 9B
to decide the group order. Keep the language classification — it is cheap,
correct, and the only part of step 0 doing work the deterministic guide
cannot. Before removing the work call, run the locale eval family under the
`criteria` variant to confirm Finding E on a larger sample than four
repetitions.
