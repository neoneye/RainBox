# The decision-model foundation

**Status:** Proposal. Nothing here is built. `benchmarks/decision_probe.py` exists.
**Date:** 2026-11-09
**Related:** every proposal that asks a System One question:
- [Noticing a repeated request](2026-11-09-noticing-a-repeated-request.md):
  the short-message Choice, the request profile, task identity, and the reply
  checklist.
- [Later verdicts on earlier messages](2026-11-09-later-verdicts-on-earlier-messages.md):
  the verdict Choice, hedges, and same-answer.
- [The room digest](2026-11-09-room-digest.md): topic assignment and the
  answer screen.

## Why a foundation

Three proposals ask about a dozen typed questions of local decision models.
Each describes the same contract in its own words: the request shape, bands,
slots, locality, and what to record. Built one proposal at a time, that
contract would be built three times, slightly differently each time. This
proposal is the one place it lives. The others name a question and a slot,
and inherit everything else.

## What System One is, as far as it matters here

- **The call.** `POST /v1/systemone` on the local Ollama server (0.40.15 or
  later) takes `{model, state, questions}`, and `questions` holds up to 64
  named questions.
- **Two question types are used:**
  - **Noul**, whose answer is `answers[name].noul`, P(yes) in [0, 1];
  - **Choice**, whose answer is `answers[name].probabilities` over 2–255
    named options.
- **Not used:** the Choice's `confidence` field, which measures how peaked
  the distribution is and is not a threshold; and Score.
- **Each question is answered in isolation**, against the shared state. A
  question that must see a list needs the list in the state, never only in
  another question's options. The probe learned this the hard way: a
  companion "new topic?" Noul could not see the topic list, and scored near
  chance.
- **The installed models are `d1:3b` and `d3:4b`.** They carry the
  `decision` capability. Measured on this machine (M1 Max, 64 GB):
  - d1 answers one question in about 0.1 s, and 14 in one request in 0.4 s;
  - d3 answers one in about 0.3–0.4 s, but scales badly within a request: 14
    took 4.6 s and 18 took 10.3 s.

## The design

### 1. One client module

`llm/systemone.py` holds the only code that talks to `/v1/systemone`.

```python
@dataclass(frozen=True)
class Question:
    name: str
    kind: Literal["noul", "choice"]
    instructions: str            # versioned, code-owned, no example phrases
    options: dict[str, str] | None = None   # choice only: name -> description

@dataclass(frozen=True)
class Answer:
    name: str
    status: Literal["ok", "unknown"]
    p: float | None              # noul: P(yes); choice: P(top option)
    label: str | None            # noul: "yes" | "no" | "unclear"; choice: top option or None
    probabilities: dict[str, float] | None
    reason: str | None           # why unknown: timeout, invalid, unbound, not_local, oversized

def ask(slot: str, state: dict, questions: list[Question], *,
        deadline_s: float, max_state_chars: int,
        bands: Bands = DEFAULT_BANDS) -> list[Answer]:
    """One request. Never raises for a model problem: every failure is an
    Answer with status 'unknown' and a reason, so callers have one code path."""
```

**Validation.** Every answer is checked before it is believed:
- the expected names are present, and no extra ones;
- Noul values are finite and in [0, 1];
- Choice probabilities are keyed by exactly the options, and sum to 1 within
  0.01.

Anything else is `unknown`.

**State is never cut to fit.** A state whose serialized length exceeds
`max_state_chars` is not truncated: the whole request is `unknown` with
reason `oversized`. A judgement on half a task is worse than none.

**Bands live in code, versioned with the question set.**
- **Defaults:** Noul `yes` ≥ 0.8, `no` ≤ 0.2; Choice accepted at ≥ 0.8.
- **Per question:** a caller can set its own band, for example the identity
  screen's 0.5, and the band used is recorded.

### 2. Slots, and verified local execution

| slot | purpose | probe default |
|---|---|---|
| `assistant.repeat_check` | fast, on the request path: short-message Choice, request profile, topic Choice, identity screen, reply checklist | `d1:3b` |
| `assistant.repeat_confirm` | careful, mostly after the turn: identity confirmation, verdicts, hedges, same-answer, the digest's answer screen | `d3:4b` |

**Binding rules:**
- **No fallback.** Neither slot falls back to `assistant.default`: a
  generative model cannot answer this API. An unbound slot makes every
  question on it `unknown` with reason `unbound`.
- **Exactly one model per slot.** The first eligible member of its group,
  resolved once per turn or job.

**Local means verified local, not a loopback address.** A local Ollama
server relays any model whose name carries `:cloud` to Ollama's servers. A
loopback host is therefore necessary but not sufficient. At bind time and at
execution time, the client:

1. **Refuses** a model name with a `:cloud` suffix.
2. **Reads** `/api/show` for the model. It accepts the model only if the
   response reports local weights: a non-empty `model_info` and a local
   digest that matches `/api/tags`. It refuses a remote or unknown
   indicator.
3. **Records** the model digest and the server version on every call.

Both slots count in `assistant_models_all_local()`, so a remote member makes
the diary gate say no, exactly as a remote decide model does.

**How the remote indicator reads in Ollama's responses** has to be confirmed
when this is built, against a deliberately configured `:cloud` model. The
rule above is the intent; the field it reads is the builder's to verify.

### 3. Residency and heat

Memory is not the constraint. The decide model (`gemma4:e4b-mlx`, 9.5 GB),
`d1:3b` (3.9 GB) and `d3:4b` (5.6 GB) total about 19 GB of 64.

Heat is the constraint. This laptop has been repaired twice after heat
damage. The rules:

- **`d1` stays resident while the assistant is active.** It answers on the
  request path, and a cold load (measured at about 1.4–2.6 s for its first
  call) would cost more than its questions do.
- **`d3` does not need to stay resident.** It mostly serves after-turn
  checks, where its load time is off the critical path. It runs with Ollama's
  default `keep_alive`.
- **Decision calls on the request path are capped**: two per turn by default
  (the short-message/profile/topic request, and at most one draft checklist).
  More would need its own measurement of the decide call's prefill time
  alongside, since a decision call on the same GPU can slow it.
- **After-turn calls go through the housekeeping queue**
  ([post-turn housekeeping](2026-11-09-post-turn-housekeeping.md)), which
  never runs them while a turn is in progress.

### 4. Every call is recorded the same way

Each `ask()` writes one row to a new `decision_call` table:

```
decision_call
  uuid, created_at, run_uuid (nullable), step_uuid (nullable), job (nullable)
  slot, model, model_digest, server_version
  question_set_version, bands_version
  state_digest           sha256 of the serialized state
  questions              names and instruction versions, never the state text
  answers                raw probabilities per question, and the banded label
  status, reason, elapsed_ms, deadline_ms
```

**Why the state is not stored.** It is the operator's messages, and those
already live in their own rows under their own access rules. The digest plus
the run and step ids reproduce it, and the trace view reads it through the
existing trace-access policy.

**On /assistant**, each decision call appears as a code-driven row where it
happened. It shows the questions, the banded answers, the raw probabilities
on expand, the model and the time.

### 5. The probe becomes the regression suite

`benchmarks/decision_probe.py` and its fixture are already the measured
record of every question. The foundation makes them the gate:

- **Question text lives in one place.** Each question's production
  instructions are defined in `llm/systemone_questions.py`. The fixture
  references questions by name and version instead of repeating the text, so
  the probe measures exactly what production asks.
- **A model or question change is a probe run.** Changing a slot's model, its
  digest (an `ollama pull` that changes weights), or a question's text means
  running the probe and committing the run file before the change is used.
- **What a run report shows next to the previous run:** top-answer accuracy,
  answers inside the band, wrong answers inside the band, abstentions, and
  the median time per question.
- **Operator labels grow the suite.** Labels from the
  [detector labels](2026-11-09-detector-labels.md) surface become new cases:
  the operator's real disagreements, translated into invented text before
  they enter the fixture, so no live data reaches the repo.

### 6. Calibration, measured rather than assumed

A probability of 0.9 is only meaningful if, among answers scored 0.9, about
nine in ten are right. Once detector labels exist, a periodic report bins
each question's recorded answers by probability. It compares the bins with
the labelled outcome and shows the reliability curve per question and model.

A question whose accepted answers are wrong more often than its band
implies gets a stricter band, or is retired. That is decided from the report,
never from the probe's handful of invented cases.

## Measuring it

- **Client correctness** is unit-tested against recorded responses: valid,
  missing keys, extra keys, values out of range, a Choice whose probabilities
  do not sum to 1, a timeout, an oversized state, an unbound slot, and a
  `:cloud` model. Each must produce the right `unknown` reason.
- **Locality** is checked once against a real `:cloud` model in a sandbox
  configuration, to confirm which response field reveals it.
- **Cost:** per-turn decision time on the request path (p50/p95), and the
  decide call's prefill time with and without the decision calls before it.

## What this does not do

- **Decide what any question is used for.** That stays in each feature's
  proposal, with its own release boundary.
- **Use tool calling or structured generation.** System One is neither. It is
  a typed scoring endpoint. Plain-text generation stays plain text, as it is
  everywhere in this codebase.
