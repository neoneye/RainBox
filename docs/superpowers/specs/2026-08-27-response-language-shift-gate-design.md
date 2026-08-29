# Gating the response-language classifier on a language shift

**Status:** Implemented. Ships behind `assistant.response_language_gate`,
default off.
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

**Qualifying** means the message carries language, which it can prove two
ways: a Unicode letter count at or above `LETTER_FLOOR`, **or** a detector
confidence at or above `CONFIDENCE_FLOOR`. Either is enough.

The letter count alone would be a script test wearing the costume of a length
test. It is a proxy for information content, and the proxy only holds for
scripts that spell words out: a complete Chinese sentence is ten characters and
`你好` is two, both detected at 1.00. Where the script alone nearly fixes the
language, length stops meaning anything, and requiring it discards fluent
requests as though they were acknowledgements.

Confidence alone does not work either. Measured, the weakest short real
non-Latin text scores 0.26 (Cyrillic — like Latin, shared by several languages,
so the confidence spreads thin), while `hej med dig` scores 0.10 and an
ordinary English debugging line 0.19. There is no threshold separating those
from noise. Taking the two together does separate them, because they fail on
different inputs:

| message | letters | confidence | qualifies |
|---|---|---|---|
| `我现在用的是什么语言？` | 10 | 1.00 | yes, on confidence |
| `你好` | 2 | 1.00 | yes, on confidence |
| `как дела` | 7 | 0.26 | yes, on confidence |
| an English debugging line | 47 | 0.19 | yes, on length |
| `hej med dig` | 11 | 0.10 | no |
| `ok` | 2 | 0.06 | no |
| `tak` | 3 | 0.08 | no |
| `y` | 1 | 0.12 | no |

Latin noise tops out at 0.12 and the weakest real non-Latin text scores 0.26,
so `CONFIDENCE_FLOOR = 0.20` sits in that gap. A message that qualifies on
neither is language-poor: it stays out of the window and, as the current
request, out of the shift test entirely, so an acknowledgement never spends a
model call.

**Every message casts one vote, scaled to its own strongest candidate, and
votes decay with age.** Neither raw confidence nor length compares across
messages, and weighting by either lets the window answer for a conversation
that has already moved on.

Confidence is not comparable between languages. Danish detects at 1.00 where
an equally clear English sentence reaches 0.375, because English shares its
script and much of its vocabulary with more of the field. Summing raw
confidence therefore counts a Danish message roughly three times as heavily as
an English one that is just as unambiguous.

Length is not comparable either. It was included on the reasoning that a long
message says more about the conversation's language than a short one — but a
long passage the operator quoted is not better evidence of what they are
writing in than the sentence they just typed. Measured on a real conversation
that had switched to English three messages earlier, one 152-letter Danish
message at p 1.00 scored 152 against 63 for the three English messages after
it, so the window kept answering Danish and every turn asked. Capping how much length any one
message could contribute bounds that, but only loosely: a cap tight enough to
stop a long paste winning is also tight enough that ordinary long messages stop
counting for more than short ones, which is the whole reason length was there.

Scaling each message's shares to its own top candidate makes one message one
vote regardless of script or length, and `WINDOW_HALF_LIFE = 3.0` messages
decays those votes so the window tracks what the conversation is running in
now. Measured against the three cases that matter:

| window | confidence x length | one vote, decayed |
|---|---|---|
| switched to English 3 messages ago | `da` ✗ | `en` ✓ |
| Danish throughout | `da` ✓ | `da` ✓ |
| one long English paste among Danish | `en` ✗ | `da` ✓ |

The old scheme gets two of the three wrong; the vote scheme gets all three, and
by clear margins rather than narrow ones.

### Quoted passages are content

A message can hold more than one language, and usually the extra one is not the
operator's: `Kan du forklare mig dette: "standard arm orange"` is a Danish
request about an English phrase. Detection reads a message whole, so it answers
by volume — and volume belongs to whatever was pasted in. Measured, a Danish
sentence wrapping a 150-character English quote scores `en` 1.00 and `da` 0.000,
which reads a Danish turn as a switch, and a few such messages carry the window
to English so the gate stops skipping for anyone who quotes habitually.

