# Where a turn's time goes, and the three copies of the locale

**Status:** Proposal. One part of it — rendering `formatting_guide` as a bare
tag so every call of the turn spells it the same way — is already the current
state; the rest is unbuilt.
**Date:** 2026-08-31
**Related:** `2026-07-23-reply-acceptance-criteria.md` (why the criteria call
exists), `2026-08-17-gating-the-response-language-classifier.md` (the same
question asked of the classifier), `2026-07-24-operator-locale-and-language.md`
(read before touching anything about language).

## The measurement

One live run, 2026-08-31 16:37, `gemma4:e4b`, request: six words, no tool use,
one reply. The classifier resolved by detection, so no model was asked for it.

| call | prompt in | out | prefill | total |
|---|---|---|---|---|
| response_language_classifier | — | — | — | 0.7s |
| acceptance_criteria | 8688 | 211 | 12.9s | 24.1s |
| decide → reply | 13648 | 781 | 21.8s | 39.3s |
| reply_audit | 9493 | 91 | 14.6s | 16.7s |
| run_summarizer | 1231 | 30 | 1.7s | 2.3s |

A six-word request costs 33k prompt tokens across four calls and 83s of
wall-clock, and **49s of that is prefill**.

### The criteria call's 24.1s

- ~6.8s the model loading after an idle period. The four criteria calls
  earlier the same day show no such gap, so this term is a cold start, not a
  standing cost. Steady state is ~15s.
- 12.9s prefilling 8688 tokens, of which ~8.4k is conversation history and
  settings that the very next call prefills again from scratch.
- 4.4s emitting 211 tokens, most of which restate deterministic data.

## Finding 1 — the runtime discards a prefix it is handed

`/activity` reports two independent numbers per call: **reusable** (how much of
the prompt's leading text rainbox had already sent — exact, computed from the
prefix-hash chain in `llm/activity_metrics.py`) and **cached** (how much the
runtime evidently reused — inferred from prefill timing, because local backends
report no cache field).

Past 24 hours, `gemma4:e4b`, 24 calls:

| caller | calls | reusable | measured cached | prompt tokens | p50 |
|---|---|---|---|---|---|
| acceptance_criteria | 6 | 3.3% | **0.0%** | 47.3k | 16.5s |
| decide | 6 | 46.4% | 6.2% | 77.5k | 34.4s |
| reply_audit | 6 | 74.9% | 9.1% | 52.7k | 14.4s |
| run_summarizer | 6 | 38.2% | 6.9% | 7.7k | 2.3s |

In the measured run: decide had **6648 reusable tokens** and reused none of
them; reply_audit had 7165 and reused none. The two calls run back to back, on
the same model, seconds apart, with nothing between them.

This is the single largest recoverable term on the page, and it costs no prompt
words at all — the prefix the single-prompt-builder work was built to create is
already there and is being thrown away. At the cold prefill rate the two calls
measured (~630-670 tok/s), honoring it is worth roughly **10s on decide and 10s
on audit, per turn**.

Suspects, in the order worth checking:

1. `OLLAMA_NUM_PARALLEL` > 1. Several slots split the KV cache and requests
   round-robin between them, so consecutive calls land in different slots and
   each sees a cold context. This would produce exactly the observed pattern:
   systematically ~0% measured against a high reusable, even back to back.
2. `num_ctx` at 100k. A context that large may force eviction between calls.
3. `keep_alive` shorter than the gap between calls.

None of these is a rainbox change. The check is a configuration read plus two
identical back-to-back calls with the prefill times compared.

**The criteria call's own 3.3% is not a defect.** It is the first call of the
turn; there is nothing before it to share with. What it shares is *forward*, to
the two expensive calls behind it — which makes it, incidentally, a cache primer
for them, and that matters to proposal C below.

## Finding 2 — the locale facts appear three times in one prompt

Every decide prompt states the same settings three times, in three different
phrasings:

1. `<user_settings_json>` — the raw profile fields, including `units`,
   `temperature`, `timezone`, `date_format`, `time_format`,
   `first_day_of_week`, `number_format`, `currency`, `currency_2`.
2. `<formatting_guide>` — the same nine fields compiled into prose directives
   by `user_profile/formatting.py`.
3. `<acceptance_criteria_markdown>` → `processing` and `formatting` — a 4B
   model's paraphrase of the guide it was just shown.

`reply_audit` then receives all three again and checks the reply against the
third.

Three statements of one fact is three chances to disagree, in front of a small
model, on the topic where small models were already measured to fail (imperial
units for a metric operator). The guide is the canonical rendering: it carries
rules the raw JSON cannot express — that these are defaults the request may
override, that a source value must be preserved with the conversion added, that
an exchange rate is never invented.

