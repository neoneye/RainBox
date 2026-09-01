# Where a turn's time goes, and the three copies of the locale

**Status:** Proposal. One part of it — rendering `formatting_guide` as a bare
tag so every call of the turn spells it the same way — is already the current
state, and Finding 1's fix (`LLAMA_ARG_SWA_FULL=1`) is applied and measured on
the live server. The rest is unbuilt.
**Date:** 2026-08-31
**Related:** `2026-07-23-reply-acceptance-criteria.md` (why the criteria call
exists), `2026-08-17-gating-the-response-language-classifier.md` (the same
question asked of the classifier), `2026-07-24-operator-locale-and-language.md`
(read before touching anything about language).

## The measurement

Everything in this section predates the SWA fix below; it is the baseline
the rest is measured against. One live run, 2026-08-31 16:37, `gemma4:e4b`, request: six words, no tool use,
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
  settings that the very next call prefilled again from scratch. That
  duplication is what Finding 1 turned out to be about, and it is now gone.
- 4.4s emitting 211 tokens, most of which restate deterministic data.

## Finding 1 — the cache break was a 1536-token window (fixed 2026-09-01)

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

### It is not the configuration it looks like

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
point deep inside a cached sequence.

### The number that explains it

At load, llama.cpp splits the cache in two:

```
llama_kv_cache_iswa: creating non-SWA KV cache, size = 102400 cells
llama_kv_cache: size = 1600.00 MiB (102400 cells,  4 layers), K/V f16
llama_kv_cache_iswa: creating     SWA KV cache, size =   1536 cells
llama_kv_cache: size =   60.00 MiB (  1536 cells, 20 layers), K/V f16
```

Four layers keep the full 102400-cell history. The other twenty keep a
**1536-cell sliding window**. Reuse therefore requires the divergence point to
sit within ~1536 tokens of the end of the cached sequence; past that the
window states for those positions have been overwritten and the whole prompt
is re-processed. criteria -> decide diverges at 6859 while the cache runs to
8898 — 2039 tokens back, just outside the window. That one number decides
every case below.

### The measured rule, with the window at its default

One model, one slot, `num_ctx=100k`, each shape measured against a prefix
nothing had cached. This is the behaviour the flag below removes; it is kept
because it is what returns if the flag is ever lost:

| shape | prompt | new tokens | prefill | reuse |
|---|---|---|---|---|
| identical repeat | 13650 | 0 | 24ms | total |
| extension — same prompt plus a tail | 12295 | 3140 | 5.3s (cold would be 17.5s) | head reused |
| fork — shared head, diverging at 45% | 12298 | 3143 | 18.0s at 684 tok/s | **none** |
| the real criteria -> decide pair | 13650 | ~6.3k | 20.9s at 652 tok/s | **none** |

**Divergence must land inside the sliding window.** An extension always does
(it diverges at the end); a fork 2000+ tokens back never does, and then a
shared head is worth exactly what sharing nothing is worth. In the log, 135
requests were forced into full re-processing, spread evenly across every
prompt size the assistant produces.

### The fix: a full-size SWA cache

`--swa-full` makes llama.cpp keep the whole window, restoring reuse from any
position. Measured on the llama-server ollama itself ships, run on a spare
port so the live server was untouched. `prompt_n` is tokens actually
processed:

| call | default | `--swa-full` |
|---|---|---|
| cold baseline | 8620 processed, 11.8s | 8620, 12.1s |
| extension | 2860, 4.6s | 2857, 4.6s |
| **fork at 45%** | **11483 processed (all of it), 16.3s** | **7612 processed, 11.4s** |

With the flag the fork reused every token up to the divergence point — 11483
minus 7612 is 3871, exactly the shared head — and cost nothing on the other
shapes. Applied to the measured turn: decide would process ~6.8k tokens
instead of 13.6k (21.8s -> ~11s) and reply_audit ~2.3k instead of 9.5k (14.6s
-> ~4s). **About 20s off an 83s turn, with no prompt change at all.**

The cost is memory: the SWA cache grows from 1536 cells to the full context,
roughly 60 MiB x (102400/1536) ~ 4 GiB at `num_ctx=100k`. This machine is an
M1 Max with 64 GiB, Metal reporting 47.5 GiB available, against 1.66 GiB of KV
cache today. Affordable here; it would not be on a small Mac.

### Reaching it through ollama — applied 2026-09-01

Ollama never passes `--swa-full` and has no setting for it, but the flag
carries an environment variable, `LLAMA_ARG_SWA_FULL`, and ollama passes
`LLAMA_ARG_*` through to llama-server (`LLAMA_ARG_FIT`, `LLAMA_ARG_FIT_TARGET`
appear in `ollama serve --help`).

```
launchctl setenv LLAMA_ARG_SWA_FULL 1
```

then quit and reopen Ollama. The server log confirms it at the next model
load:

```
llama_kv_cache_iswa: using full-size SWA cache
llama_kv_cache_iswa: creating non-SWA KV cache, size = 100096 cells
llama_kv_cache_iswa: creating     SWA KV cache, size = 100096 cells
```

The window went from 1536 cells to the full context. KV memory went from 1.66
GiB to 5.4 GiB (the SWA half is now 3910 MiB across 20 layers), against 47.5
GiB available.

Replaying the measured run's three big prompts in production order against the
live server afterwards:

| call | prefill, flag off | prefill, flag on | prompt |
|---|---|---|---|
| acceptance_criteria | 12.9s | 2.1s | 8690 |
| decide | 21.8s | **10.3s** | 13650 |
| reply_audit | 14.6s | **2.9s** | 9495 |

