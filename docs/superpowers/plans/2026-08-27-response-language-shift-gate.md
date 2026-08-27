# Response-Language Shift Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put a deterministic language-shift gate in front of
`response_language_classifier`, behind a default-off switch, so a turn whose
language has not changed reuses the room's last classification instead of
spending a model call.

**Architecture:** A new pure module, `source/agents/response_language_gate.py`,
detects language per message with lingua and answers one question — *should the
classifier run?* It reads no settings and touches no database, so it is testable
without either. `source/agents/assistant.py` reads the switch, calls the module,
and on a skip installs the room's previous classification and records a
`skipped` step row carrying the gate's reasoning. Detection gates; it never
decides what language to use.

**Tech Stack:** Python 3.14, Flask-SQLAlchemy, pytest,
`lingua-language-detector==2.2.0`, `langcodes` / `language_data` (CLDR names).

**Design spec:** `docs/superpowers/specs/2026-08-27-response-language-shift-gate-design.md`.
Read it before Task 1 — it carries the measurements the thresholds come from.

## Global Constraints

- Database safety: `source/conftest.py` forces pytest onto `rainbox_claude`, so
  the test path needs nothing. For any ad-hoc script or REPL poke, set
  `DATABASE_URL=postgresql+psycopg://localhost/rainbox_claude` explicitly.
  Never run experimental SQL against `rainbox_production`.
- Run everything from `source/` with that directory's venv:
  `cd source && ./venv/bin/python -m pytest ...`.
- Thresholds are module constants, never inline literals:
  `LETTER_FLOOR = 16`, `WINDOW_MESSAGES = 8`, `WEIGHT_CAP = 200`,
  `SHIFT_FLOOR = 0.15`, `NAME_MIN_LETTERS = 4`.
- Language codes compare on the base subtag only (`en`, not `en-US`).
- No hardcoded language tables anywhere. Language names come from CLDR through
  `language_data`. Danish and English appear in tests because they are the
  measured hard case, not because they are configured in shipped code.
- The gate fails open in every direction: any exception means run the
  classifier.
- Commit after every task. Never amend; each revision is its own commit.

---

### Task 1: Dependencies and the detector

**Files:**
- Modify: `source/requirements.txt`
- Create: `source/agents/response_language_gate.py`
- Test: `source/agents/test_response_language_gate.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Detection` (frozen dataclass with fields `letters: int`,
  `below_floor: bool`, `undetected: bool`, `top: str | None`,
  `confidence: dict[str, float]`), `detect(text: str) -> Detection`,
  and the constants `LETTER_FLOOR`, `WINDOW_MESSAGES`, `WEIGHT_CAP`,
  `SHIFT_FLOOR`, `NAME_MIN_LETTERS`.

- [ ] **Step 1: Add the dependencies**

Append to `source/requirements.txt`, keeping the file's existing ordering
convention:

```
lingua-language-detector==2.2.0
langcodes
language_data
```

- [ ] **Step 2: Install them**

Run: `cd source && ./venv/bin/pip install -r requirements.txt`
Expected: lingua-language-detector 2.2.0, langcodes and language_data installed.

Verify: `cd source && ./venv/bin/python -c "import lingua, langcodes, language_data; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Write the failing test**

Create `source/agents/test_response_language_gate.py`:

```python
"""Tests for the deterministic gate in front of the response-language
classifier.

The gate answers one question — should the classifier run — from the messages
alone. It reads no settings and no database, so these tests need neither.
"""

from agents.response_language_gate import (
    LETTER_FLOOR,
    SHIFT_FLOOR,
    detect,
)

# Measured cases from the design spec. The Danish ones avoid non-ASCII where
# the spelling allows it, so a terminal or editor cannot silently change what
# is under test.
EN_PROSE = (
    "The margin rule is dropped because the window already supplies the "
    "stability that restriction was buying."
)
EN_DEBUGGING = (
    "the test fails with a KeyError on the second run, but only when the "
    "cache is warm"
)
DA_PROSE = (
    "Jeg vil gerne have at du svarer pa dansk naar jeg skriver pa dansk "
    "til dig."
)
DA_WITH_ENGLISH_NOUNS = (
    "Kan du lige tjekke om den her classifier stadig kalder LLM'en paa "
    "hver eneste turn?"
)
DA_TRANSLATE_REQUEST = (
    "translate to english: Jeg vil gerne vide hvor lang tid det tager."
)
FI_PROSE = "Haluaisin etta vastaat minulle suomeksi tasta eteenpain, kiitos."


def test_detect_reports_the_share_of_each_language():
    """The shift test reads confidence in a NAMED language, so that number is
    what `detect` must expose — not merely the winning label."""
    english = detect(EN_PROSE)
    assert english.top == "en"
    assert english.confidence["en"] >= 0.9
    assert english.confidence.get("da", 0.0) < SHIFT_FLOOR

    danish = detect(DA_PROSE)
    assert danish.top == "da"
    assert danish.confidence["da"] >= SHIFT_FLOOR
    assert danish.confidence.get("en", 0.0) < SHIFT_FLOOR


def test_danish_with_english_nouns_keeps_its_danish_share():
    """The case the whole design turns on. The top label is unstable here --
    Danish confuses with Norwegian Bokmal -- but Danish's own share stays well
    clear of the floor, which is why the gate tests the share."""
    d = detect(DA_WITH_ENGLISH_NOUNS)
    assert d.confidence["da"] >= SHIFT_FLOOR
    assert d.confidence.get("en", 0.0) < SHIFT_FLOOR


def test_translate_request_reads_as_its_source_language():
    """Mostly Danish tokens asking for an English reply. The detector cannot
    know that, and must not pretend to: it reports Danish, which is a shift
    away from an English window and therefore asks the classifier."""
    d = detect(DA_TRANSLATE_REQUEST)
    assert d.confidence["da"] >= SHIFT_FLOOR
    assert d.confidence.get("en", 0.0) < SHIFT_FLOOR


def test_an_undeclared_language_is_reported_honestly():
    """The detector is not restricted to any profile's languages, so a language
    nobody declared is reported as itself rather than force-fitted to a
    declared one. Both English and Danish sit below the floor, so any window
    asks."""
    d = detect(FI_PROSE)
    assert d.top == "fi"
    assert d.confidence.get("en", 0.0) < SHIFT_FLOOR
    assert d.confidence.get("da", 0.0) < SHIFT_FLOOR


def test_a_low_confidence_top_is_still_a_usable_share():
    """Technical English scores far below prose in absolute terms. The floor
    has to sit under that, or ordinary debugging messages read as shifts."""
    d = detect(EN_DEBUGGING)
    assert d.top == "en"
    assert d.confidence["en"] >= SHIFT_FLOOR


