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
`LETTER_FLOOR`. Acknowledgements carry no language content, and unrestricted the
detector says so in the only way it can — measured, `ok` tops out at `zu` 0.06
and `tak` at `mi` 0.08, noise at noise-level confidence. Against any window both
score below `SHIFT_FLOOR`, so without a floor every acknowledgement would ask.
The floor keeps them out of the window and, as the current request, out of the
shift test entirely.

Each qualifying message contributes `min(letter_count, WEIGHT_CAP)` weight, so
one long paste cannot define the window on its own. The cap bounds a long
message's influence rather than neutralising it, and `WEIGHT_CAP = 200` is
where that bound becomes real: measured against a 3560-letter English paste
(p(en) 1.00, so 200 after capping), a saturated eight-message Danish window
scores 243 and outvotes it, while three short Danish messages score 96 and do
not — which is correct, because 3560 letters of English is more language
evidence than three short sentences. Above ~280 the cap fails its own purpose:
at 400 the single paste outvotes even a full window.

### The shift test

    window_dominant = argmax over languages of
        sum(confidence(language, message) * weight(message) for message in window)

    shift = confidence(window_dominant, current_request) < SHIFT_FLOOR

`confidence` is lingua's full distribution
(`compute_language_confidence_values`), not just the winning label.

**The test asks how much of the window's language is in the request — not
whether the request's top label changed.** Measured unrestricted, Danish
confuses with Norwegian Bokmal (`da` 0.38 / `nb` 0.22 on ordinary Danish
technical writing), so a top-label test flaps inside a conversation that never
changed language, and every flap is a spurious call. The confidence in a
*named* language does not flap:

| request | p(en) | p(da) |
|---|---|---|
| English prose | 1.00 | 0.00 |
| English debugging message | 0.43 | 0.04 |
| Python stack trace | 0.67 | 0.01 |
| `Please answer in Danish from now on.` | 0.65 | 0.01 |
| Danish prose | 0.00 | 0.60 |
| Danish with English technical nouns | 0.01 | 0.38 |
| short Danish sentence | 0.02 | 0.23 |
| `translate to english: <Danish>` | 0.05 | 0.35 |
| Finnish prose | 0.00 | 0.00 |

Reading the column for the window's own language: same-language requests score
0.23-1.00, different-language requests score 0.00-0.05. `SHIFT_FLOOR = 0.15`
sits in that gap. The two cases the design turns on both land correctly —
Danish with English nouns in a Danish window scores 0.38 and skips;
`translate to english: <Danish>` in an English window scores 0.05 and asks.

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

Matching needs a precision filter, because `name_to_code` over CLDR's full name
set is far too permissive to point at raw text: measured, `the` resolves to
`thx`, `a` to `auq`, `to` to `toz` and `second` to `cs`. Scanning every token
unfiltered fires trigger 2 on nearly every English sentence, and a gate that
always asks is the status quo with extra steps. Three filters, all data-driven:

1. tokens shorter than `NAME_MIN_LETTERS = 4` are skipped, which removes the
   function-word matches;
2. the resolved code must be a two-letter ISO 639-1 tag, which removes the
   obscure three-letter matches;
3. **the token must round-trip** — it must appear in `code_to_names(code)`, the
   set of that code's recorded names. `second` resolves to `cs` but is not among
   Czech's names, so it is rejected.

The round-trip runs the same CLDR data in both directions, so the filter adds no
table of its own and stays language-agnostic. Measured over sixteen cases —
English, Danish, Italian and German requests naming a language, against
technical English and Danish prose, a stack trace and an acknowledgement — it
separates them without error, matching `Danish`, `dansk`, `italiano` and
`Deutsch` while rejecting every non-naming request.

## The rule

Run `response_language_classifier` when **any** of:

1. the room has no previous recorded classification;
2. the request names a language (CLDR names and endonyms);
3. the request's confidence in the window's dominant language is below
   `SHIFT_FLOOR`;
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
that force-fit's mitigation. Unrestricted, a Finnish request scores `fi` 1.00
and — measured — assigns 0.00 to both English and Danish, so it falls below
`SHIFT_FLOOR` for any window and asks. The force-fit trap closes by
construction rather than by a threshold.

Measured on this repo's Python with `lingua-language-detector==2.2.0`:
`from_all_languages()` builds in under a millisecond because models load
lazily, and resident memory settles at **141 MB** after detecting across eight
languages — not the gigabytes an eagerly loaded build would cost. Importing
lingua costs **2.35s once**. Because the switch ships off, that import is made
lazily inside the memoised detector builder rather than at module level: an
operator who never enables the gate never pays it, and the one who does pays it
on the first gated turn, where it is still a fifth of the call it replaces.
Detection is **5-13ms per message warm**, 64ms on the first call.

## Components

`source/agents/response_language_gate.py`, a new module with no dependency on
the assistant:

- `detect(text) -> Detection` — letter count, and when the text is at or above
  `LETTER_FLOOR`, the top language, its base subtag and the full confidence
  distribution. A `Detection` distinguishes its two empty cases: *below floor*
  (nothing was asked of the detector) and *undetected* (the detector was asked
  and found nothing). Only the second is trigger 5.
- `window_dominant(messages) -> str | None` — the weighted argmax above.
- `names_a_language(text) -> tuple[str, str] | None` — the CLDR check with its
  three filters; returns the matched name and the code it resolved to, for the
  trace.
- `decide(messages, request) -> GateDecision` — a frozen dataclass carrying
  `should_ask: bool`, `trigger: str` (which of the five fired, or `"reuse"`),
  the window dominant and size, `window_share` (the request's confidence in
  that dominant language — the number the shift test compares), the request's
  own top language, its letter count, the matched language name when trigger 2
  fired, and the detector's elapsed milliseconds. It serialises to the `args`
  block below.

The module reads no settings: the switch is the assistant's to read, so the gate
stays a pure function of the messages and is testable without a database.

The detector instance is built once per process and reused. Detection results
are memoised on a bounded module-level cache keyed by the message text, because
the window re-reads the same history every turn: without it a turn spends
8 x ~10ms redetecting messages whose language cannot have changed.

`source/agents/assistant.py`: `_run_response_language_classifier` reads
`assistant.response_language_gate` and, when it is on, calls `decide(...)`
before building the prompt — a skip costs neither the call nor the prompt
assembly. `_previous_room_classification()` reads the last observed result.

`source/requirements.txt`: `lingua-language-detector==2.2.0`, `langcodes`,
`language_data`. Lingua is already vetted in this repo at that version in
`source/voice_tts_dotstts/requirements.txt`. `langcodes` pulls `language_data`,
which carries the CLDR name tables trigger 2 reads.

## The switch

The gate ships behind `assistant.response_language_gate` — a bool in the
`db/settings.py` registry, **default off**, read per turn through the same
best-effort path as `assistant.formatting_guide` and
`assistant.knowledge_calibration`. An unreadable switch reads as off, which
means the classifier runs: the fail-safe direction.

Off, the turn behaves exactly as it does today. On, the gate decides, and the
difference is legible in the run trace the operator already reads.

The switch state joins the formatting and calibration switches in
`_build_turn_log`, so it appears on every step row this turn. That log exists
for "the first questions when troubleshooting a weird reply", and a reply in the
wrong language is precisely that question.

### What a skipped turn records

A skip still writes a `response_language_classifier` step row — `phase`
`"skipped"`, as the unbound-model path already does, and distinguished from it
by its reason. The row carries:

- `duration_ms`: the gate's own elapsed time, not a model call's. This is the
  measurement. The action goes from 9–18s rows to sub-second rows, in place, in
  every run, with the reason attached.
