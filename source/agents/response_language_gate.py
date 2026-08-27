"""Deterministic gate in front of the response-language classifier.

The classifier is the assistant's first model call on every turn, and on most
turns it re-confirms the language the previous turn already established. This
module answers the cheaper question — *has anything changed enough to be worth
asking* — from the messages alone.

It reads no settings and touches no database, so it is a pure function of its
input and testable without either. It also never decides which language to use:
that answer is the classifier's, and on a skip the caller reuses the room's
previous classification. A detector asked "what language is this" answers the
wrong question confidently on a request like `translate to english: <Danish>`;
asked "has this changed", it is well posed.
"""

from __future__ import annotations

import functools
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Sequence

logger = logging.getLogger(__name__)

#: Below this many letters a message carries no usable language signal.
#: Measured, `ok` tops out at `zu` 0.06 and `tak` at `mi` 0.08 -- noise at
#: noise-level confidence, which would read as a shift away from any window.
LETTER_FLOOR = 16
#: How many qualifying operator messages define "the conversation's language".
WINDOW_MESSAGES = 8
#: Per-message weight ceiling: a long message counts as several short ones,
#: never as the whole conversation. At this value a saturated window of
#: WINDOW_MESSAGES messages outweighs any single message, so one message is
#: at most a WINDOW_MESSAGES'th of a saturated window.
WEIGHT_CAP = 200
#: The request must carry at least this share of the window's language.
#: Measured, same-language requests score 0.23-1.00 and different-language
#: requests 0.00-0.05; this sits in that gap.
SHIFT_FLOOR = 0.15
#: Shorter tokens are not put to the CLDR name lookup -- `the`, `a` and `to`
#: all resolve to obscure language codes.
NAME_MIN_LETTERS = 4

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def _letter_count(text: str) -> int:
    return sum(1 for ch in text if unicodedata.category(ch).startswith("L"))


@dataclass(frozen=True)
class Detection:
    """What the detector made of one message.

    `below_floor` and `undetected` are deliberately separate: the first means
    nothing was asked of the detector, the second means it was asked and found
    nothing. Only the second is worth a model call.
    """

    letters: int
    below_floor: bool
    undetected: bool
    top: str | None
    confidence: dict[str, float] = field(default_factory=dict)

    def share(self, code: str | None) -> float:
        """This message's confidence in `code` -- the number the shift test
        compares. Absent means zero, which is the honest reading."""
        if not code:
            return 0.0
        return self.confidence.get(code, 0.0)


@functools.lru_cache(maxsize=1)
def _detector():
    """Built once per process, over every language lingua knows.

    Unrestricted on purpose. Restricting to the operator's declared languages
    is faster, but then a language nobody declared force-fits to one that is
    declared, and the gate skips a genuine shift. Unrestricted, an undeclared
    language scores near zero against any window and asks. Models load lazily,
    so resident memory settles around 141 MB rather than the gigabytes an eager
    build would cost.

    Importing lingua costs ~2.4s, and the gate ships switched off, so the
    import is here rather than at module level: an operator who never enables
    the gate never pays it, and the one who does pays it on the first gated
    turn -- a fifth of the call it replaces.
    """
    from lingua import LanguageDetectorBuilder

    return LanguageDetectorBuilder.from_all_languages().build()


@functools.lru_cache(maxsize=512)
def _detect_cached(text: str) -> Detection:
    """Memoised because the window re-reads the same history every turn, and a
    message's language cannot change after it is written. Without this a turn
    spends WINDOW_MESSAGES x ~10ms redetecting settled messages."""
    letters = _letter_count(text)
    if letters < LETTER_FLOOR:
        return Detection(
            letters=letters, below_floor=True, undetected=False, top=None)
    values = _detector().compute_language_confidence_values(text)
    confidence: dict[str, float] = {}
    for value in values:
        if value.value <= 0.0:
            continue
        code = value.language.iso_code_639_1.name.lower()
        confidence[code] = value.value
    if not confidence:
        return Detection(
            letters=letters, below_floor=False, undetected=True, top=None)
    top = max(confidence, key=lambda code: confidence[code])
    return Detection(
        letters=letters, below_floor=False, undetected=False, top=top,
        confidence=confidence)