Detection therefore runs on the message with quoted spans and code removed —
matched double quotes, guillemets, curly quotes, backticks and fenced blocks —
and falls back to the whole message when nothing is left, because then the
quote *is* the message. Single quotes need four characters between them so that
an apostrophe in `LLM'en` or `don't` does not open a span.

This corrects both directions: the Danish request above reads `da`, and
`Can you explain this to me: "det virker ikke rigtigt"` reads `en`, where
reading it whole answered `da`. It also sharpens ordinary technical writing —
`Hvorfor fejler ` + "`db.session.query(AppSetting)`" + ` her?` goes from `da`
0.53 to 0.94 once the identifier stops being read as prose.

Naming a language inside a quote still triggers the name check, which reads the
raw text: quoting is not a way to smuggle an instruction past the gate, and the
classifier is the right place to judge one.

### The shift test

    window_dominant = argmax over languages of
        sum(confidence(language, message) * weight(message) for message in window)

    strongest = max(confidence(language, current_request) for language in ...)
    shift = confidence(window_dominant, current_request) < SHIFT_RATIO * strongest

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
0.23-1.00, different-language requests 0.00-0.05.

**The comparison is a ratio, not an absolute floor**, because those absolute
numbers are not comparable between messages. Short Latin text spreads its
confidence across every language sharing the script, so the correct answer
scores low for reasons that have nothing to do with whether the language
changed: `what do I do for work` gives English only 0.106, and `kan du hjaelpe
mig med det her` gives Danish only 0.106. An absolute floor calls both of those
a shift and spends a model call on an unchanged conversation.

What stays stable is how much of its *own best guess* the request gives to the
window's language. Measured across English, Danish, Spanish and Chinese
windows, a request in the window's language scores **1.00** on that ratio every
time — the window's language is simply the request's top candidate — while a
genuine change tops out at **0.311** (English against a Danish window, which
share a script and some vocabulary). `SHIFT_RATIO = 0.5` sits in that gap.

The two cases the design turns on both land correctly: Danish with English
nouns in a Danish window scores 1.00 and skips, even though its absolute Danish
share is only 0.38 and its top label flaps toward Norwegian;
`translate to english: <Danish>` in an English window scores 0.05 absolute and
asks.

In the assembled gate that translate request is in fact caught one step
earlier, by the name check below: the text contains the token `english`. Both
routes ask, so the outcome is the same, and the shift test remains the signal
that catches the same request with its target language unnamed. The tests pin
the outcome for the literal case and the shift route for an unnamed variant,
because what must never regress is that such a request asks at all — not which
of the two signals gets there first.

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
endonyms come from CLDR, via `language_data.names` (`name_to_code` and
`code_to_names`), matched against the request text — never a hand-written
table, which would be both a second source of truth for the language list and
an anglocentric one.

Matching needs a precision filter, because `name_to_code` over CLDR's full name
set is far too permissive to point at raw text: measured, `the` resolves to
`thx`, `a` to `auq`, `to` to `toz` and `second` to `cs`. Scanning every token
unfiltered fires trigger 2 on nearly every English sentence, and a gate that
always asks is the status quo with extra steps. Two filters, both data-driven:

1. tokens shorter than `NAME_MIN_LETTERS = 4` are skipped, which removes the
   function-word matches;
2. **the token must round-trip** — it must appear in `code_to_names(code)`, the
   set of that code's recorded names. `second` resolves to `cs` but is not among
   Czech's names, so it is rejected; so is `margin`, which resolves to `mrt`.

Both length rules are Latin-script heuristics: they exist because short Latin
function words collide with obscure language names. In Chinese, Japanese and
Korean one character is a morpheme, the names run two to three characters
(`中文`, `英语`, `日本語`, `한국어`), and the collision does not arise — measured over
42 common words in those scripts the round-trip admits the genuine language
names and nothing else. Tokens in those scripts are therefore exempt from both
minimums.

Those scripts also write without spaces, so a whole clause arrives as one token
and the name has to be found inside it. For such a token the check scans every
two-to-four-character substring, which is bounded by the length of the names it
is looking for and costs about a millisecond on a 640-character paste.
Measured over ordinary Chinese sentences it yields no spurious match. Note that
`我现在用的是什么语言？` — *what language am I writing in* — correctly matches no name:
it asks about a language without naming one, and belongs to the shift test.