def test_short_text_is_not_put_to_the_detector():
    """`ok` and `tak` carry no language content. Unrestricted the detector
    answers noise at noise-level confidence, which would read as a shift from
    any window, so they never reach it."""
    for text in ("ok", "tak", "ja"):
        d = detect(text)
        assert d.below_floor is True
        assert d.undetected is False
        assert d.top is None
        assert d.confidence == {}
        assert d.letters < LETTER_FLOOR


def test_below_floor_and_undetected_are_different_answers():
    """Being too short to classify is not the same as being unclassifiable.
    Only the second is worth a model call, so the two must not collapse."""
    short = detect("ok")
    assert short.below_floor is True and short.undetected is False

    unclassifiable = detect("1234567890 !!!!!!!!!! ---------- @@@@@@@@@@")
    assert unclassifiable.below_floor is True
    assert unclassifiable.letters == 0
```

- [ ] **Step 4: Run the test to verify it fails**

Run: `cd source && ./venv/bin/python -m pytest agents/test_response_language_gate.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agents.response_language_gate'`

- [ ] **Step 5: Write the module**

Create `source/agents/response_language_gate.py`:

```python
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
import time
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
#: Per-message weight ceiling. It bounds a long message's influence rather
#: than neutralising it: measured, a saturated window outvotes a single
#: 3560-letter paste at 200 but not at 400.
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
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `cd source && ./venv/bin/python -m pytest agents/test_response_language_gate.py -v`
Expected: PASS, 7 tests. The first test in the run pays a one-off ~2.4s lingua
import and a ~64ms cold detection; later ones are single-digit milliseconds.

- [ ] **Step 7: Commit**

```bash
git add source/requirements.txt source/agents/response_language_gate.py source/agents/test_response_language_gate.py
git commit -m "feat(assistant): detect a message's language shares without a model"
```

---

### Task 2: The window

**Files:**
- Modify: `source/agents/response_language_gate.py`
- Test: `source/agents/test_response_language_gate.py`

**Interfaces:**
- Consumes: `Detection`, `detect`, `LETTER_FLOOR`, `WINDOW_MESSAGES`,
  `WEIGHT_CAP` from Task 1.
- Produces: `window_dominant(texts: Sequence[str]) -> tuple[str | None, int]`,
  returning the dominant base subtag and how many messages qualified.

- [ ] **Step 1: Write the failing test**

Append to `source/agents/test_response_language_gate.py`:

```python
from agents.response_language_gate import WINDOW_MESSAGES, window_dominant


def test_window_dominant_reads_a_uniform_conversation():
    dominant, size = window_dominant([EN_PROSE, EN_DEBUGGING])
    assert dominant == "en"
    assert size == 2


def test_short_messages_do_not_enter_the_window():
    """An acknowledgement between two real messages neither counts nor votes."""
    dominant, size = window_dominant([DA_PROSE, "ok", DA_WITH_ENGLISH_NOUNS])
    assert dominant == "da"
    assert size == 2


def test_a_window_of_only_short_messages_is_empty():
    """Empty is a distinct answer, not a language. The caller asks the
    classifier rather than guessing from nothing."""
    dominant, size = window_dominant(["ok", "tak", "ja"])
    assert dominant is None
    assert size == 0


def test_the_window_keeps_only_the_most_recent_messages():
    """A language the operator left behind long ago must not outvote the one
    they are using now."""
    texts = [DA_PROSE] * 20 + [EN_PROSE] * WINDOW_MESSAGES
    dominant, size = window_dominant(texts)
    assert dominant == "en"
    assert size == WINDOW_MESSAGES


def test_a_long_message_cannot_single_handedly_define_the_window():
    """Weight is capped, so one long paste counts as several messages rather
    than as the whole conversation: a full window outvotes it."""
    long_english = EN_PROSE * 40
    danish = ([DA_PROSE, DA_WITH_ENGLISH_NOUNS] * WINDOW_MESSAGES)[
        :WINDOW_MESSAGES - 1]
    dominant, size = window_dominant([long_english, *danish])
    assert dominant == "da"
    assert size == WINDOW_MESSAGES
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd source && ./venv/bin/python -m pytest agents/test_response_language_gate.py -k window -v`
Expected: FAIL — `ImportError: cannot import name 'window_dominant'`

- [ ] **Step 3: Write the implementation**

Append to `source/agents/response_language_gate.py`:

```python
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
    must not outvote every real message around it.

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
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd source && ./venv/bin/python -m pytest agents/test_response_language_gate.py -v`
Expected: PASS, 12 tests.

- [ ] **Step 5: Commit**

```bash
git add source/agents/response_language_gate.py source/agents/test_response_language_gate.py
git commit -m "feat(assistant): read the conversation's language from a weighted window"
```

---

### Task 3: The language-mention check

**Files:**
- Modify: `source/agents/response_language_gate.py`
- Test: `source/agents/test_response_language_gate.py`

**Interfaces:**
- Consumes: `NAME_MIN_LETTERS`, `_WORD_RE` from Task 1.
- Produces: `names_a_language(text: str) -> tuple[str, str] | None`, returning
  the matched token and the two-letter code it resolved to.

**Why this task exists:** neither the detector nor the window can see
`Please answer in Danish from now on.` — it is English, in an English window,
and both would skip it. In the live sample, 8 of the 9 non-default
classifications were driven by an instruction naming a language, and 5 of those
were written in English. Without this check the gate silently ignores direct
instructions.

- [ ] **Step 1: Write the failing test**

Append to `source/agents/test_response_language_gate.py`:

```python
from agents.response_language_gate import names_a_language


def test_a_named_language_is_found_in_any_language():
    """CLDR carries names and endonyms for every language in every language, so
    the check works without a table of our own and without favouring English."""
    assert names_a_language("Please answer in Danish from now on.") == (
        "Danish", "da")
    assert names_a_language("Svar pa dansk fra nu af.") == ("dansk", "da")
    assert names_a_language("Voglio che tu risponda in italiano.") == (
        "italiano", "it")
    assert names_a_language("Bitte antworte auf Deutsch.") == ("Deutsch", "de")


def test_a_request_comparing_languages_is_found():
    """These are written in the conversation's own language, so nothing else in
    the gate can see them."""
    assert names_a_language(
        "Compare American and British English spelling.") == ("English", "en")
    assert names_a_language(
        "make a table of words in English, French and Spanish") is not None


def test_ordinary_prose_names_no_language():
    """The lookup over CLDR's full name set is far too permissive to point at
    raw text -- unfiltered, `the` resolves to `thx` and `a` to `auq`, and the
    gate would ask on every English sentence it ever saw."""
    for text in (EN_DEBUGGING, EN_PROSE, DA_WITH_ENGLISH_NOUNS,
                 "Add a setting so I can turn the gate on and off.",
                 "commit this to the branch and then write the plan"):
        assert names_a_language(text) is None


