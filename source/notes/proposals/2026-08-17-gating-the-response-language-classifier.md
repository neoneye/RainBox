# Gating the response-language classifier with cheap detection

**Status:** Proposed. Nothing implemented.
**Date:** 2026-08-17
**Extends:** `2026-07-24-operator-locale-and-language.md` — that proposal decides
*which* language the reply uses and is not reopened here. This one decides *when
it is worth asking a model at all*.

## The problem

`response_language_classifier` is the assistant's first model-facing activity and
runs on every turn. It is a narrow call — a small prompt, a handful of output
tokens — but it is answered by a full model, and on most turns it re-confirms the
language the previous turn already established.

## What it costs today

From 167 classifications in the live trace:

| metric | value |
|---|---|
| avg duration | 10.9s |
| p50 / p90 / max | 9.9s / 19.0s / 26.0s |
| share of total run wall-clock | 10.6% |
| avg input / output tokens | 1734 / 81 |
| model | **unbound** |

The binding-only `response_language_classifier` role exists on `/agentmodel` and
is not bound, so the assistant's own group answers it. The parent proposal
measured the same call at **3.9s on `gemma4:e4b`**. A 1734-in/81-out call taking
10.9s is a big model doing small work.

**This is the first finding and it is not a gate.** Binding the role to a small
model is a configuration change with no code, no new dependency and no
correctness surface. Any gate built before that is optimising the wrong term.

## What the 167 classifications do and do not show

Resolved top language:

| result | n |
|---|---|
| `en-US` | 158 (94.6%) |
| `en` | 5 |
| `en-GB` | 3 |
| `da` | 1 |

**This sample is not representative and must not be used to project a saving.**
It is dominated by debugging sessions, which are English-heavy by nature. The
operator's ordinary conversational traffic contains materially more Danish, and —
as measured below — Danish technical writing is the single hardest case for a
statistical detector, because it code-switches into English nouns. The skip rate
on representative traffic is **unknown**, and the honest range spans from "most
turns" to "few turns".

What the sample *does* establish is the shape of the exceptions, which is a
statement about the mechanism rather than about frequency. Of the 9 non-default
cases, **8 were driven by an explicit language instruction in the request**, not
by the language the message was written in. Five of those eight were themselves
written in English while naming other languages.

## Candidate detectors

| library | languages | notes |
|---|---|---|
| **lingua** | ~75 | Strongest on short text. Returns a full confidence distribution, not just a label. Restrictable to a language subset. Already vetted in this repo: `lingua-language-detector==2.2.0` in `voice_tts_dotstts/requirements.txt`. |
| fastText `lid.176.ftz` | 176 | 917KB model file, very fast, weaker on short text. The choice if coverage ever has to exceed the profile's declared languages. |
| langdetect / langid.py | 55 / 97 | Older, pure Python, poor on short text. No reason to prefer either here. |

**Recommendation: lingua**, built from the tags in `languages.rows` rather than a
hardcoded set — the language list is already storage the operator owns, and
deriving the detector from it keeps the mechanism tag-generic.

Measured, restricted to the five profile languages, warm:

| input | detected | ms |
|---|---|---|
| ordinary English sentence | `en` 0.98 | 2.7 |
| `translate to english: <Danish text>` | `da` 0.98 | 5.3 |
| `ok` | `da` 0.41 | 0.1 |

Sub-5ms against 10,900ms. The detector's cost is not a design consideration.

## The gate

The operator's formulation — *if the detector finds multiple candidates, ask the
model; if one language clearly dominates, don't* — is the right primitive. lingua
returns a confidence distribution, so "multiple candidates" is directly readable
as a small margin between the top two.

Measured on representative cases (`p1`/`p2` = top two confidences):

| case | top | p1 | p2 | margin | gate on margin? |
|---|---|---|---|---|---|
| English debugging message | `en` | 0.81 | 0.12 | 0.69 | skip |
| English prose | `en` | 0.99 | 0.00 | 0.99 | skip |
| Danish prose | `da` | 1.00 | 0.00 | 0.99 | skip |
| short Danish sentence | `da` | 0.94 | 0.04 | 0.90 | skip |
| **Danish with English technical nouns** | `da` | 0.50 | 0.38 | **0.11** | **ask** |
| `translate to english: <Danish>` | `da` | 0.98 | 0.01 | 0.97 | skip |
| "Please answer in Danish from now on." | `en` | 0.94 | 0.05 | 0.89 | skip |
| "Compare American and British English…" | `en` | 0.98 | 0.00 | 0.98 | skip |
| "…table of words in English, French, Spanish, Portuguese" | `en` | 0.99 | 0.00 | 0.99 | skip |
| `ok` | `da` | 0.41 | 0.28 | 0.13 | ask |
| `tak` | `sv` | 0.31 | 0.28 | 0.03 | ask |
| `Proceed with 5W1H` | `en` | 0.92 | 0.03 | 0.89 | skip |
| stack trace | `en` | 0.72 | 0.09 | 0.62 | skip |
| bare URL | `en` | 0.60 | 0.25 | 0.35 | ask |

Three findings follow, and they define the gate.

### 1. Margin alone is not enough — a second signal is needed

`translate to english: <Danish>` is detected as Danish at 0.98 with a 0.97 margin:
maximally confident, and the reply must be English. The margin rule skips it. What
catches it is that the detected language **differs from the previous turn's
resolved language** — Danish where en-US was established. So the gate needs both:

- **ambiguity** — a small margin between the top two candidates;
- **change** — the top candidate differs from the last resolved base language.

Neither subsumes the other. Ambiguity catches code-switched writing where the
previous language still dominates; change catches confident foreign text.

### 2. Neither signal can see an explicit language request

