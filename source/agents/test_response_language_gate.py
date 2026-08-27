"""Tests for the deterministic gate in front of the response-language
classifier.

The gate answers one question — should the classifier run — from the messages
alone. It reads no settings and no database, so these tests need neither.
"""

from agents.response_language_gate import (
    LETTER_FLOOR,
    SHIFT_FLOOR,
    WINDOW_MESSAGES,
    detect,
    names_a_language,
    window_dominant,
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
    """One assertion per filter, so a regression says which one broke. The two-letter-code filter is load-bearing: EN_PROSE begins "The margin rule is dropped…" and is tested directly in test_ordinary_prose_names_no_language."""
    # Too short: resolves to `thx`, `auq`, `toz`.
    assert names_a_language("the a to") is None
    # Long enough and resolves, but to an obscure three-letter code (`mrt`).
    assert names_a_language("the margin rule is dropped") is None
    # Long enough and a two-letter code (`cs`), but `second` is not among
    # Czech's recorded names -- only the round-trip catches this one.
    assert names_a_language("the second run failed") is None