The round-trip runs the same CLDR data in both directions, so the filter adds no
table of its own and stays language-agnostic. Measured over a nineteen-case
corpus — English, Danish, Italian and German requests naming a language, against
technical English and Danish prose, a stack trace and acknowledgements — the two
filters separate them without error.

**The resolved code is not restricted to ISO 639-1.** An earlier draft also
required a two-letter code. Measured, that filter contributes nothing to
precision — the corpus scores 19/19 with or without it, because the round-trip
rejects every false positive on its own — while it silently confines the check
to the ~180 languages that hold a two-letter tag. Without it `Cherokee` (`chr`),
`Cebuano` (`ceb`) and `Hawaiian` (`haw`) are recognised, so `names_a_language`
returns whatever length of code CLDR gives. Nothing downstream compares that
code; only the matched token is recorded on the trace.

The length minimum, by contrast, is load-bearing and cannot come down: at three,
`the` resolves to `thx` **and round-trips**, taking 8 false positives across the
same corpus. The cost is that language names of three letters or fewer — Ewe,
Lao, Twi, Ido — are not recognised by this trigger. A request naming one of them
is caught only if it also reads as a language shift.

A code longer than two letters needs six letters, not `NAME_MIN_LETTERS`'s four
(`NAME_LONG_CODE_MIN_LETTERS = 6`). Measured against 875 real operator messages
above `LETTER_FLOOR`, the four-letter floor alone fired on 104 of them (11.9%),
and 57 of those (6.5% of all traffic) were false: ordinary English words that
round-trip into an obscure language's name in some locale rather than being a
mention of that language — `more` into Mossi (`mos`, 37 occurrences by itself),
`even` into Even (`eve`), `meta` into `mgo`, `logo` into `log`, `male` into
`ms`. These are genuine CLDR names, so the round-trip is working as designed;
the false fires are homographs, not lookup errors. Restoring a flat two-letter
requirement — the filter an earlier draft removed — would fix them, but at the
cost of losing `Cherokee`, `Cebuano` and `Hawaiian` again. Raising the floor
only for codes longer than two letters keeps both: `Cherokee`, `Cebuano` and
`Hawaiian` are each at least six letters long and still resolve, while `more`,
`even`, `meta` and `logo` fall under the higher floor and are rejected. On the
same 875 messages this takes the false-fire rate from 6.5% to 0.8% of traffic;
the residual false tokens (`lets`, `male`, `persia`, `angle`, `island` — 7
messages) all resolve to two-letter codes, which the higher floor does not
touch, and a false fire costs one classifier call, never correctness.

The bounded cost of this floor is not only the homographs it was raised to
drop — it is every genuine CLDR language whose name is four or five letters
and whose code is three letters or more: `Bemba` (`bem`), `Sakha` (`sah`),
`Dogri` (`doi`), `Erzya` (`myv`), `Khasi` (`kha`), `Mizo` (`lus`), `Zarma`
(`dje`), `Tulu` (`tcy`), `Tigre` (`tig`), `Mende` (`men`) and `Karen` (`kbj`)
among them go unrecognised by this trigger, same as the homographs. A request
naming one of them is caught only if it also reads as a language shift.

## The trigger neither the shift test nor the name check can see

The classifier's prompt carries the operator's declared `/profile` languages
(`user_settings_languages_json`), and its result is reconciled against the
profile's declared variants (`_reconcile_response_language_profile_variants`),
so a classification is a function of the profile as well as of the messages.
An operator who edits their declared reply languages on `/profile` and keeps
writing in the language they always have produces no shift and names nothing —
both signals the gate already has read the turn as unchanged, yet the
classification a skip would reuse was resolved against the profile as it stood
before the edit.

The comparison does not read the profile off the reused classification at
all. The classifier is instructed to copy every declared profile-language
code exactly into its result, but that contract is not reliably honoured —
`_reconcile_response_language_profile_variants` exists because the model
sometimes collapses `en-GB` and `en-US` into the broader `en`, and it declines
to repair that, reporting it as a contract failure instead. Inferring the
profile from the classification's codes would either miss a removed code (a
code present in the classification but no longer declared is indistinguishable
from one the request itself required) or fire on every turn in a room where
the model routinely omits a declared code.