def test_each_filter_rejects_its_own_kind_of_false_match():
    """One assertion per filter, so a regression says which one broke."""
    # Too short: resolves to `thx`, `auq`, `toz`.
    assert names_a_language("the a to") is None
    # Long enough and resolves, but to an obscure three-letter code.
    assert names_a_language("Please respond in soc") is None
    # Long enough and a two-letter code (`cs`), but `second` is not among
    # Czech's recorded names -- only the round-trip catches this one.
    assert names_a_language("the second run failed") is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd source && ./venv/bin/python -m pytest agents/test_response_language_gate.py -k names_a_language -v`
Expected: FAIL — `ImportError: cannot import name 'names_a_language'`

- [ ] **Step 3: Write the implementation**

Append to `source/agents/response_language_gate.py`:

```python
@functools.lru_cache(maxsize=None)
def _recorded_names(code: str) -> frozenset[str]:
    """Every recorded CLDR name for `code`, casefolded."""
    from language_data.names import code_to_names

    return frozenset(name.casefold() for name in code_to_names(code).values())


@functools.lru_cache(maxsize=2048)
def _token_language(token: str) -> str | None:
    """The language `token` names, or None.

    Three filters, because the raw lookup is far too permissive to point at
    prose: measured, `the` resolves to `thx`, `a` to `auq`, `to` to `toz` and
    `second` to `cs`. Length removes the function words; a two-letter result
    removes the obscure codes; and the round-trip -- requiring the token to be
    among that code's own recorded names -- removes `second`, which resolves to
    Czech but is not one of Czech's names.

    The round-trip reads the same CLDR data in both directions, so no table of
    ours can drift from it and no language is privileged over another.
    """
    from language_data.names import name_to_code

    if len(token) < NAME_MIN_LETTERS:
        return None
    try:
        code = name_to_code("language", token, "und")
    except Exception:
        return None
    if not code or len(code) != 2:
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
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd source && ./venv/bin/python -m pytest agents/test_response_language_gate.py -v`
Expected: PASS, 16 tests.

- [ ] **Step 5: Commit**

```bash
git add source/agents/response_language_gate.py source/agents/test_response_language_gate.py
git commit -m "feat(assistant): recognise a request that names a language"
```

---

### Task 4: The decision

**Files:**
- Modify: `source/agents/response_language_gate.py`
- Test: `source/agents/test_response_language_gate.py`

**Interfaces:**
- Consumes: everything from Tasks 1-3.
- Produces: `GateDecision` (frozen dataclass with fields `should_ask: bool`,
  `trigger: str`, `window_dominant: str | None`, `window_size: int`,
  `window_share: float | None`, `request_top: str | None`,
  `request_letters: int`, `named_language: str | None`, `detector_ms: int`,
  `error: str | None`, and a method `as_args() -> dict`), plus
  `decide(*, window_texts: Sequence[str], request_text: str, has_previous: bool) -> GateDecision`
  and the trigger name constants
  `TRIGGER_NO_PREVIOUS`, `TRIGGER_NAMED_LANGUAGE`, `TRIGGER_SHIFT`,
  `TRIGGER_EMPTY_WINDOW`, `TRIGGER_DETECTOR_ERROR`, `TRIGGER_REUSE`.

- [ ] **Step 1: Write the failing test**

Append to `source/agents/test_response_language_gate.py`:

```python
from agents.response_language_gate import (
    TRIGGER_DETECTOR_ERROR,
    TRIGGER_EMPTY_WINDOW,
    TRIGGER_NAMED_LANGUAGE,
    TRIGGER_NO_PREVIOUS,
    TRIGGER_REUSE,
    TRIGGER_SHIFT,
    GateDecision,
    decide,
)

EN_WINDOW = [EN_PROSE, EN_DEBUGGING]
DA_WINDOW = [DA_PROSE, DA_WITH_ENGLISH_NOUNS]


def _decide(window, request, has_previous=True) -> GateDecision:
    return decide(
        window_texts=window, request_text=request, has_previous=has_previous)


def test_an_unchanged_conversation_reuses():
    d = _decide(EN_WINDOW, EN_DEBUGGING)
    assert d.should_ask is False
    assert d.trigger == TRIGGER_REUSE
    assert d.window_dominant == "en"
    assert d.window_share is not None and d.window_share >= SHIFT_FLOOR


def test_danish_technical_writing_in_a_danish_window_reuses():
    """The case that decides whether the feature is worth having. The top label
    is unstable across these messages; the Danish share is not."""
    d = _decide(DA_WINDOW, DA_WITH_ENGLISH_NOUNS)
    assert d.should_ask is False
    assert d.trigger == TRIGGER_REUSE


def test_a_change_of_language_asks():
    d = _decide(EN_WINDOW, DA_PROSE)
    assert d.should_ask is True
    assert d.trigger == TRIGGER_SHIFT
    assert d.window_dominant == "en"
    assert d.window_share is not None and d.window_share < SHIFT_FLOOR


def test_a_translate_request_asks():
    """Mostly Danish tokens asking for an English reply, in an English
    conversation. The detector reads Danish, which is a shift, so the
    classifier gets the question it is actually equipped to answer."""
    d = _decide(EN_WINDOW, DA_TRANSLATE_REQUEST)
    assert d.should_ask is True
    assert d.trigger == TRIGGER_SHIFT


def test_an_instruction_naming_a_language_asks():
    """English, in an English window: no shift, and the whole gate would skip
    it. This is the failure the gate is least allowed to make."""
    d = _decide(EN_WINDOW, "Please answer in Danish from now on.")
    assert d.should_ask is True
    assert d.trigger == TRIGGER_NAMED_LANGUAGE
    assert d.named_language == "Danish"


def test_an_acknowledgement_reuses():
    """`ok` has no language content. Asking a model what language it is in
    spends the turn's most expensive call on nothing."""
    d = _decide(EN_WINDOW, "ok")
    assert d.should_ask is False
    assert d.trigger == TRIGGER_REUSE
    assert d.window_share is None
    assert d.request_letters == 2


def test_a_short_request_naming_a_language_still_asks():
    """Below the letter floor, so the shift test never runs -- but the name
    check does not care about length."""
    d = _decide(EN_WINDOW, "in Danish please")
    assert d.should_ask is True
    assert d.trigger == TRIGGER_NAMED_LANGUAGE


