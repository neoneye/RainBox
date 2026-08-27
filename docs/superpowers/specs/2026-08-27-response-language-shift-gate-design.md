# Gating the response-language classifier on a language shift

**Status:** Design approved. Nothing implemented.
**Date:** 2026-08-27
**Extends:** `source/notes/proposals/2026-08-17-gating-the-response-language-classifier.md`,
which established that a cheap detector may decide *whether to ask* the
classifier but must never decide *what language to use*. That constraint is
carried here unchanged. The parent of both,
`source/notes/proposals/2026-07-24-operator-locale-and-language.md`, decides
which language the reply uses and is not reopened.

## What this changes

`response_language_classifier` runs on every turn as the assistant's first
model-facing activity. This design adds a deterministic gate in front of it: the
classifier runs when the conversation's language may have shifted, and otherwise
the turn reuses the room's last recorded classification.

Measured over all 216 classifications recorded to date (2026-07-25 → 2026-08-25):

| metric | value |
|---|---|
| avg / p50 / p90 / max duration | 11.4s / 11.5s / 18.4s / 26.0s |
| avg tokens | 2280 in → 81 out |
| resolved model group | `assistant.default` — the role is unbound |

## The signal: a shift across a window, not a label for one message

The gate detects language per message and compares the current request against
the recent conversation. Both sides of that comparison come from the same
detector, so detector bias cancels: Danish mixed with English technical nouns
detects as `da` 0.50 / `de` 0.38, but if the preceding operator messages detect
the same way there is no shift and no call. A rule that asked whether one
message looks ambiguous in isolation would ask on every such message; this one
asks only when the message looks different from its neighbours.

### The window

The window is the last `WINDOW_MESSAGES` qualifying **operator** messages
preceding the current request, taken from the room message list the turn already
holds — the same list at `source/agents/assistant.py:3751`, whose rows carry
`sender_type` and `text`. No new storage and no new query.

Assistant replies are excluded. A reply is written in whatever language a
previous resolution chose, so a window containing replies partly measures the
gate's own past output, and a single wrong resolution would then justify itself
on every later turn. The classifier prompt already carries an anti-perpetuation
rule; the gate must not reintroduce underneath it the problem that rule exists
to prevent. Operator messages are the record of what the human actually wrote.

**Qualifying** means the message's Unicode letter count is at or above
`LETTER_FLOOR`. Acknowledgements carry no language content — measured, `ok`
detects as `da` 0.41 and `tak` as `sv` 0.31 — so they neither enter the window
nor, as the current request, trigger anything.

Each qualifying message contributes `min(letter_count, WEIGHT_CAP)` weight, so
one long paste cannot define the window on its own.

### The shift test

    window_dominant = argmax over languages of
        sum(confidence(language, message) * weight(message) for message in window)

    shift = detect_top(current_request) != window_dominant

`confidence` is lingua's full distribution
(`compute_language_confidence_values`), not just the winning label, so a message
that is 0.50/0.38 between two languages contributes to both in proportion.

Comparison is on the base language subtag. `en-US` and `en-GB` are one language
for the purpose of "has the conversation switched"; choosing between them is the
classifier's job and reaching it requires an explicit request, which trigger 2
below catches.

## Detection gates; the classifier decides

On a skip the turn reuses **the room's last recorded classification** — not the
detected language. The detected language is only ever compared, never adopted.

The last classification is read from the trace: the most recent
`assistant_step` row with `action='response_language_classifier'` and
`phase='observed'`, joined to `assistant_run.room_uuid`. Its
`observation_preview` holds the serialised `ResponseLanguageClassification`,
which is parsed back and installed exactly as a fresh classification would be,
including `_format_reply_language_markdown`.

This is what makes the design safe where using the detector as the classifier
would not be, and it handles the case the detector cannot see: an operator who
writes English but has asked for Danish replies produces no shift, so the turn
reuses the Danish resolution rather than the English the detector saw.

## The trigger a shift cannot see

> *"Please answer in Danish from now on."*

is English, in an English window. No shift. Skipped, the operator gets English.
The same holds for every request that names a language while being written in
the current one — dialect comparisons, multilingual tables, translation targets.
In the 216-classification sample, 8 of the 9 non-default results were driven by
an instruction naming a language rather than by the language the message was
written in, and 5 of those 8 were written in English.

So the gate also asks when **the request names a language**. The names and
endonyms come from CLDR (`langcodes` / `language_data`), matched against the
request text — never a hand-written table, which would be both a second source
of truth for the language list and an anglocentric one.

## The rule

Run `response_language_classifier` when **any** of:

1. the room has no previous recorded classification;
2. the request names a language (CLDR names and endonyms);
3. the request's detected top language differs from the window's dominant
   language;
4. the window contains no qualifying messages;
5. the detector raised, or was asked and found no language.

Otherwise reuse the previous classification.

A request below `LETTER_FLOOR` is never put to the detector, so it satisfies
neither 3 nor 5 — being too short to classify is not the same as being
unclassifiable, and only the second is worth a model call. Triggers 1 and 2
still apply to it, so `in Danish please` — short and explicit — still asks.

Every uncertainty resolves toward asking. A false ask costs one classifier call:
latency, never correctness. A false skip reuses the conversation's established
language, which is degraded and visible rather than a hard error.

## Two departures from the parent proposal