Instead, the observed classifier row snapshots the profile's declared codes
as they stood at the moment it was resolved — a plain list written into the
row's `args` alongside the `gate` block, independent of anything the model
returned. The comparison is then symmetric: the profile's currently declared
codes against that snapshot, as sets, with `!=`. An addition, a removal, or a
retag (the same code count, different tags) all trigger, because all three
mean the reused classification no longer describes what is currently
declared. A row written before this snapshot existed carries none, which
reads as changed — one extra ask for that room, and every row after it
carries a snapshot to compare exactly.

This comparison is not a function of `window_texts` or `request_text`, so it
stays out of `agents/response_language_gate.py` entirely: the assistant
computes the boolean (`AssistantAgent._profile_languages_changed`) and passes
it into `decide()` as `profile_languages_changed`. The gate module never reads
a profile and is still testable without one; only the assistant's own tests
exercise the comparison.

## The rule

Run `response_language_classifier` when **any** of:

1. the room has no previous recorded classification;
2. the request names a language (CLDR names and endonyms);
3. the profile's currently declared languages differ from the snapshot taken
   when the reused classification was recorded;
4. the request gives the window's dominant language less than `SHIFT_RATIO` of
   its own strongest candidate;
5. the window contains no qualifying messages;
6. the detector raised, or was asked and found no language.

Otherwise reuse the previous classification.

A language-poor request satisfies neither 4 nor 6 — carrying too little
language to classify is not the same as being unclassifiable, and only the
second is worth a model call. Triggers 1, 2 and 3
still apply to it, so `in Danish please` — short and explicit — still asks,
and so does a profile-language edit on a turn whose request is just `ok`.

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
`SHIFT_RATIO` for any window and asks. The force-fit trap closes by
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

- `detect(text) -> Detection` — letter count, and for a message that carries
  language, the top language, its base subtag and the full confidence
  distribution. A `Detection` distinguishes its two empty cases:
  *language-poor* (too little language to be worth classifying, so its shares
  are dropped rather than reported — nothing downstream may vote with noise)
  and *undetected* (the detector was asked and found nothing). Only the second
  is trigger 6.
- `window_dominant(texts: Sequence[str]) -> tuple[str | None, int]` — the
  weighted argmax above, with the number of messages that qualified. `(None,
  0)` when none did, which is trigger 5.
- `names_a_language(text) -> tuple[str, str] | None` — the CLDR check with its
  two filters; returns the matched name and the code it resolved to, for the
  trace. The code may be two or three letters.
- `decide(*, window_texts, request_text, has_previous,
  profile_languages_changed) -> GateDecision` — a frozen dataclass carrying
  `should_ask: bool`, `trigger: str` (which of the six fired, or `"reuse"`),
  the window dominant and size, `window_share` (the request's confidence in
  that dominant language — the number the shift test compares), the request's
  own top language, its letter count, the matched language name when trigger 2
  fired, and the detector's elapsed milliseconds. It serialises to the `args`
  block below. `profile_languages_changed` is the one argument that is not
  derived from `window_texts` or `request_text`; the caller computes it (see
  "The trigger neither the shift test nor the name check can see" above) so
  the module stays a pure function of message text.

The module reads no settings: the switch is the assistant's to read, so the gate
stays a pure function of the messages and is testable without a database.

The detector instance is built once per process and reused. Detection results
are memoised on a bounded module-level cache keyed by the message text, because
the window re-reads the same history every turn: without it a turn spends
8 x ~10ms redetecting messages whose language cannot have changed.

`source/agents/assistant.py`: `_run_response_language_classifier` reads
`assistant.response_language_gate` and, when it is on, calls `decide(...)`
before building the prompt — a skip costs neither the call nor the prompt
assembly. On the observed path, before recording the row, it also snapshots
the profile's declared codes into `args["profile_declared_language_codes"]`
(`user_profile.declared_language_candidates(profile)`), a sibling of the
`gate` block written only there — never on a skip, since only observed rows
are ever read back.
`_previous_room_classification()` reads the last observed row and returns the
parsed classification together with that snapshot (`frozenset[str] | None`,
`None` when the row predates the snapshot), both from the one query.
`_response_language_gate_decision` calls
`_profile_languages_changed(profile, declared_snapshot)` — comparing the
profile's currently declared codes against the snapshot, as sets, with `!=`
— to build the `profile_languages_changed` argument `decide()` needs; the
gate module itself never receives or reads `profile`.