def test_no_previous_classification_asks():
    """Nothing to reuse. This is the first turn in a room."""
    d = _decide(EN_WINDOW, EN_PROSE, has_previous=False)
    assert d.should_ask is True
    assert d.trigger == TRIGGER_NO_PREVIOUS


def test_an_empty_window_asks():
    d = _decide(["ok", "tak"], EN_PROSE)
    assert d.should_ask is True
    assert d.trigger == TRIGGER_EMPTY_WINDOW
    assert d.window_size == 0


def test_a_raising_detector_asks(monkeypatch):
    """A gate that cannot decide has decided to ask."""
    import agents.response_language_gate as gate

    def boom(_text):
        raise RuntimeError("detector unavailable")

    monkeypatch.setattr(gate, "detect", boom)
    d = _decide(EN_WINDOW, EN_PROSE)
    assert d.should_ask is True
    assert d.trigger == TRIGGER_DETECTOR_ERROR
    # The run says what broke rather than looking like an ordinary ask.
    assert "detector unavailable" in d.as_args()["error"]


def test_the_decision_serialises_the_number_it_was_tuned_on():
    """`window_share` is what the threshold is compared against, so every
    recorded row has to carry it or the operator cannot retune."""
    args = _decide(EN_WINDOW, EN_DEBUGGING).as_args()
    assert args["trigger"] == TRIGGER_REUSE
    assert args["should_ask"] is False
    assert args["window_dominant"] == "en"
    assert args["window_size"] == 2
    assert isinstance(args["window_share"], float)
    assert args["request_top"] == "en"
    assert args["named_language"] is None
    assert isinstance(args["detector_ms"], int)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd source && ./venv/bin/python -m pytest agents/test_response_language_gate.py -k decide -v`
Expected: FAIL — `ImportError: cannot import name 'decide'`

- [ ] **Step 3: Write the implementation**

Append to `source/agents/response_language_gate.py`:

```python
TRIGGER_NO_PREVIOUS = "no_previous"
TRIGGER_NAMED_LANGUAGE = "named_language"
TRIGGER_SHIFT = "shift"
TRIGGER_EMPTY_WINDOW = "empty_window"
TRIGGER_DETECTOR_ERROR = "detector_error"
TRIGGER_REUSE = "reuse"


@dataclass(frozen=True)
class GateDecision:
    """Why the classifier is about to run, or not.

    Recorded whole on the step row: a run that skipped its most expensive call
    has to say what it read and what it concluded, or the operator is left
    guessing at a reply in the wrong language.
    """

    should_ask: bool
    trigger: str
    window_dominant: str | None = None
    window_size: int = 0
    window_share: float | None = None
    request_top: str | None = None
    request_letters: int = 0
    named_language: str | None = None
    detector_ms: int = 0
    #: Set only on the fail-open path, so a run says what broke rather than
    #: silently looking like an ordinary ask.
    error: str | None = None

    def as_args(self) -> dict:
        args = {
            "should_ask": self.should_ask,
            "trigger": self.trigger,
            "window_dominant": self.window_dominant,
            "window_size": self.window_size,
            "window_share": self.window_share,
            "request_top": self.request_top,
            "request_letters": self.request_letters,
            "named_language": self.named_language,
            "detector_ms": self.detector_ms,
        }
        if self.error:
            args["error"] = self.error
        return args


def decide(
    *,
    window_texts: Sequence[str],
    request_text: str,
    has_previous: bool,
) -> GateDecision:
    """Should the response-language classifier run this turn?

    Every uncertainty resolves towards asking. A false ask costs one classifier
    call -- latency, never correctness. A false skip replies in the
    conversation's established language, which is degraded and visible rather
    than a hard error, and repairs itself on the next turn: the operator either
    writes in the other language, which is a shift, or names it, which is the
    name check.
    """
    started = time.perf_counter()

    def elapsed() -> int:
        return int((time.perf_counter() - started) * 1000)

    try:
        # Cheapest first, and independent of everything else: an instruction
        # naming a language is invisible to both the detector and the window,
        # because it is written in the conversation's current language.
        named = names_a_language(request_text)
        if named:
            return GateDecision(
                should_ask=True,
                trigger=TRIGGER_NAMED_LANGUAGE,
                named_language=named[0],
                detector_ms=elapsed(),
            )
        if not has_previous:
            return GateDecision(
                should_ask=True,
                trigger=TRIGGER_NO_PREVIOUS,
                detector_ms=elapsed(),
            )
        request = detect(request_text)
        dominant, size = window_dominant(window_texts)
        if dominant is None:
            return GateDecision(
                should_ask=True,
                trigger=TRIGGER_EMPTY_WINDOW,
                window_size=size,
                request_top=request.top,
                request_letters=request.letters,
                detector_ms=elapsed(),
            )
        if request.below_floor:
            # Too short to classify is not the same as unclassifiable. There is
            # nothing here to shift, and the previous resolution is the
            # deterministic answer.
            return GateDecision(
                should_ask=False,
                trigger=TRIGGER_REUSE,
                window_dominant=dominant,
                window_size=size,
                request_letters=request.letters,
                detector_ms=elapsed(),
            )
        if request.undetected:
            return GateDecision(
                should_ask=True,
                trigger=TRIGGER_SHIFT,
                window_dominant=dominant,
                window_size=size,
                window_share=0.0,
                request_letters=request.letters,
                detector_ms=elapsed(),
            )
        share = request.share(dominant)
        shifted = share < SHIFT_FLOOR
        return GateDecision(
            should_ask=shifted,
            trigger=TRIGGER_SHIFT if shifted else TRIGGER_REUSE,
            window_dominant=dominant,
            window_size=size,
            window_share=share,
            request_top=request.top,
            request_letters=request.letters,
            detector_ms=elapsed(),
        )
    except Exception as exc:
        logger.warning(
            "response-language gate failed open", exc_info=True)
        return GateDecision(
            should_ask=True,
            trigger=TRIGGER_DETECTOR_ERROR,
            detector_ms=elapsed(),
            error=f"{type(exc).__name__}: {exc}",
        )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd source && ./venv/bin/python -m pytest agents/test_response_language_gate.py -v`
Expected: PASS, 27 tests.

- [ ] **Step 5: Commit**

```bash
git add source/agents/response_language_gate.py source/agents/test_response_language_gate.py
git commit -m "feat(assistant): decide whether the response-language classifier needs to run"
```

---

### Task 5: The switch

**Files:**
- Modify: `source/db/settings.py:216-231`
- Modify: `source/agents/assistant.py` — `_declared_block_switches`
  (around line 4367) and `_build_turn_log` (around line 4385)
- Test: `source/agents/test_assistant_response_language_gate.py` (create)

**Interfaces:**
- Consumes: nothing from Tasks 1-4.
- Produces: the setting key `assistant.response_language_gate`, and
  `AssistantAgent._response_language_gate_enabled() -> bool`.

- [ ] **Step 1: Write the failing test**

Create `source/agents/test_assistant_response_language_gate.py`:

```python
"""Tests for the assistant's side of the response-language gate: the switch,
the previous-classification read, and the skipped step row.
"""