Read the decide and audit rows as real: each ran directly behind the call it
follows in production, which is exactly the cache state a live turn presents.
**Do not** read the criteria row that way — in a live turn it is the first
call and inherits whatever the previous turn left, whereas in the replay it
followed a decide prompt it shares 6859 tokens with. A live turn should expect
roughly 49s of prefill falling to the mid-20s, not to 15s.

### What this makes of the prompt-assembly work

Every call of the turn ends with its own `turn_instructions` and its own
request anchor, so every pair of calls is a fork by construction. With the
window at its default that made the block nesting inert. With the full window
it makes it valuable: reuse now runs up to the divergence point, so how late
`_ALL_STATIC_BLOCKS` pushes that point is paid straight back in prefill.
Keeping the per-call block sets nested is a live performance property now, not
a tidiness argument.

Consecutive decide steps within a turn remain the cheapest pair: the
scratchpad grows append-only and only the tail moves.

### Knobs that look relevant and are not

On this machine, measured rather than assumed:

- **`OLLAMA_FLASH_ATTENTION=1`** — already on. Ollama launches with
  `--flash-attn auto` and the server logs `Flash Attention enabled`.
- **`OLLAMA_NUM_PARALLEL=1`** — already the effective value; the launch line
  carries `-np 1`. (The variable also still exists in 0.32.15, despite the
  closed feature request arguing it should not be needed.)
- **`OLLAMA_KV_CACHE_TYPE=q8_0`** — halves KV memory. KV is 1.66 GiB out of
  47.5 GiB available here, so it buys ~830 MiB of a budget nothing is
  competing for, while adding a quality risk to a 4B model doing structured
  output. It does not touch the window size, which is the actual constraint.
- **`OLLAMA_CONTEXT_LENGTH=8192`** — actively wrong for this workload: the
  decide prompt is 13648 tokens. rainbox sends `num_ctx` per request anyway,
  so the variable would mostly reach other callers and truncate them instead.

The one general-advice item that does apply is **`keep_alive`**. The default
is 5 minutes, and the criteria call in the measured run spent 6.8s on
`llama-server started in 6.54 seconds` after an idle gap. A longer keep-alive
trades ~10 GiB resident for removing that from the first turn of a session.

### What `/activity` now means

`reusable_prefix_tokens` counts shared *leading text*. With the window at its
default that was close to meaningless — the page read "the runtime is losing
prefixes it could have kept" and recommended fewer models in rotation, which
was wrong advice for this stack. With the full window it is a fair predictor
again: reuse really does run to the divergence point, so reusable and cached
should now track each other.

That gives the page a new job. **Reusable high with cached near zero is now
the signature of a lost `LLAMA_ARG_SWA_FULL`** — an Ollama reinstall, a new
machine, a `launchctl` environment that did not persist. Worth reading it that
way rather than as a prompt-assembly problem.

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

Per Finding 1 this uniformity now pays: the pair reuses everything up to the
point where they first differ, so an attribute on one side and not the other
would cut the shared run at the guide's opening tag and cost decide the
guide's prefill.

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

This was drafted as the proposal that fights the cache, and with the full
window it does. criteria genuinely primes decide now: decide reuses everything
up to the point the two prompts first differ, and that point sits inside the
conversation history. Trimming criteria's history moves it earlier and hands
back part of the 11s the flag just won on decide — plausibly more than the
~11s the trim saves on criteria itself, because decide and audit both sit
behind it.

**So this one is now measure-first, not do-first.** The experiment is cheap:
render a criteria prompt with the short window, replay criteria -> decide ->
audit against the live server, and compare total prefill with the 15.3s the
current shape produces. Do not implement it on the strength of the original
~11s figure, which was computed when the reuse it destroys did not exist.

### D. Ask whether the call should exist on trivial turns

If A lands, the criteria call's entire model-derived output is one sentence
about ambiguity — and the decide call already holds everything needed to
produce it. Worth asking, but only after A: the value of the criteria block is
partly that it is fixed *before* the work starts, and folding it into decide
gives that up. Not proposed, only flagged.

## Order

Step 0 is done: `LLAMA_ARG_SWA_FULL=1` is set and measured, and it moved a
turn's prefill from ~49s toward the mid-20s without a prompt change. What
remains:

1. **A**, gated behind the replay diff. Independent of the cache — fewer
   output tokens, twenty fewer instruction lines, and a criteria block that
   stops being fresh model prose on every turn.
2. **B**, which is nearly free once A has removed the third copy.
3. **C**, and only after the measurement described in it. Its sign changed
   when the flag landed and it may now be net-negative.
4. **`keep_alive`**, if the ~6.5s model load on the first turn after an idle
   gap is worth ~10 GiB resident.

The `/activity` metric split is dropped: with the full window, reusable is a
fair predictor again and needs no companion number.

## How to verify

- **The flag is still on.** `~/.ollama/logs/server.log` at the next model load
  must say `using full-size SWA cache` and `SWA KV cache, size = 100096
  cells`. `1536 cells` means it was lost. This is the first thing to check
  when turn latency regresses.
- **The runtime, directly.** Two `/api/chat` calls over a shared prefix with
  `prompt_eval_duration` compared says more in a minute than any dashboard.
  Vary the shape — identical, extension, fork — because those three had
  completely different costs before the fix and should now behave alike up to
  the divergence point.
- **`/activity`**: reusable versus measured cached, per caller. They should
  track each other now; a gap means the flag, not the prompts.
- **`agents/test_assistant_prompt_tiers.py`**: the shared-prefix tests assert
  the byte-level property directly, so a builder edit that quietly reorders a
  section fails there rather than silently costing prefill.
- **`evals/profile_guidance.py`** and **`evals/profile_gate.py`**:
  prompt-construction variants against a given profile, for measuring a
  formatting change without live turns.
