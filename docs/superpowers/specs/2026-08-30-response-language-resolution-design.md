# Resolving the response language without a model

**Status:** Design. Nothing implemented.
**Date:** 2026-08-30
**Supersedes:** `2026-08-27-response-language-shift-gate-design.md`. That design
gates a model call on a detected language *shift*; this one resolves the reply
language outright and calls the model only where a deterministic answer would
be a guess. The earlier document's reading fixes are kept and named below; its
comparison machinery is not.

## What is not known

The skip rate on representative traffic. There is no sample of the operator's
ordinary conversation in the trace: what is recorded is development work and
deliberate tests, both unrepresentative in opposite directions — the work is
monolingual English, the tests switch language almost every turn.

This matters for how the design is judged. On monolingual traffic the shift gate
it supersedes already skips nearly every turn, so the gain there is not fewer
model calls but fewer thresholds: four defects on the current branch came from
comparing confidences that are not comparable, and restricting the detector to a
room's own languages removes that whole class. The call-count gain is real only
to the extent that switching, cold starts and short messages are real, and those
frequencies are unknown.

Nothing here should be enabled on the strength of a projected saving. The
measurement to take is the one the parent proposal asked for and nobody has
taken: run it on ordinary traffic and count.

## The goal, stated as the operator did

> I don't want the LLM to be triggered in my typical interaction with the
> assistant.

`response_language_classifier` runs as the first activity of every assistant
turn and costs ~11s. The shift gate reduced that to "only when the language
changes" — which, for an operator who switches languages routinely, is close to
every turn. This design aims at *never*, for ordinary use, and says plainly
which cases are left over.

## What the measurements say

All figures below come from this installation's own trace and messages.

**Switching is mostly returning — in a sample that cannot carry the claim.**
Of 19 recorded language switches, 17 were back to a language the room had
already used. **Those switches are test messages**, written to exercise the
ReAct loop, not ordinary conversation; the operator's real traffic is primarily
American English with occasional Danish, and no representative sample of it
exists. The shape is still informative — a returning switch is cheap to serve
and a novel one is not — but the frequency is not, and this document must not
be read as projecting a saving from it. The parent proposal made the same
warning about its own 94.6% figure, for the same reason.

**Restricting the detector to a room's own languages makes confidence
comparable.** The same message, detected against all 75 languages and against a
two-language set:

| message | all languages | the room's set |
|---|---|---|
| `Hvordan går det.` | `da` 0.36, `nb` 0.33 | **`da` 1.00** |
| `explain the joke` | `en` 0.29, `tl` 0.07 | **`en` 0.96** |
| `Hjælp med at fixe dette problem: [Errno 61] Connection refused` | `da` 0.31, `nn` 0.25 | **`da` 0.88**, `en` 0.12 |

Most of the thresholds, ratios and normalisation in the superseded design exist
to cope with confidences that are not comparable between languages. That
incomparability is largely an artefact of making every message compete against
languages the room will never use.

The third row also settles a question raised while designing this: a message
mixing an instruction with a quoted foreign error is **not** ambiguous. It reads
as one language, correctly, and needs no model call.

**A deterministic answer matches the model.** Detecting the request and mapping
it to the matching declared profile row reproduces the model's own top answer on
**173 of 177** recorded classifications (98%). Unlike the switch figure this one
survives its sample: excluding every message that is itself a language
experiment leaves **170 of 173, still 98%**, so it is measured over ordinary
work and not over the language tests. The four exceptions are not
noise, and they define two of the rules below:

- three are an English sentence inside a Danish conversation, where the model
  answered Danish — it had conversation context and a detector does not;
- one is `¿En qué idioma estoy escribiendo ahora?` detected against a
  two-language set, which force-fit it to English where the model correctly
  said Spanish.

## The room's languages

A room's candidate languages are **derived on each turn, never stored**:

1. the base languages of the classifications that room's own `observed`
   classifier rows have resolved, and
2. the declared languages of the profiles of the room's human members.

The first is what the room has settled on in practice; the second stops a new
room being cold, and it is where a room of several people — a family, an office
— gets a candidate set none of its members would have alone.

Both sources are already in the database. This design adds no table, no
setting, and no operator-facing surface. It also names no language in shipped
code: the set is whatever those two sources contain.

## Two detectors, two questions