**The margin rule is dropped.** The parent asks the classifier when the top two
candidates are close. With a window that test is redundant in one direction and
harmful in the other: ambiguity landing on the window's own language is not
interesting, and ambiguity landing elsewhere is already a shift. Keeping both
would ask on every Danish technical message — the parent's trap 2, and the case
it named as deciding whether the feature is worth having.

**The detector is not restricted to the profile's languages.** The parent
restricts lingua to the tags in `languages.rows` for accuracy on short text, and
accepts that unknown languages force-fit to a declared tag; the margin rule was
that force-fit's mitigation. Unrestricted, a Finnish message detects as `fi`,
differs from the window, and asks — correct, with no threshold. The window and
the letter floor supply the stability restriction was buying.

The cost is memory: `from_all_languages()` loads models for ~75 languages.
Lingua loads them lazily by default, so the working set is the languages
actually seen. **Implementation must measure resident memory and first-call
latency in shadow mode.** If the cost is unacceptable, the fallback is the
parent's shape — restrict to `languages.rows` and reinstate the margin rule as
the force-fit mitigation — not an unmitigated restricted detector.

## Components

`source/agents/response_language_gate.py`, a new module with no dependency on
the assistant:

- `detect(text) -> Detection` — letter count, and when the text is at or above
  `LETTER_FLOOR`, the top language, its base subtag and the full confidence
  distribution. A `Detection` distinguishes its two empty cases: *below floor*
  (nothing was asked of the detector) and *undetected* (the detector was asked
  and found nothing). Only the second is trigger 5.
- `window_dominant(messages) -> str | None` — the weighted argmax above.
- `names_a_language(text) -> str | None` — the CLDR check; returns the matched
  name for the trace.
- `decide(messages, request) -> GateDecision` — a frozen dataclass carrying
  `should_ask: bool`, `trigger: str` (which of the five fired, or `"reuse"`),
  the window dominant, the request's top language and confidence, the window
  size, and the detector's elapsed milliseconds.

The detector instance is built once per process and reused; construction is not
on the turn's path.

`source/agents/assistant.py`: `_run_response_language_classifier` calls
`decide(...)` before building the prompt, and `_previous_room_classification()`
reads the last observed result.

`source/requirements.txt`: `lingua-language-detector`, `langcodes`,
`language_data`. Lingua is already vetted in this repo at 2.2.0 in
`source/voice_tts_dotstts/requirements.txt`; the main venv pins the same
version.

## Shadow mode

The gate ships computing its decision and acting on nothing. The classifier runs
every turn as it does today. The decision is written to the classifier step
row's `args` — a JSON column the trace already persists, and which
`_generic_action` in `webapp/assistant_components.py` renders as `arguments`
for any action without a bespoke pane, as this one is — as:

    {"gate": {"should_ask": bool, "trigger": str, "window_dominant": str|null,
              "window_size": int, "request_top": str|null,
              "request_confidence": float|null, "named_language": str|null,
              "detector_ms": int, "agreed": bool|null}}

`agreed` compares a would-be skip against what the classifier actually resolved:
`true` when the classifier's top base language matches the previous
classification's, `false` when the gate would have skipped and the classifier
resolved something else, `null` when the gate would have asked. The set of
`false` rows is the evidence that decides whether to enable — each one is a turn
that would have replied in the wrong language.

Enabling is a separate decision on separate evidence: a skip rate worth having,
and a disagreement set that is empty or explained. Thresholds are chosen from
shadow data. `LETTER_FLOOR = 16`, `WINDOW_MESSAGES = 8` and `WEIGHT_CAP = 400`
are starting values for collecting that data, not an operating point.

## Failure handling

The gate is wrapped so that any exception — import failure, detector error,
malformed trace row — logs and falls through to running the classifier. A gate
that cannot decide has decided to ask. In shadow mode a failure additionally
records `{"gate": {"error": "..."}}` and changes nothing else about the turn.

## Testing

- `detect` on the parent proposal's measured cases: English prose, Danish prose,
  Danish with English technical nouns, `ok`, `tak`, a stack trace, a bare URL.
- `window_dominant` on: a uniform window; a mixed window where weighting decides;
  a window whose only messages are below the floor (empty → trigger 4).
- `decide` on each of the five triggers, and on the reuse path.
- The four cases the design turns on, as named scenarios: Danish technical
  writing in a Danish window skips; `translate to english: <Danish>` in an
  English window shifts and asks; `Please answer in Danish from now on.` in an
  English window asks on trigger 2; `ok` in an English window reuses.
- `_previous_room_classification` round-trips a recorded classification back
  into `_reply_language_markdown`, and returns `None` for a room with no
  observed classifier row.
- A raising detector runs the classifier and records the error.
- Fixtures use whichever languages the case is about. Danish and English appear
  because they are the measured hard case, not because they are configured
  anywhere in shipped code.

## Not in scope

**Binding the classifier's model.** `assistant.response_language_classifier`
resolves to `assistant.default`, so a full model answers an 81-token question.
The parent measured the same call at 3.9s on a small model. Binding it on
`/agentmodel` is configuration with no code, it is worth more than this gate,
and the two are independent — but a gate skipping an 11.4s call and a gate
skipping a 3.9s call are different propositions, so the binding should land
first and the shadow data should be read against the bound number.

**Enabling the gate.** Shadow data first.

**Durable per-conversation language state.** The parent required it. Reading the
last classification from the trace removes the need, and adding state that
duplicates the trace would create a second source of truth for the same fact.