def detect(text: str) -> Detection:
    """Language shares for one message, keyed by base subtag."""
    return _detect_cached(text or "")


def window_dominant(texts: Sequence[str]) -> tuple[str | None, int]:
    """The language the recent conversation has been running in.

    `texts` are the operator's messages, oldest first; the most recent
    WINDOW_MESSAGES qualifying ones are taken. Each contributes its whole
    confidence distribution rather than only its winning label, so a message
    split between two languages votes for both in proportion -- which is what
    keeps a Danish conversation reading as Danish when individual messages
    tip towards Norwegian.

    Weight is the letter count capped at WEIGHT_CAP: a long message says more
    about the conversation's language than a short one, but a pasted document
    counts as several messages rather than as the whole conversation -- a
    full window of ordinary messages still outweighs it.

    Returns (dominant base subtag, qualifying message count). A count of zero
    means the window said nothing -- distinct from a window that chose a
    language.
    """
    window: list[Detection] = []
    for text in reversed(list(texts)):
        detection = detect(text)
        if detection.below_floor or detection.undetected:
            continue
        window.append(detection)
        if len(window) >= WINDOW_MESSAGES:
            break
    if not window:
        return None, 0
    totals: dict[str, float] = {}
    for detection in window:
        weight = min(detection.letters, WEIGHT_CAP)
        for code, value in detection.confidence.items():
            totals[code] = totals.get(code, 0.0) + value * weight
    dominant = max(totals, key=lambda code: totals[code])
    return dominant, len(window)


@functools.lru_cache(maxsize=None)
def _recorded_names(code: str) -> frozenset[str]:
    """Every recorded CLDR name for `code`, casefolded."""
    from language_data.names import code_to_names

    return frozenset(name.casefold() for name in code_to_names(code).values())


@functools.lru_cache(maxsize=2048)
def _token_language(token: str) -> str | None:
    """The language `token` names, or None.

    Two filters, because the raw lookup is far too permissive to point at
    prose: measured, `the` resolves to `thx`, `a` to `auq`, `to` to `toz` and
    `second` to `cs`. Length removes the function words -- they are the short
    ones. The round-trip -- requiring the token to be among that code's own
    recorded names -- removes everything else, including `second`, which
    resolves to Czech but is not one of Czech's names.

    The round-trip reads the same CLDR data in both directions, so no table of
    ours can drift from it and no language is privileged over another. The
    returned code is whatever CLDR assigns the language -- two letters for
    languages with an ISO 639-1 tag, three otherwise (`chr` for Cherokee,
    `ceb` for Cebuano) -- so a language is not missed merely for lacking a
    two-letter tag.

    Names shorter than NAME_MIN_LETTERS letters are never recognised, even
    when they would round-trip: at a minimum of three, `the` resolves to `thx`
    and passes the round-trip too, so the floor cannot go that low. The
    bounded cost is real language names at or under three letters -- Ewe,
    Lao, Twi, Ido -- going unrecognised.
    """
    from language_data.names import name_to_code

    if len(token) < NAME_MIN_LETTERS:
        return None
    try:
        code = name_to_code("language", token, "und")
    except Exception:
        return None
    if not code:
        return None
    if token.casefold() not in _recorded_names(code):
        return None
    return code


def names_a_language(text: str) -> tuple[str, str] | None:
    """The first language `text` names, as (matched token, code).

    This is the one signal that sees an instruction like "answer in Danish
    from now on" -- written in the conversation's current language, so the
    detector reads no shift and the window reads no change.
    """
    for token in _WORD_RE.findall(text or ""):
        code = _token_language(token)
        if code:
            return token, code
    return None