## Finding 3 — two of the criteria call's three fields are deterministic

`processing` and `formatting` are a pure function of the profile plus the
already-resolved language. The model contributes paraphrase, latency, and the
risk of dropping a line — which is why the turn instructions have to nag:

> Read the formatting guide line by line and restate every line that bears on
> this reply.

An instruction that exists only to make a model faithfully copy data the code
is holding is a sign the copy should not be a model's job. Only `assumptions`
is genuinely per-request: it names what the request left open, which nothing
deterministic can produce.

## Finding 4 — the reply's language is stated three times too

The guide's Language line, `reply_language_markdown`, and the criteria's
`formatting` all name it. Roughly eleven lines of the criteria call's turn
instructions exist to tell the model *not* to re-derive a language while
simultaneously showing it the transcript and the settings block that would
mislead it. That paragraph is the cost of asking a model to restate a decision
another component already made.

## The current state of the shared prefix

The criteria prompt and the decide prompt are byte-identical from
`<current_user_request>` through the end of `<formatting_guide>`; they diverge
after it, where criteria goes to its `turn_instructions` and decide goes on to
`assistant_persona`. The guide is therefore the **last block those two calls
can share**, and only while both spell its tag the same way — which is why
every call now renders a bare `<formatting_guide>`. What a call must *do* with
the guide is `turn_instructions`' job to say: the criteria call reads it as
material to restate, the decide call as the defaults its reply follows.

Note the size of that particular win: the shared run was already ~211 lines
long, so matching the tag adds the guide's own ~400 tokens, not the 6648 that
Finding 1 is about. It is a correctness fix to the prefix chain, not a
performance fix.

## Proposals

### A. Render the criteria's deterministic half in code

Code fills `processing` and `formatting` from the criteria snapshot profile and
the resolved language; the model is asked only for `assumptions`.

- Output drops from ~200 tokens to ~60: ~3s off every criteria call.
- The restate-every-line paragraph and the whole language paragraph leave the
  turn instructions — roughly 20 lines off the prompt, and Finding 4 dissolves
  without reopening the parked locale work.
- The injected criteria block becomes **stable text across turns** for a given
  profile, which compounds with a working cache instead of fighting it: today
  it is fresh model prose on every turn, so it can never be cached.
- A dropped formatting line stops being possible rather than being nagged
  against.

The open question is empirical: does the model's version ever carry something
the deterministic one would not? Answerable offline by replaying recorded runs
through `evals/profile_guidance.py` and diffing the two renderings — no live
turns, no guessing.

### B. Drop the locale fields from `user_settings_json` while the guide is on

Keep identity and biography there; let the guide be the only statement of the
nine locale fields. Small token win, real contradiction-surface win. If the
guide is switched off, the raw fields return — the JSON stays the fallback.

### C. Shrink the criteria call's input

The call does not need 7k tokens of transcript to state one ambiguity. The
request plus the last exchange covers the follow-up case the instructions
actually rely on (a request carrying only a pronoun takes its subject from the
exchange before it). That takes the call from ~15s to ~4s.

**This one fights the cache.** History sits early in the prompt, so a trimmed
history means decide and audit share almost nothing with criteria — the primer
effect from Finding 1 is lost. Right now that costs nothing, because the
runtime reuses nothing. Fix Finding 1 first and C becomes a genuine trade to
measure; leave Finding 1 unfixed and C is free money.

### D. Ask whether the call should exist on trivial turns

If A lands, the criteria call's entire model-derived output is one sentence
about ambiguity — and the decide call already holds everything needed to
produce it. Worth asking, but only after A: the value of the criteria block is
partly that it is fixed *before* the work starts, and folding it into decide
gives that up. Not proposed, only flagged.

## Order

1. **Finding 1.** Configuration, no code, largest term.
2. **A**, gated behind the replay diff.
3. **B**, which is nearly free once A has removed the third copy.
4. **C**, measured against a cache that by then works.

## How to verify

- `/activity`: reusable versus measured cached, per caller. Reusable high and
  cached low means the runtime evicted; reusable low means prompt assembly
  broke the prefix. The two are deliberately kept apart and both are needed.
- `agents/test_assistant_prompt_tiers.py`: the shared-prefix tests assert the
  byte-level property directly, so a builder edit that quietly reorders a
  section fails there rather than silently costing prefill.
- `evals/profile_guidance.py` and `evals/profile_gate.py`: prompt-construction
  variants against a given profile, for measuring a formatting change without
  live turns.