- `args`: the gate decision, `{"gate": {"should_ask": false, "trigger":
  "reuse", "window_dominant": str, "window_size": int,
  "window_share": float|null, "request_top": str|null, "request_letters": int,
  "named_language": str|null, "detector_ms": int}}`. `window_share` is the
  number the threshold is tuned on, so every row carries it. The same block is recorded on
  the ask path with the trigger that fired, so a run always says why the
  classifier ran or did not.
- `observation_preview`: the reused classification, serialised as the observed
  path serialises a fresh one — the row states which language the turn actually
  proceeded in, not merely that it declined to ask.
- no `system_prompt` or `user_prompt`. Nothing was built and nothing was sent;
  recording a prompt that was never used would misreport the turn.

`args` needs no rendering work: the classifier has no bespoke pane, so
`_generic_action` in `webapp/assistant_components.py` renders it as `arguments`.

### What the switch measures that shadow mode could not

The classifier is the turn's first model call, and its prompt warms the shared
prefix the later calls reuse. Skipping it moves that warming onto whichever call
now runs first, so the saving is the classifier's wall-clock **minus** the cost
that shifts. Shadow mode cannot see this at all — with the classifier still
running, the prefix is still warmed, and the projected saving would have been
the full 11.4s. Toggling the switch across comparable runs measures the real
figure, including the shifted cost.

### Recovering from a wrong skip

A skip that should have asked replies in the conversation's established
language. The correction needs no new machinery: the operator's next message
either is written in the other language, which is a shift, or names the language
it wants, which is trigger 2. Either way the following turn asks. This is why
the gate is acceptable without a disagreement corpus — a wrong skip costs one
turn and repairs itself on the next.

`LETTER_FLOOR = 16`, `WINDOW_MESSAGES = 8`, `WEIGHT_CAP = 200` and
`SHIFT_FLOOR = 0.15` are starting values. `SHIFT_FLOOR` and `WEIGHT_CAP` are
each placed against a measured crossover; the other two are judgement, and all
four are tuned against runs where the switch was on rather than fixed here.

## Failure handling

The gate is wrapped so that any exception — import failure, detector error,
malformed trace row — logs, records `{"gate": {"error": "..."}}` in `args`, and
falls through to running the classifier. A gate that cannot decide has decided
to ask, so a broken gate degrades to today's behaviour rather than to a wrong
language.

## Testing

- `detect` on the parent proposal's measured cases: English prose, Danish prose,
  Danish with English technical nouns, `ok`, `tak`, a stack trace, a bare URL.
- `window_dominant` on: a uniform window; a mixed window where weighting decides;
  a window whose only messages are below the floor (empty → trigger 4).
- `decide` on each of the five triggers, and on the reuse path.
- `names_a_language` matches `Danish`, `dansk`, `italiano` and `Deutsch`, and
  rejects `the`, `a`, `to` and `second` — one assertion per filter, so a
  regression names which filter broke.
- The four cases the design turns on, as named scenarios: Danish technical
  writing in a Danish window skips; `translate to english: <Danish>` in an
  English window shifts and asks; `Please answer in Danish from now on.` in an
  English window asks on trigger 2; `ok` in an English window reuses.
- `_previous_room_classification` round-trips a recorded classification back
  into `_reply_language_markdown`, and returns `None` for a room with no
  observed classifier row.
- A raising detector runs the classifier and records the error in `args`.
- The switch off runs the classifier regardless of what the gate would decide;
  an unreadable switch reads as off.
- A skipped row carries the reused classification, the gate decision, a
  gate-scale `duration_ms`, and no prompts.
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
first and the switch should be judged against the bound number.

**Turning the switch on.** The switch ships default off. Enabling it is the
operator's decision, taken from runs rather than from this document.

**Durable per-conversation language state.** The parent required it. Reading the
last classification from the trace removes the need, and adding state that
duplicates the trace would create a second source of truth for the same fact.
