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

logger = logging.getLogger(__name__)

#: Below this many letters a message carries no usable language signal.
#: Measured, `ok` tops out at `zu` 0.06 and `tak` at `mi` 0.08 -- noise at
#: noise-level confidence, which would read as a shift away from any window.
LETTER_FLOOR = 16
#: How many qualifying operator messages define "the conversation's language".
WINDOW_MESSAGES = 8
#: Per-message weight ceiling, so one long paste cannot define the window.
WEIGHT_CAP = 400
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
