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