Restricting a detector to a candidate set makes it certain — including certain
about languages that are not in the set. That is the force-fit trap, and it is
how the Spanish case above failed. So detection asks two questions with two
instruments:

- **Unrestricted, over every language the detector knows** — *is this one of the
  room's languages at all?* This is the novelty check, and its answer may be
  low-confidence without harm, because it is only being asked for membership.
- **Restricted to the room's candidate set** — *which of them is it?* This is
  the answer used, and it is sharp.

When the unrestricted top language is outside the candidate set, the request is
in a language the room does not speak, and the classifier decides. Removing the
unrestricted detector as redundant would make every unknown language read as a
known one; it is load-bearing, not defensive.

## Recency

Each qualifying message votes for its language, and votes decay with age, so the
most recent message weighs most and older ones fade. This is carried over from
the superseded design and is the mechanism that answers a question the operator
raised and did not need to settle by hand: a single foreign sentence inside a
conversation does not move it, while a sustained switch does. The half-life is
the only knob controlling how many messages make a switch real.

It is also why the three "English sentence in a Danish conversation" exceptions
resolve the way the model resolved them: one English message does not outweigh
the Danish around it.

## Reuse is per language

The trace already records a classification on every `observed` classifier row.
"The room's last classification whose top language is X" is the existing query
with one added filter.

So a turn that resolves to a language the room has resolved before reuses *that
language's* answer rather than only the most recent one.

How much this saves depends on how often the operator switches, which is
unmeasured — see the warning on the switch figure above. It is included because
it is nearly free (one added filter on a query already made) and because it
removes a failure the shift gate has by construction: with a single reuse slot,
every return to a previous language costs a model call no matter how well
established that language is. Whether that failure is frequent or rare is a
different question from whether it should exist.

## Cold start

With no usable history and no clear language in the request, the reply uses the
profile's **preferred** language. `valid_profile_languages()` in
`source/user_profile/formatting.py` already defines that deterministically — a
`prefer` row sorts first, declaration order settles the rest — so this
introduces no new notion of preference and behaves sensibly when no row is
marked preferred or several are.

**This rule is scoped to the vacuum, and the scope is load-bearing.**
`format_formatting_guide` carries a deliberate decision in the opposite
direction: *the preferred language is NOT the output language; replies mirror
the conversation*, because a bare "prefer da" reads to a small model as an
instruction to switch. That decision stands wherever a conversation exists.
Widening the cold-start rule into a general preference would silently reverse
it.

In the same case the formatting guide's language line — *reply in the language
of the current message; never switch on your own. Use en-US or da only when the
message asks for it* — is suppressed. With no history and an unclear message it
points at something unknowable and forbids the fallback the operator asked for.
The precedence already resolves the conflict (`reply_language_markdown` is
source rank 3, `formatting_guide` rank 4), so this is for clarity rather than
correctness: a rank-4 instruction contradicting rank 3 is how small models are
made to wobble.

A profile that declares no languages at all still does not ask. Being made to
wait on a model for a question the model cannot answer any better than the code
can — there is no history and no language in the request, so it would be
guessing from the same nothing — is a poor trade for a case that is pure
guesswork either way.

The fallback is CLDR's likely language for whatever locale the profile does
carry. A profile with no languages but a country gets that country's language:

| profile locale | resolved |
|---|---|
| `DK` | `da` |
| `JP` | `ja` |
| `BR` | `pt` |
| `IN` | `hi` |
| nothing at all | `en` |

The last row is not a hardcoded English default. `Language.get("und").maximize()`
returns `en-Latn-US` — asked with no information, the Unicode consortium's own
data answers English. Reaching that answer through CLDR rather than through a
literal in our source keeps the rule the same shape as every other language
decision here, and means a profile that says anything at all about where its
owner is gets a better answer than English automatically.

This restores the `langcodes` dependency, which was dropped as unused during an
earlier cleanup; `language_data` alone does not carry likely-subtags.

This rule and trigger 3 divide the first turn between them, and the division is
worth stating because both could be read as owning it. A first request that
*carries* a language resolves to that language and asks, because there is no
classification yet to reuse. A first request that carries *no* language — an
`ok`, a bare URL, a stack trace — has nothing to resolve from and takes the
preferred language without asking. The distinction is whether the request says
anything about its own language, not whether the room is new.

## The rule, assembled

Run `response_language_classifier` when **any** of:

1. the request names a language (CLDR names and endonyms, unchanged);
2. the unrestricted detector's top language is outside the room's candidate set;
3. the resolved language carries language but has no recorded classification
   in this room yet — at most once per language per room (see the open question
   about sharing these across rooms);
4. the two detectors disagree about which candidate language it is;
5. the profile's declared languages have changed since the reused classification
   was recorded;
6. detection raised.

Otherwise resolve deterministically, by one mechanism rather than three
cases: **the recency-weighted dominant language over the room's messages,
counting the current request as the newest and heaviest vote.** The request
therefore weighs most without automatically winning, which is what makes a
single foreign sentence fail to move a conversation while a sustained switch
moves it. When no message in the room carries language at all — a new room whose
first request is `ok` — there is nothing to weigh, and the cold-start rule
below supplies the answer.

Every uncertainty resolves toward asking. A wrong ask costs latency; a wrong
resolution costs a reply in the wrong language, which is visible and corrects on
the next turn.

## What carries over, and what does not

**Kept**, because they are about reading a message correctly rather than
comparing languages across a wide field:

- quoted spans and code are excluded from detection, with the whole message read
  when nothing remains — a quote is what the operator is asking *about*, not
  what they are writing *in*;
- a message carries language if it has enough letters **or** the detector is
  sure of it, so a ten-character Chinese sentence is not treated as an
  acknowledgement;
- the language-name check exempts scripts that write a morpheme per character
  from its length minimums, and scans inside tokens for scripts that write
  without spaces;
- the profile-change snapshot that invalidates a reused classification.

**Dissolved**, because the restricted detector removes the problem they solve:
the fixed-size message window, the shift ratio against the request's strongest
candidate, and the per-message confidence normalisation.

## Traps

1. **Dropping the unrestricted detector.** It looks redundant beside a sharper
   restricted one. It is the only thing standing between an unknown language and
   a confident wrong answer.
2. **Widening the cold-start preference.** See above; it reverses a decision
   taken for a measured reason.
3. **Storing the room's language set.** It is derivable from two existing
   sources. A stored copy is a second source of truth that will drift from the
   messages, and the operator explicitly asked for it to stay internal.
4. **Reading the `en` fallback as an anglocentric default.** It is CLDR's
   answer for an unknown locale, not a choice made here, and it is reached only
   after the profile's declared languages and its locale have both said nothing.
   Replacing it with a literal would look identical in the common case and
   would stop a Danish or Japanese profile getting its own language.
5. **Assuming a room has one language, or two.** A room may have several
   members with disjoint languages. Nothing in the mechanism may assume a
   particular language, count, or script.

## Testing

- The measured cases above, as fixtures: the restricted/unrestricted split, the
  mixed instruction-plus-error message, and the four recorded disagreements.
- Per-language reuse: switching back to a language the room has resolved before
  makes no model call, and reuses that language's answer rather than the most
  recent one.
- Force-fit: a request in a language outside the candidate set asks, and does
  not resolve to a candidate.
- Cold start: no history and an unclear request resolves to the preferred
  profile language; a profile declaring no languages asks.
- Scoping: once a conversation exists, the preferred language does not override
  what the conversation is in.
- Fixtures use whichever languages a case is about, and at least one case uses a
  script that is neither Latin nor the operator's own, so a Latin assumption
  cannot pass unnoticed.

## Open questions

- **Should a language's classification be shared across rooms?** As written it
  is per room, so a new room pays one model call per language before it settles.
  For an operator who opens rooms often that is the dominant remaining cost, and
  the answer for "what does a Danish reply look like" plausibly does not depend
  on the room. Against: a room of several people is exactly where the answer
  might legitimately differ. Unresolved; the per-room form is the conservative
  starting point.
- **Does a per-language reuse expire?** A classification recorded months ago is
  still a fine answer for "what does a Danish reply look like", and the profile
  snapshot already covers the one thing that invalidates it. Left unexpired
  until something argues otherwise.
- **Do the room's resolved languages need a floor?** One accidental message in a
  language currently adds it to the candidate set permanently. Recency weighting
  makes it harmless for the *window*, but it also widens the restricted
  detector. Whether that matters is a measurement nobody has taken.
- **Where does the deterministic resolution get recorded?** It should appear on
  the classifier's step row like a skip does, so a run says how the language was
  decided. The row shape from the superseded design fits; the trigger vocabulary
  needs extending.
