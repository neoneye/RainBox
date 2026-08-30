# Resolving the response language without a model

**Status:** Design. Nothing implemented.
**Date:** 2026-08-30
**Supersedes:** `2026-08-27-response-language-shift-gate-design.md`. That design
gates a model call on a detected language *shift*; this one resolves the reply
language outright and calls the model only where a deterministic answer would
be a guess. The earlier document's reading fixes are kept and named below; its
comparison machinery is not.

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

**Switching is mostly returning.** Of 19 recorded language switches, **17 (89%)
were back to a language the room had already used.** Under the shift gate every
one of those costs a model call, because reuse has a single slot per room.

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
**173 of 177** recorded classifications (98%). The four exceptions are not
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
language's* answer. This is what turns the measured 89% from a model call into a
lookup, and it is the single largest saving in this design.

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

A profile that declares no languages at all is the one case with nothing to go
on. It asks.

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
4. **Assuming a room has one language, or two.** A room may have several
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