from uuid import uuid4

import pytest

import db
from agents.assistant import AssistantAgent
from agents.config import ASSISTANT_UUID


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    yield app
    db.db.session.rollback()
    ctx.pop()


def _agent() -> AssistantAgent:
    return AssistantAgent(
        agent_uuid=ASSISTANT_UUID, name="assistant", send=lambda _: None)


def test_the_gate_is_registered_and_defaults_off(app_ctx):
    """Default off: the gate ships dormant and the operator turns it on when
    they want to compare runs."""
    assert "assistant.response_language_gate" in db.SETTINGS
    setting = db.SETTINGS["assistant.response_language_gate"]
    assert setting.kind == "bool"
    assert setting.default is False


def test_the_switch_reads_off_when_unset(app_ctx):
    db.set_setting("assistant.response_language_gate", False)
    assert _agent()._response_language_gate_enabled() is False


def test_the_switch_reads_on_when_set(app_ctx):
    db.set_setting("assistant.response_language_gate", True)
    assert _agent()._response_language_gate_enabled() is True


def test_an_unreadable_switch_reads_off(app_ctx, monkeypatch):
    """Off means the classifier runs, which is today's behaviour. A switch that
    cannot be read must not silently start skipping model calls."""
    def boom(_key):
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(db, "get_setting", boom)
    assert _agent()._response_language_gate_enabled() is False
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd source && ./venv/bin/python -m pytest agents/test_assistant_response_language_gate.py -v`
Expected: FAIL — `assert 'assistant.response_language_gate' in db.SETTINGS`

- [ ] **Step 3: Register the setting**

In `source/db/settings.py`, after the `assistant.knowledge_calibration` entry
and before `profile.current_changed_at`, add:

```python
    "assistant.response_language_gate": Setting(
        "assistant.response_language_gate", None, "bool", False,
        description="Skip the response-language classifier on a turn whose "
                    "language has not changed, reusing the room's last "
                    "classification instead. Default off: on, the classifier's "
                    "trace row goes from a model call to a sub-second gate "
                    "decision, so turning it off and on again compares the two "
                    "directly. A turn that names a language, changes language, "
                    "or has nothing to reuse still asks.",
    ),
```

- [ ] **Step 4: Add the switch reader**

In `source/agents/assistant.py`, directly after `_declared_block_switches`, add:

```python
    def _response_language_gate_enabled(self) -> bool:
        """Whether the response-language classifier may be skipped this turn.

        Best-effort, and an unreadable switch reads as off — off means the
        classifier runs, which is the behaviour that was already correct. A
        settings failure must never start skipping model calls on its own."""
        try:
            return bool(db.get_setting("assistant.response_language_gate"))
        except Exception:
            logger.warning(
                "assistant: response-language gate switch read failed",
                exc_info=True)
            return False
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `cd source && ./venv/bin/python -m pytest agents/test_assistant_response_language_gate.py -v`
Expected: PASS, 4 tests.

- [ ] **Step 6: Put the switch state on every step row this turn**

The turn log answers "the first questions when troubleshooting a weird reply",
and a reply in the wrong language is exactly that question.

In `source/agents/assistant.py:4385`, give `_build_turn_log` the parameter and
the entry:

```python
    @staticmethod
    def _build_turn_log(
        context: "user_profile.ProfileContext",
        formatting_enabled: bool, calibration_enabled: bool,
        persona: "db.PersonaResolution | None",
        response_language_gate_enabled: bool,
    ) -> list[dict[str, Any]]:
```

and after the `knowledge_calibration` entry at the end of the method body:

```python
        entries.append({"label": "response_language_gate",
                        "text": "on" if response_language_gate_enabled else "off"})
        return entries
```

There are **three** call sites; all must pass the new argument.

`source/agents/assistant.py:3727` (the live turn):

```python
            self._turn_log = self._build_turn_log(
                context, formatting_on, calibration_on, self._persona,
                self._response_language_gate_enabled())
```

`source/agents/assistant.py:6029` — read the surrounding lines for which
switches that path already has in scope, and pass
`self._response_language_gate_enabled()` as the fifth argument in the same
style.

`source/agents/test_assistant_persona.py:79` constructs the log directly. Add
`False` as the fifth argument: that test is about persona entries, and the gate
switch is not what it is asserting.

- [ ] **Step 7: Run the assistant suite for regressions**

Run: `cd source && ./venv/bin/python -m pytest agents/test_assistant.py agents/test_assistant_persona.py agents/test_assistant_response_language_gate.py agents/test_response_language_classifier.py -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add source/db/settings.py source/agents/assistant.py source/agents/test_assistant_response_language_gate.py
git commit -m "feat(assistant): add the response-language gate switch, default off"
```

---

### Task 6: Reading the room's previous classification

**Files:**
- Modify: `source/agents/assistant.py` — new method beside
  `_request_response_language_classification` (around line 5232)
- Test: `source/agents/test_assistant_response_language_gate.py`

**Interfaces:**
- Consumes: `ResponseLanguageClassification` (already in `agents/assistant.py`).
- Produces: `AssistantAgent._previous_room_classification(room_uuid: UUID) -> ResponseLanguageClassification | None`.

**Why a read and not new state:** the resolved language is already in the step
trace. Adding a column for it would create a second source of truth for the
same fact, and they would drift.

- [ ] **Step 1: Write the failing test**

Append to `source/agents/test_assistant_response_language_gate.py`:

```python
from agents.assistant import (
    ResponseLanguageClassification,
    ResponseLanguageItem,
)


@pytest.fixture
def room(app_ctx):
    human = db.get_human_user()
    assert human is not None
    chatroom = db.create_chatroom(
        f"language-gate-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])
    try:
        yield chatroom
    finally:
        db.db.session.rollback()
        db.db.session.query(db.AssistantRun).filter(
            db.AssistantRun.room_uuid == chatroom.uuid).delete()
        db.db.session.query(db.Chatroom).filter(
            db.Chatroom.uuid == chatroom.uuid).delete()
        db.db.session.commit()


def _record_classification(room_uuid, classification, phase="observed"):
    """Write a classifier step row the way a real turn writes one."""
    import json

    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room_uuid, agent_uuid=ASSISTANT_UUID)
    db.append_assistant_step(
        run_uuid=run.uuid,
        step_index=0,
        phase=phase,
        action="response_language_classifier",
        reason=classification.reason,
        observation_preview=json.dumps(
            classification.model_dump(), ensure_ascii=False, indent=1),
        code_driven=True,
    )
    db.db.session.commit()
    return run


def test_no_previous_classification_in_a_fresh_room(room):
    assert _agent()._previous_room_classification(room.uuid) is None


def test_the_previous_classification_round_trips(room):
    original = ResponseLanguageClassification(
        reason="The conversation is running in Danish.",
        languages=[
            ResponseLanguageItem(code="da", score=5),
            ResponseLanguageItem(code="en-GB", score=2),
        ],
    )
    _record_classification(room.uuid, original)

    recovered = _agent()._previous_room_classification(room.uuid)
    assert recovered is not None
    assert [item.code for item in recovered.languages] == ["da", "en-GB"]
    assert recovered.reason == original.reason


def test_the_most_recent_observed_classification_wins(room):
    _record_classification(room.uuid, ResponseLanguageClassification(
        reason="English.",
        languages=[ResponseLanguageItem(code="en-GB", score=5)]))
    _record_classification(room.uuid, ResponseLanguageClassification(
        reason="Danish now.",
        languages=[ResponseLanguageItem(code="da", score=5)]))

    recovered = _agent()._previous_room_classification(room.uuid)
    assert recovered is not None
    assert recovered.languages[0].code == "da"


def test_a_skipped_or_failed_row_is_not_a_previous_classification(room):
    """A row with no result is not a resolution. Reusing one would reply in
    whatever language a failed call happened to leave behind."""
    _record_classification(
        room.uuid,
        ResponseLanguageClassification(
            reason="never ran",
            languages=[ResponseLanguageItem(code="en-GB", score=5)]),
        phase="failed")
    assert _agent()._previous_room_classification(room.uuid) is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd source && ./venv/bin/python -m pytest agents/test_assistant_response_language_gate.py -k previous -v`
Expected: FAIL — `AttributeError: 'AssistantAgent' object has no attribute '_previous_room_classification'`

- [ ] **Step 3: Write the implementation**

In `source/agents/assistant.py`, add beside
`_request_response_language_classification`:

```python
    def _previous_room_classification(
        self, room_uuid: "UUID"
    ) -> ResponseLanguageClassification | None:
        """The last language this room resolved, read back from the trace.

        This is what a skipped turn reuses. It is a read rather than stored
        state on purpose: the resolved language is already recorded on the
        classifier's step row, and a second copy would be a second source of
        truth for the same fact.

        Only an `observed` row counts. A skipped or failed classifier produced
        no resolution, and reusing one would reply in whatever language a
        broken call happened to leave behind. A row that cannot be parsed back
        into the schema is treated as absent, which asks.
        """
        try:
            row = (
                db.db.session.query(db.AssistantStep)
                .join(db.AssistantRun,
                      db.AssistantStep.run_uuid == db.AssistantRun.uuid)
                .filter(db.AssistantRun.room_uuid == room_uuid)
                .filter(db.AssistantStep.action
                        == self.RESPONSE_LANGUAGE_CLASSIFIER_ACTION)
                .filter(db.AssistantStep.phase == "observed")
                .filter(db.AssistantStep.observation_preview.isnot(None))
                .order_by(db.AssistantStep.id.desc())
                .first()
            )
        except Exception:
            logger.warning(
                "assistant: previous response-language read failed",
                exc_info=True)
            return None
        if row is None:
            return None
        try:
            return ResponseLanguageClassification.model_validate_json(
                row.observation_preview)
        except Exception:
            logger.warning(
                "assistant: previous response-language row did not parse",
                exc_info=True)
            return None
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd source && ./venv/bin/python -m pytest agents/test_assistant_response_language_gate.py -v`
Expected: PASS, 8 tests.

- [ ] **Step 5: Commit**

```bash
git add source/agents/assistant.py source/agents/test_assistant_response_language_gate.py
git commit -m "feat(assistant): read a room's last resolved response language from the trace"
```

---

### Task 7: Wiring the gate into the turn

**Files:**
- Modify: `source/agents/assistant.py` — `_run_response_language_classifier`
  (around line 5300)
- Test: `source/agents/test_assistant_response_language_gate.py`

**Interfaces:**
- Consumes: `decide`, `GateDecision`, the trigger constants (Task 4);
  `_response_language_gate_enabled` (Task 5);
  `_previous_room_classification` (Task 6).
