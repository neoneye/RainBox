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

## Finding 1 — a shared head is worth nothing; only a whole prefix counts

`/activity` reports two numbers per call: **reusable** (how much of the
prompt's leading text rainbox had already sent — exact, from the prefix-hash
chain in `llm/activity_metrics.py`) and **cached** (how much the runtime
evidently reused — inferred from prefill timing, because local backends report
no cache field).

Past 24 hours, `gemma4:e4b`, 24 calls:

| caller | calls | reusable | measured cached | prompt tokens | p50 |
|---|---|---|---|---|---|
| acceptance_criteria | 6 | 3.3% | **0.0%** | 47.3k | 16.5s |
| decide | 6 | 46.4% | 6.2% | 77.5k | 34.4s |
| reply_audit | 6 | 74.9% | 9.1% | 52.7k | 14.4s |
| run_summarizer | 6 | 38.2% | 6.9% | 7.7k | 2.3s |

decide had 6648 reusable tokens and reused none. The two calls run back to
back, seconds apart, on the same model.

### It is not configuration

`llama-server` is launched with `-c 102400 -np 1 --context-shift --keep 4`:
one slot, so request parallelism is not involved. `OLLAMA_NUM_PARALLEL` is
unset on this machine, `OLLAMA_KEEP_ALIVE` defaults to 5m against calls
seconds apart, and probing the runtime directly shows its prefix cache working
perfectly — an identical 19.4k-token prompt resent goes from 31,676ms to
103ms. Changing the structured-output schema between calls does not cost the
prefix, and loading a different model in between does not evict it.

### It is the prompt's shape, and the server says so

Replaying the run's own criteria and decide prompts back to back, outside
rainbox, reproduces production exactly: criteria 12.5s, decide 20.9s at 652
tok/s — fully cold — after which a byte-identical repeat of decide takes 24ms.
The 25,911 characters the two prompts share bought nothing. `llama-server`
explains itself in the log:

```
checking sim = 0.540 (7371/13648) > 0.100
selected slot by LCP similarity, f_sim_best = 0.540
checking checkpoint with [7148, 8683] against 6859...
forcing full prompt re-processing due to lack of cache data
  (likely due to SWA or hybrid/recurrent memory)
erased invalidated context checkpoint (... n_swa = 512 ...)
```

It **found** the 7371-token shared prefix and threw it away. `gemma4` is a
sliding-window-attention model, and llama.cpp cannot resume from a divergence
point deep inside a cached sequence: the window states for those positions are
gone, and the context checkpoints it keeps did not cover the reuse point.

### The measured rule

One model, one slot, `num_ctx=100k`, each shape measured against a prefix
nothing had cached:

| shape | prompt | new tokens | prefill | reuse |
|---|---|---|---|---|
| identical repeat | 13650 | 0 | 24ms | total |
| extension — same prompt plus a tail | 12295 | 3140 | 5.3s (cold would be 17.5s) | head reused |
| fork — shared head, diverging at 45% | 12298 | 3143 | 18.0s at 684 tok/s | **none** |
| the real criteria → decide pair | 13650 | ~6.3k | 20.9s at 652 tok/s | **none** |

**An earlier call's prompt must be a complete prefix of the later one.**
Sharing a head and then diverging is worth exactly zero on this stack — the
same price as sharing nothing. In the log, 135 requests were forced into full
re-processing, spread evenly across every prompt size the assistant produces.

### What that costs, and what it does not

The ~20s per turn is real, but it is **not** recoverable by making the prompts
share more. Every call of the turn ends with its own `turn_instructions` and
its own request anchor, so every pair of calls is a fork by construction, and
no amount of block-order nesting changes that. The nesting in
`_ALL_STATIC_BLOCKS` is still worth keeping — it is necessary, it costs
nothing, and it pays in full on a non-SWA model — but on this model it is
currently inert.

Consecutive decide steps within a turn are the one pair that is nearly an
extension: the scratchpad grows append-only and only the tail moves. That is
where the 6.2% decide hit rate comes from.

Two levers would restore mid-prompt reuse, and neither is a prompt change:

- **`--swa-full`** — llama.cpp keeps the whole SWA cache, trading memory for
  arbitrary-position reuse. Ollama 0.32.15 never passes it and exposes no
  environment variable for it, so it is not reachable from here today.
- **a non-SWA model** on the assistant slots, which turns every one of these
  findings from inert into cashable.

### `/activity` is measuring the wrong thing for this model

`reusable_prefix_tokens` counts shared *leading text*, which is the right
metric when the runtime can resume mid-sequence and a misleading one when it
cannot. On an SWA model the number that predicts a cache hit is whether the
earlier prompt is a **strict prefix** of this one. The page currently reads
"the runtime is losing prefixes it could have kept" and recommends fewer
models in rotation; on this stack that advice is wrong. Worth splitting the
metric into "shared head" and "is a strict prefix of a previous prompt", and
letting the second one drive the recommendation.

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
`assistant_persona`. Every call therefore renders a bare `<formatting_guide>`,
so the two spell the tag the same way. What a call must *do* with the guide is
`turn_instructions`' job to say: the criteria call reads it as material to
restate, the decide call as the defaults its reply follows.

Per Finding 1 this uniformity buys no prefill on the current model — the pair
is a fork whatever the tag says, and a fork reuses nothing. It is a
consistency fix that keeps the prompt honest and would pay on a non-SWA model,
not a performance fix.

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

This was drafted as the proposal that fights the cache — criteria's full
history was thought to warm decide behind it. Finding 1 removes the conflict:
the pair is a fork, decide re-prefills from token 0 regardless, and criteria's
history is pure cost with no downstream benefit. **On the current model this
is the largest single saving in the note and it has no trade-off.** It becomes
a trade again only if the assistant moves to a non-SWA model, where criteria
really would prime the calls behind it.

### D. Ask whether the call should exist on trivial turns

If A lands, the criteria call's entire model-derived output is one sentence
about ambiguity — and the decide call already holds everything needed to
produce it. Worth asking, but only after A: the value of the criteria block is
partly that it is fixed *before* the work starts, and folding it into decide
gives that up. Not proposed, only flagged.

## Order

1. **C.** No trade-off while the model is SWA, and it is the biggest single
   number: ~11s off every turn.
2. **A**, gated behind the replay diff.
3. **B**, which is nearly free once A has removed the third copy.
4. **The `/activity` metric split**, so the page stops recommending a fix that
   does not apply to this stack.

Finding 1 is no longer first: there is no configuration to correct. What it
leaves behind is a constraint the other proposals are now written against —
prefill is paid in full on every call, so the only lever is sending fewer
tokens.

## How to verify

- **The runtime, directly.** Two `/api/chat` calls over a shared prefix with
  `prompt_eval_duration` compared says more in a minute than any dashboard.
  Vary the shape — identical, extension, fork — because on an SWA model those
  three have completely different costs.
- **`~/.ollama/logs/server.log`.** It states per request whether the slot was
  reused and why not, including the `forcing full prompt re-processing` line
  that settled this. `grep -c` on that phrase is a direct count of wasted
  prefill.
- **`/activity`**: reusable versus measured cached, per caller — read with
  Finding 1's caveat about what reusable means here.
- **`agents/test_assistant_prompt_tiers.py`**: the shared-prefix tests assert
  the byte-level property directly, so a builder edit that quietly reorders a
  section fails there rather than silently costing prefill.
- **`evals/profile_guidance.py`** and **`evals/profile_gate.py`**:
  prompt-construction variants against a given profile, for measuring a
  formatting change without live turns.