`Please answer in Danish from now on.` detects as English at 0.94 with a 0.89
margin. Single candidate, no change from en-US — **both rules skip it, and the
operator gets English.** The same holds for every request that names a language
while being written in the current one: dialect comparisons, multilingual tables,
translation targets. In the live sample this shape accounts for 5 of the 9
non-default classifications.

The third signal is therefore: **does the message name a language?** This is
cheap, deterministic, and independent of the detector. It must not be a
hand-written table — the parent proposal's trap 4 (duplicate sources of truth) and
the standing constraint against anglocentric hardcoding both apply. Source the
names and endonyms from CLDR data (`langcodes` / `language_data`), matched against
the request text.

This signal is load-bearing, not a refinement. A gate shipped without it will
silently ignore direct instructions, which is the single failure the operator is
least willing to accept.

### 3. Language-poor messages should reuse, not ask

`ok` (0.41/0.28) and `tak` (0.31/0.28) are ambiguous, so the margin rule asks the
model — and spends 10.9s on a message with no language content to classify. That
is backwards. The parent proposal already specifies the deterministic answer for
language-poor messages: recent operator language, then the profile's preferred
language. No model call is warranted.

So a length or token floor precedes the margin test: below it, reuse the previous
resolution. The language-mention signal still applies (`in Danish please` is short
*and* explicit).

### The rule, assembled

Ask `response_language_classifier` when **any** of:

1. the request names any language (CLDR names and endonyms), **or**
2. the detected top language differs from the last resolved base language at
   confidence above a floor, **or**
3. the top two candidates are within a margin threshold, **and** the message is
   above the language-poor floor, **or**
4. there is no previous resolution for this conversation, **or**
5. the detector raised, or returned nothing.

Otherwise reuse the previous resolution.

## Why detection may gate but must never decide

The parent proposal rejects statistical detection for this feature, and that
rejection stands: `translate to english: <Danish text>` is mostly Danish tokens
requesting an English reply, so a detector asked *what language is this* answers
the wrong question confidently.

The gate asks a different question — *has anything changed enough to be worth
asking* — and that question is well-posed for a detector. The distinction has a
concrete safety consequence:

- a **false ask** costs one classifier call: latency, never correctness;
- a **false skip** falls back to the previous resolution, which for a
  conversation that has not changed language is the answer the classifier would
  have given anyway;
- a detector exception or an unknown language fails open to asking.

The worst case is therefore a reply in the conversation's established language
when a change was wanted — degraded, plausible, and visible to the operator — not
a hard error. This is the property that makes the gate acceptable where using the
detector as the classifier would not be.

**Corollary:** the resolved language must be recorded per conversation so "the
previous resolution" is a real value and not an inference. It is already in the
step trace; the gate needs it as durable per-room state.

## Traps

1. **A restricted detector force-fits unknown languages.** Built from
   `languages.rows`, lingua cannot return anything outside those tags — a Finnish
   message becomes some declared language. Mitigation: the margin test. A
   force-fit is a low-margin result, which asks. Do not raise the margin
   threshold high enough to defeat this.
2. **Danish technical writing is the hardest real case**, not an edge case: 0.50
   `da` / 0.38 `de` on an ordinary sentence mixing Danish with English identifiers.
   If conversational traffic is largely this shape, the gate rarely skips and the
   saving evaporates. This is the measurement that decides whether the feature is
   worth having.
3. **The 94.6% figure is a debugging artifact.** Do not cite it as an expected
   skip rate. See the sample caveat above.
4. **Thresholds tuned on synthetic cases will not hold.** The numbers in this
   document come from constructed inputs. They establish the mechanism's shape,
   not its operating point.

## Staged plan

**Phase 0 — bind the model.** Bind `response_language_classifier` to a small
model on `/agentmodel` and re-measure. No code. Expected to remove the majority of
the cost this proposal is about, and it may lower the value of the gate enough to
change the decision. Nothing else starts before this is measured.

**Phase 1 — shadow mode.** Add the detector and compute the gate decision on
every turn, record it beside the classifier's result, and **act on nothing**. The
classifier keeps running. This produces, on representative traffic:

- the real skip rate;
- every disagreement, as a case where the gate would have skipped and the
  classifier resolved something other than the previous language.

Shadow mode is the whole evidence base and costs single-digit milliseconds a turn.

**Phase 2 — enable.** Only if Phase 1 shows a skip rate worth having and a
disagreement set that is empty or explained. Thresholds are chosen from Phase 1
data, not from this document. Keep a switch that forces the classifier on.

## Open questions

- **Where does the previous resolution live?** Per room, per journal, or per
  conversation window — and does it expire? A month-old resolution is weak
  evidence for the current turn.
- **Does the classifier's own output need a stability signal?** If it reports low
  confidence, the gate should probably not treat its result as a baseline worth
  reusing.
- **Does the gate interact with the parent proposal's eval corpus?** That corpus
  does not exist yet and is the parent's release gate. A gate is exactly the
  change it is meant to catch — Phase 2 may need to wait on it rather than on
  Phase 1 alone.
- **Should Phase 1 shadow data seed that corpus?** Real disagreements are better
  eval cases than constructed ones.

## Where to continue

- [ ] Bind `response_language_classifier` to a small model; re-measure latency and
      the share of run wall-clock. Decide from the new number whether to proceed.
- [ ] Record the resolved response language as durable per-conversation state.
- [ ] Add lingua as a main-venv dependency, built from `languages.rows`.
- [ ] Implement the language-mention check over CLDR names and endonyms; no
      hand-written table.
- [ ] Ship the gate in shadow mode; record decision, margin, top candidates and
      agreement with the classifier.
- [ ] Review shadow data for skip rate and disagreements; choose thresholds.
- [ ] Enable skipping behind a switch, with the classifier forceable on.