- Produces: the skip path, and `AssistantAgent._response_language_gate_args`
  (a `dict | None` holding the turn's gate decision for the step row).

- [ ] **Step 1: Write the failing test**

Append to `source/agents/test_assistant_response_language_gate.py`:

```python
DA_MESSAGES = [
    {"sender_type": "human",
     "text": "Jeg vil gerne have at du svarer pa dansk naar jeg skriver "
             "pa dansk til dig."},
    {"sender_type": "agent",
     "text": "Selvfolgelig, jeg svarer pa dansk fra nu af."},
    {"sender_type": "human",
     "text": "Kan du lige tjekke om den her classifier stadig kalder "
             "LLM'en paa hver eneste turn?"},
]


def test_the_gate_does_nothing_when_the_switch_is_off(room, monkeypatch):
    """Off is today's behaviour: the classifier runs and the gate is never
    consulted."""
    import agents.response_language_gate as gate

    db.set_setting("assistant.response_language_gate", False)
    called = []
    monkeypatch.setattr(gate, "decide", lambda **kw: called.append(kw))

    agent = _agent()
    assert agent._response_language_gate_decision(
        DA_MESSAGES, room.uuid) is None
    assert called == [], "the gate must not be consulted while the switch is off"


def test_an_unchanged_danish_conversation_skips(room):
    """The window is the operator's Danish messages; the request is more
    Danish. Nothing changed, so nothing is asked."""
    db.set_setting("assistant.response_language_gate", True)
    _record_classification(room.uuid, ResponseLanguageClassification(
        reason="Danish conversation.",
        languages=[ResponseLanguageItem(code="da", score=5)]))

    decision, previous = _agent()._response_language_gate_decision(
        DA_MESSAGES, room.uuid)
    assert decision.should_ask is False
    assert decision.window_dominant == "da"
    assert previous is not None


def test_the_assistant_replies_are_not_in_the_window(room):
    """A reply is written in whatever language a previous resolution chose. If
    replies voted, one wrong resolution would keep justifying itself."""
    db.set_setting("assistant.response_language_gate", True)
    _record_classification(room.uuid, ResponseLanguageClassification(
        reason="English conversation.",
        languages=[ResponseLanguageItem(code="en-GB", score=5)]))
    messages = [
        {"sender_type": "human", "text": DA_MESSAGES[0]["text"]},
        {"sender_type": "agent",
         "text": "The window must not count this English reply as evidence "
                 "about what language the operator is writing in."},
        {"sender_type": "human", "text": DA_MESSAGES[2]["text"]},
    ]
    decision, _ = _agent()._response_language_gate_decision(
        messages, room.uuid)
    assert decision.window_dominant == "da"


def test_the_current_request_is_not_part_of_its_own_window(room):
    """The last human message is the request being judged. Counting it in the
    window it is compared against makes every turn look unchanged."""
    db.set_setting("assistant.response_language_gate", True)
    _record_classification(room.uuid, ResponseLanguageClassification(
        reason="English conversation.",
        languages=[ResponseLanguageItem(code="en-GB", score=5)]))
    messages = [
        {"sender_type": "human", "text": EN_LONG},
        {"sender_type": "human", "text": DA_MESSAGES[0]["text"]},
    ]
    decision, _ = _agent()._response_language_gate_decision(
        messages, room.uuid)
    assert decision.window_dominant == "en"
    assert decision.should_ask is True


def test_a_room_with_no_previous_classification_asks(room):
    db.set_setting("assistant.response_language_gate", True)
    decision, previous = _agent()._response_language_gate_decision(
        DA_MESSAGES, room.uuid)
    assert decision.should_ask is True
    assert decision.trigger == "no_previous"
    assert previous is None
```

Add near the top of the file, beside `DA_MESSAGES`:

```python
EN_LONG = (
    "The margin rule is dropped because the window already supplies the "
    "stability that restriction was buying, and the letter floor keeps "
    "acknowledgements out of the comparison entirely."
)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd source && ./venv/bin/python -m pytest agents/test_assistant_response_language_gate.py -k gate_decision -v`
Expected: FAIL — `AttributeError: 'AssistantAgent' object has no attribute '_response_language_gate_decision'`

- [ ] **Step 3: Write the decision helper**

In `source/agents/assistant.py`, add before `_run_response_language_classifier`:

```python
    def _response_language_gate_decision(
        self, messages: list[dict[str, Any]], room_uuid: "UUID"
    ) -> tuple[
        response_language_gate.GateDecision,
        ResponseLanguageClassification | None,
    ] | None:
        """The gate's verdict for this turn and what a skip would reuse, or
        None when the gate does not apply.

        The previous classification is returned alongside the verdict because
        the verdict depends on whether one exists: reading it twice would put
        two queries on the turn's critical path to answer one question.

        None means "run the classifier as always": the switch is off, or the
        turn has no room to read a previous resolution from. The gate itself
        never returns None — it always has a verdict, and every uncertainty in
        it resolves towards asking.

        The window is the operator's earlier messages only. The final human
        message is the request being judged, so it is excluded from the window
        it is compared against; assistant replies are excluded because they are
        written in whatever language a previous resolution chose, and letting
        them vote would let one wrong resolution justify itself forever.
        """
        if not self._response_language_gate_enabled():
            return None
        human_texts = [
            str(m.get("text") or "")
            for m in messages
            if self._message_role(m) == "user"
        ]
        if not human_texts:
            return None
        previous = self._previous_room_classification(room_uuid)
        decision = response_language_gate.decide(
            window_texts=human_texts[:-1],
            request_text=human_texts[-1],
            has_previous=previous is not None,
        )
        return decision, previous
```

`agents/assistant.py` has no `TYPE_CHECKING` block, so add a plain top-level
import beside the module's other `agents.*` imports:

```python
from agents import response_language_gate
```

This is cheap: `response_language_gate` imports lingua lazily inside its
detector builder, so importing the module costs nothing until the gate actually
runs.

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd source && ./venv/bin/python -m pytest agents/test_assistant_response_language_gate.py -v`
Expected: PASS, 13 tests.

- [ ] **Step 5: Write the failing test for the skip path**

Append to `source/agents/test_assistant_response_language_gate.py`:

```python
def test_a_skipped_turn_records_what_it_reused(room, monkeypatch):
    """The row is the whole point: the operator reads runs, so a turn that
    skipped its most expensive call has to say what it read, what it concluded,
    and which language it proceeded in."""
    db.set_setting("assistant.response_language_gate", True)
    _record_classification(room.uuid, ResponseLanguageClassification(
        reason="Danish conversation.",
        languages=[ResponseLanguageItem(code="da", score=5)]))

    agent = _agent()
    agent._run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=ASSISTANT_UUID)

    def fail(*a, **kw):
        raise AssertionError("the classifier must not run on a skip")

    monkeypatch.setattr(
        agent, "_request_response_language_classification", fail)

    agent._run_response_language_classifier(
        step_index=0, messages=DA_MESSAGES, profile=None)
    db.db.session.commit()

    row = (
        db.db.session.query(db.AssistantStep)
        .filter(db.AssistantStep.run_uuid == agent._run.uuid)
        .order_by(db.AssistantStep.id.desc())
        .first()
    )
    assert row.phase == "skipped"
    assert row.action == "response_language_classifier"
    # The language it proceeded in, not merely that it declined to ask.
    assert "da" in (row.observation_preview or "")
    # The gate's reasoning, on the row, for the operator reading the run.
    assert row.args["gate"]["should_ask"] is False
    assert row.args["gate"]["window_dominant"] == "da"
    assert isinstance(row.args["gate"]["window_share"], float)
    # A gate decision is not a model call: no prompts, and a gate-scale
    # duration. This is the before/after the switch exists to show.
    assert row.system_prompt is None
    assert row.user_prompt is None
    assert row.model_uuid is None
    assert row.duration_ms is not None and row.duration_ms < 1000
    # The turn proceeds in the reused language.
    assert agent._response_language_classification is not None
    assert agent._response_language_classification.languages[0].code == "da"
    assert "da" in agent._reply_language_markdown


def test_an_asking_turn_records_why_it_asked(room, monkeypatch):
    """The decision goes on the row on both paths, so a run always says why
    the classifier ran or did not."""
    db.set_setting("assistant.response_language_gate", True)
    _record_classification(room.uuid, ResponseLanguageClassification(
        reason="English conversation.",
        languages=[ResponseLanguageItem(code="en-GB", score=5)]))

    agent = _agent()
    agent._run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=ASSISTANT_UUID)
    monkeypatch.setattr(
        agent, "_request_response_language_classification",
        lambda **kw: ResponseLanguageClassification(
            reason="Switched to Danish.",
            languages=[ResponseLanguageItem(code="da", score=5)]))

    messages = [
        {"sender_type": "human", "text": EN_LONG},
        {"sender_type": "human", "text": DA_MESSAGES[0]["text"]},
    ]
    agent._run_response_language_classifier(
        step_index=0, messages=messages, profile=None)
    db.db.session.commit()

    row = (
        db.db.session.query(db.AssistantStep)
        .filter(db.AssistantStep.run_uuid == agent._run.uuid)
        .order_by(db.AssistantStep.id.desc())
        .first()
    )
    assert row.phase == "observed"
    assert row.args["gate"]["should_ask"] is True
    assert row.args["gate"]["trigger"] == "shift"