`source/requirements.txt`: `lingua-language-detector==2.2.0`, `language_data`.
Lingua is already vetted in this repo at that version in
`source/voice_tts_dotstts/requirements.txt`. `language_data` carries the CLDR
name tables trigger 2 reads directly — nothing here imports `langcodes`, and
`language_data` does not depend on it, so it is not a requirement of this
gate.

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

Rendering is dedicated, not generic. The classifier's row is `code_driven`
and its phase is `observed`, `failed` or `skipped`, so `_step_events` in
`source/db/assistant_log.py` never emits an `action` event for it — the
`_generic_action` renderer in `webapp/assistant_components.py` never sees
this row on either path. On the ask path (`observed` or `failed`) the row is
a `kind: "llm"` event, and `_llm` renders a "gate decision" block from
`payload["gate"]` above the prompts, so the trigger that sent an 11s call out
is visible without opening them. On the skip path the row is a
`kind: "skipped"` event; `_skipped` tells the two rows that share that phase
apart by the `gate_replaced_call` marker — without it, nothing ran and
nothing failed, so it renders only the reason and any error; with it, the
gate ran in place of the call, and the pane renders the reason, the same
"gate decision" block, and the reused classification instead of a
"never made" note.

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
language. Most of the time the correction needs no new machinery: the
operator's next message either is written in the other language, which is a
shift, or names the language it wants, which is trigger 2 — either way the
following turn asks, which is why the gate is acceptable without a
disagreement corpus for those paths.

A profile-language edit is different, and is the one wrong-skip path that does
not repair itself on its own. If the operator changes their declared reply
languages on `/profile` and then keeps writing in the language they already
were, there is nothing for the shift test to see and nothing for the name
check to match — without trigger 3 that skip would recur on every later turn,
because a stale classification does not become fresher by being reused again.
Trigger 3 exists specifically to close this path: the moment the profile's
currently declared codes no longer match the snapshot taken when the reused
classification was resolved, the next turn asks, and the recovery claim above
holds for every trigger rather than for five of six.

`LETTER_FLOOR = 16`, `CONFIDENCE_FLOOR = 0.20`, `WINDOW_MESSAGES = 8`,
`WINDOW_HALF_LIFE = 3.0` and
`SHIFT_RATIO = 0.5` are starting values. `SHIFT_RATIO` and `CONFIDENCE_FLOOR`
are each placed against a measured gap; the other two are judgement, and all
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
- `decide` on each of the six triggers, and on the reuse path. The
  profile-changed trigger is exercised at the gate level by passing
  `profile_languages_changed=True` directly (the module takes it as a plain
  bool and does not compute it), and at the assistant level by
  `_response_language_gate_decision` covering an addition, a removal, a
  retag (same code count, different tags — a one-way comparison would miss
  this and a removal alike), an unchanged profile, a row with no snapshot at
  all (reads as changed), and a classification whose own codes omit a
  declared code the profile still declares unchanged (the
  `_reconcile_response_language_profile_variants` shape) still reusing,
  because the comparison never reads the classification's codes.
- `names_a_language` matches `Danish`, `dansk`, `italiano` and `Deutsch`, and
  languages with no two-letter code (`Cherokee`, `Cebuano`, `Hawaiian`). Each
  filter gets an assertion that isolates it — one that fails if that filter
  alone is removed: `Ewe` for the length minimum, `second` for the round-trip,
  `more` for `NAME_LONG_CODE_MIN_LETTERS` (it round-trips into Mossi, `mos`,
  and is the single largest false fire measured on real traffic).
- The four cases the design turns on, as named scenarios: Danish technical
  writing in a Danish window skips; `translate to english: <Danish>` in an
  English window asks (by the name check, since it names English; the same
  request with its target unnamed asks by the shift); `Please answer in Danish from now on.` in an
  English window asks on trigger 2; `ok` in an English window reuses.
- `_previous_room_classification` round-trips a recorded classification (and
  its declared-codes snapshot) back into `_reply_language_markdown`, and
  returns `None` for a room with no observed classifier row.
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