def test_a_skip_needs_a_previous_classification_to_reuse(room, monkeypatch):
    """If the read comes back empty the gate has already decided to ask, so
    the classifier runs. A skip can never reach the reply with no language."""
    db.set_setting("assistant.response_language_gate", True)
    agent = _agent()
    agent._run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=ASSISTANT_UUID)
    ran = []
    monkeypatch.setattr(
        agent, "_request_response_language_classification",
        lambda **kw: ran.append(True) or ResponseLanguageClassification(
            reason="First turn.",
            languages=[ResponseLanguageItem(code="da", score=5)]))

    agent._run_response_language_classifier(
        step_index=0, messages=DA_MESSAGES, profile=None)
    assert ran == [True]
```

- [ ] **Step 6: Run the test to verify it fails**

Run: `cd source && ./venv/bin/python -m pytest agents/test_assistant_response_language_gate.py -k recorded -v`
Expected: FAIL — the classifier runs, so the `fail` monkeypatch raises
`AssertionError: the classifier must not run on a skip`.

- [ ] **Step 7: Wire the skip into the classifier method**

In `source/agents/assistant.py`, at the top of
`_run_response_language_classifier`, before the prompts are built, insert:

```python
        # The gate decides before anything is built: a skip costs neither the
        # model call nor the prompt assembly. It applies only to a real turn —
        # the eval harness runs with `self._run` None and has no room to read a
        # previous resolution from.
        self._response_language_gate_args = None
        verdict = None
        if self._run is not None:
            verdict = self._response_language_gate_decision(
                messages, self._run.room_uuid)
        if verdict is not None:
            decision, previous = verdict
            self._response_language_gate_args = {"gate": decision.as_args()}
            # `previous is None` cannot reach here with should_ask False — the
            # gate returns TRIGGER_NO_PREVIOUS in that case — but the reply
            # must never proceed with no language, so the guard is explicit
            # rather than inferred.
            if not decision.should_ask and previous is not None:
                self._response_language_classification = previous
                self._reply_language_markdown = (
                    self._format_reply_language_markdown(previous))
                self._response_language_classifier_meta = {
                    "duration_ms": decision.detector_ms,
                }
                self._record_response_language_classifier_step(
                    step_index=step_index,
                    phase="skipped",
                    reason=(
                        "the conversation's language has not changed; reusing "
                        "this room's last classification"
                    ),
                    observation_preview=json.dumps(
                        previous.model_dump(), ensure_ascii=False, indent=1),
                    system_prompt=None,
                    user_prompt=None,
                    requested_at=datetime.now(UTC),
                )
                return
```

`_record_response_language_classifier_step` currently types `system_prompt` and
`user_prompt` as `str`; widen both to `str | None`, and add the gate args to its
`db.append_assistant_step` call:

```python
            args=self._response_language_gate_args,
```

Initialise the attribute beside the other classifier state in `__init__`
(around line 3540):

```python
        self._response_language_gate_args: dict[str, Any] | None = None
```

and reset it beside `self._response_language_classifier_meta = {}` at both
reset sites (around lines 3749 and 4538).

- [ ] **Step 8: Run the test to verify it passes**

Run: `cd source && ./venv/bin/python -m pytest agents/test_assistant_response_language_gate.py -v`
Expected: PASS, 16 tests.

- [ ] **Step 9: Run the full assistant suite for regressions**

Run: `cd source && ./venv/bin/python -m pytest agents/ webapp/test_assistant_log_view.py db/test_assistant_trace.py -q`
Expected: PASS. The switch defaults off, so every existing test sees today's
behaviour. Any failure here is a real regression, not a fixture that needs
relaxing.

- [ ] **Step 10: Commit**

```bash
git add source/agents/assistant.py source/agents/test_assistant_response_language_gate.py
git commit -m "feat(assistant): skip the response-language classifier when nothing changed"
```

---

### Task 8: Documentation

**Files:**
- Modify: `source/notes/assistant-design.md`
- Modify: `source/notes/proposals/2026-08-17-gating-the-response-language-classifier.md`

- [ ] **Step 1: Describe the gate in the design notes**

Read `source/notes/assistant-design.md`, find where it describes
`response_language_classifier`, and add a passage after it carrying these
points, rewritten in that file's own voice and heading style:

- The classifier runs behind a gate, switched by
  `assistant.response_language_gate`, default off.
- The gate detects each operator message's language shares with lingua and
  compares the request against a weighted window of the preceding operator
  messages. Assistant replies are excluded: a reply is written in whatever
  language a previous resolution chose, so letting replies vote would let one
  wrong resolution justify itself.
- It asks the classifier when the request names a language, when the request
  carries too little of the window's language, when there is nothing to reuse,
  when the window is empty, or when the detector fails. Otherwise it reuses the
  room's last observed classification, read back from the step trace.
- Detection gates but never decides: the detected language is compared, never
  adopted.
- A skipped turn records a `skipped` classifier row carrying the gate's
  decision, the reused classification, and a gate-scale duration.

Describe how it works now — no migration notes, no "previously", no reference
to this plan or to the switch's default changing. Git holds the history.

- [ ] **Step 2: Mark the proposal as implemented**

The proposal's `Status:` line says "Proposed. Nothing implemented." Change it to
record that the gate shipped behind a default-off switch in a shape the spec
documents, and point at
`docs/superpowers/specs/2026-08-27-response-language-shift-gate-design.md`.
Leave the proposal's analysis alone — it is the operator's document, and its
measurements are still the reasoning behind the thresholds.

- [ ] **Step 3: Commit**

```bash
git add source/notes/
git commit -m "docs(assistant): describe the response-language gate"
```

---

## After the plan

The switch is off. Two things remain, both the operator's to do and neither
part of this plan:

1. **Bind `assistant.response_language_classifier` to a small model** on
   `/agentmodel`. It currently resolves to `assistant.default`, which is why a
   2280-in/81-out call takes 11.4s. This is worth more than the gate, and it
   changes what the gate is worth — judge the gate against the bound number.
2. **Turn the gate on and read some runs.** The classifier's row goes from a
   9-18s model call to a sub-second gate decision carrying its reasoning. The
   thresholds are starting values; `window_share` is on every row so they can
   be retuned from real traffic.
