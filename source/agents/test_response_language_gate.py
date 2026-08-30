"""Tests for the deterministic gate in front of the response-language
classifier.

The gate answers one question — should the classifier run — from the messages
alone. It reads no settings and no database, so these tests need neither.
"""

from agents.response_language_gate import (
    Detection,
    GateDecision,
    LANGUAGE_SLOTS,
    LETTER_FLOOR,
    SHIFT_RATIO,
    TRIGGER_DETECTOR_ERROR,
    TRIGGER_EMPTY_WINDOW,
    TRIGGER_NAMED_LANGUAGE,
    TRIGGER_NO_PREVIOUS,
    TRIGGER_PROFILE_CHANGED,
    TRIGGER_REUSE,
    TRIGGER_SHIFT,
    decide,
    detect,
    language_slots,
    names_a_language,
)

#: A language either shows up in a message's shares or it does not; these
#: detection tests only need a bound that tells those apart.
_PRESENT = 0.15

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
    assert english.confidence.get("da", 0.0) < _PRESENT

    danish = detect(DA_PROSE)
    assert danish.top == "da"
    assert danish.confidence["da"] >= _PRESENT
    assert danish.confidence.get("en", 0.0) < _PRESENT


def test_danish_with_english_nouns_keeps_its_danish_share():
    """The case the whole design turns on. The top label is unstable here --
    Danish confuses with Norwegian Bokmal -- but Danish's own share stays well
    clear of the floor, which is why the gate tests the share."""
    d = detect(DA_WITH_ENGLISH_NOUNS)
    assert d.confidence["da"] >= _PRESENT
    assert d.confidence.get("en", 0.0) < _PRESENT


def test_translate_request_reads_as_its_source_language():
    """Mostly Danish tokens asking for an English reply. The detector cannot
    know that, and must not pretend to: it reports Danish, which is a shift
    away from an English window and therefore asks the classifier."""
    d = detect(DA_TRANSLATE_REQUEST)
    assert d.confidence["da"] >= _PRESENT
    assert d.confidence.get("en", 0.0) < _PRESENT


EN_QUOTE = (
    "the quick brown fox jumps over the lazy dog and then it runs away into "
    "the forest where nobody can find it any more, which is the whole point"
)


def test_a_quoted_passage_is_content_not_the_message_s_language():
    """A quote is what the operator is asking about, not what they are writing
    in. By volume a long English quote swamps the Danish sentence wrapping it,
    so reading the message whole answers English and the gate treats a Danish
    turn as a switch."""
    assert detect(f'Kan du forklare mig dette: "{EN_QUOTE}"').top == "da"
    assert detect(f'Kan du forklare mig dette: "{EN_QUOTE} {EN_QUOTE}"').top == "da"
    # The same in reverse: an English request quoting Danish is English.
    assert detect(
        'Can you explain this to me: "det virker ikke rigtigt her"').top == "en"


def test_a_message_that_is_only_a_quote_is_read_whole():
    """Nothing is left after the quote comes out, so there is nothing to read
    but the quote itself."""
    assert detect(f'"{EN_QUOTE}"').top == "en"


def test_code_is_not_read_as_language():
    """Backticked code is content too, and it drags the reading of the sentence
    around it."""
    d = detect("Hvorfor fejler `db.session.query(AppSetting)` her?")
    assert d.top == "da"


def test_an_undeclared_language_is_reported_honestly():
    """The detector is not restricted to any profile's languages, so a language
    nobody declared is reported as itself rather than force-fitted to a
    declared one. Both English and Danish sit below the floor, so any window
    asks."""
    d = detect(FI_PROSE)
    assert d.top == "fi"
    assert d.confidence.get("en", 0.0) < _PRESENT
    assert d.confidence.get("da", 0.0) < _PRESENT


def test_a_low_confidence_top_is_still_a_usable_share():
    """Technical English scores far below prose in absolute terms. The floor
    has to sit under that, or ordinary debugging messages read as shifts."""
    d = detect(EN_DEBUGGING)
    assert d.top == "en"
    assert d.confidence["en"] >= _PRESENT


def test_short_text_is_not_put_to_the_detector():
    """`ok` and `tak` carry no language content. Unrestricted the detector
    answers noise at noise-level confidence, which would read as a shift from
    any window, so they never reach it."""
    for text in ("ok", "tak", "ja"):
        d = detect(text)
        assert d.language_poor is True
        assert d.undetected is False
        assert d.top is None
        assert d.confidence == {}
        assert d.letters < LETTER_FLOOR


def test_a_short_message_in_a_dense_script_carries_language():
    """A complete Chinese sentence is ten characters. Counting characters alone
    calls that language-poor and reuses whatever came before -- which is how a
    Chinese request in a Spanish conversation was answered as Spanish. The
    detector is certain about it, and that certainty is what admits it."""
    d = detect("我现在用的是什么语言？")
    assert d.language_poor is False
    assert d.top == "zh"
    assert d.letters < LETTER_FLOOR

    # Two characters, still unambiguous.
    assert detect("你好").language_poor is False


def test_a_chinese_request_in_an_english_conversation_asks():
    """The reported bug, end to end: the operator switches to Chinese and the
    gate must not reuse the previous language."""
    d = decide(window_texts=[EN_PROSE, EN_DEBUGGING],
               request_text="我现在用的是什么语言？",
               has_previous=True, profile_languages_changed=False)
    assert d.should_ask is True
    assert d.trigger == TRIGGER_SHIFT


def test_noise_stays_language_poor_in_any_script():
    """The floor still has to reject acknowledgements, or every `ok` spends a
    model call. Measured, Latin noise tops out at 0.12 confidence while the
    weakest real non-Latin text scores 0.26."""
    for text in ("ok", "tak", "ja", "y", "hmm", "1234", "..."):
        assert detect(text).language_poor is True, text


def test_language_poor_and_undetected_are_different_answers():
    """Being too short to classify is not the same as being unclassifiable.
    Only the second is worth a model call, so the two must not collapse."""
    short = detect("ok")
    assert short.language_poor is True and short.undetected is False

    unclassifiable = detect("1234567890 !!!!!!!!!! ---------- @@@@@@@@@@")
    assert unclassifiable.language_poor is True
    assert unclassifiable.letters == 0


def test_slots_are_the_recently_used_languages_newest_first():
    """The scan walks back from the newest message, so the order is the room's
    own recency and doubles as the eviction order."""
    slots = language_slots(
        [DA_PROSE, EN_PROSE, "我现在用的是什么语言？"], pinned=())
    assert slots[0] == "zh"
    assert set(slots) == {"zh", "en", "da"}


def test_pinned_languages_are_always_present():
    """The profile's primary and secondary stay candidates however long ago
    they were used -- a declaration is a standing statement of intent."""
    slots = language_slots([EN_PROSE, EN_DEBUGGING], pinned=("en", "da"))
    assert "da" in slots


def test_the_set_is_capped_and_evicts_the_oldest_unpinned():
    """Four is the cap because the detector's sharpness decays with the size of
    the set; a fifth language pushes out the least recently used one that is
    not pinned."""
    texts = [DA_PROSE, "Bitte antworte auf Deutsch bitte sehr gerne",
             "Voglio che tu risponda in italiano adesso per favore",
             EN_PROSE, "我现在用的是什么语言？"]
    slots = language_slots(texts, pinned=("en",))
    assert len(slots) == LANGUAGE_SLOTS
    assert "en" in slots          # pinned, though it is not the newest
    assert "zh" in slots          # newest
    assert "da" not in slots      # oldest unpinned, evicted


def test_language_poor_messages_do_not_claim_a_slot():
    """An acknowledgement is not evidence that a language belongs to the room."""
    assert language_slots(["ok", "tak", EN_PROSE], pinned=()) == ("en",)


def test_pinned_languages_survive_an_empty_room():
    assert language_slots([], pinned=("en", "da")) == ("en", "da")


def test_a_named_language_is_found_in_any_language():
    """CLDR carries names and endonyms for every language in every language, so
    the check works without a table of our own and without favouring English."""
    assert names_a_language("Please answer in Danish from now on.") == (
        "Danish", "da")
    assert names_a_language("Svar pa dansk fra nu af.") == ("dansk", "da")
    assert names_a_language("Voglio che tu risponda in italiano.") == (
        "italiano", "it")
    assert names_a_language("Bitte antworte auf Deutsch.") == ("Deutsch", "de")


def test_a_language_named_in_a_spaceless_script_is_found():
    """Chinese, Japanese and Korean write without spaces, so the whole clause
    arrives as one token and the name has to be found inside it. Their language
    names are also two or three characters, below the minimum that keeps short
    Latin function words out -- a minimum that means nothing in a script where
    one character is a morpheme."""
    assert names_a_language("请用中文回答") == ("中文", "zh")
    assert names_a_language("请说英语") == ("英语", "en")
    assert names_a_language("日本語で答えて") == ("日本語", "ja")
    assert names_a_language("한국어로 답해 주세요") == ("한국어", "ko")


def test_ordinary_spaceless_text_names_no_language():
    """Scanning inside a token could match by accident. It does not: these
    names are compound morphemes rather than common words, so they do not fall
    out of surrounding text."""
    for text in ("你好，今天天气很好", "这个代码有问题需要修复",
                 "我们明天开会讨论这个项目", "数据库连接失败了",
                 "今天的会议取消了", "谢谢你的帮助"):
        assert names_a_language(text) is None, text


def test_asking_about_language_does_not_name_one():
    """The request that exposed the script bug asks which language it is in
    without naming any. The name check must stay out of it -- this one belongs
    to the shift test."""
    assert names_a_language("我现在用的是什么语言？") is None


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
    """One assertion per filter, and each is chosen to isolate it: it fails if
    that filter alone were removed, so a regression says which one broke.

    `Ewe` is 3 letters, resolves to `ee`, and round-trips -- only the length
    minimum rejects it. `second` is 6 letters and resolves to `cs`, but is not
    among Czech's recorded names -- only the round-trip rejects it."""
    # Too short, and would otherwise match: resolves to `ee` and round-trips.
    # No other token here matches, so `Ewe` is the one the length minimum stops.
    assert names_a_language("Please write your reply using Ewe") is None
    # Long enough and resolves to `cs`, but `second` is not among Czech's
    # recorded names -- only the round-trip catches this one.
    assert names_a_language("the second run failed") is None


def test_a_common_short_word_resolving_to_a_long_code_is_rejected():
    """`more` round-trips to Mossi (`mos`) and is by far the most frequent
    false fire measured on real operator traffic -- 37 of 875 messages. It
    clears NAME_MIN_LETTERS (4 letters) but not NAME_LONG_CODE_MIN_LETTERS
    (6), which applies because its resolved code is three letters long."""
    assert names_a_language(
        "I would like more detail, could you write a bit more?") is None
    assert names_a_language("more") is None


def test_a_language_without_a_two_letter_code_is_still_found():
    """The check is not restricted to ISO 639-1: a language whose only CLDR
    code is three letters is recognised exactly like one with a two-letter
    code, as long as it round-trips."""
    assert names_a_language("translate this into Cherokee") == (
        "Cherokee", "chr")
    assert names_a_language("Please reply in Cebuano.") == ("Cebuano", "ceb")
    assert names_a_language("Can you write this in Hawaiian?") == (
        "Hawaiian", "haw")


EN_WINDOW = [EN_PROSE, EN_DEBUGGING]
DA_WINDOW = [DA_PROSE, DA_WITH_ENGLISH_NOUNS]


def _decide(
    window, request, has_previous=True, profile_languages_changed=False,
) -> GateDecision:
    return decide(
        window_texts=window, request_text=request, has_previous=has_previous,
        profile_languages_changed=profile_languages_changed)


def test_an_unchanged_conversation_reuses():
    d = _decide(EN_WINDOW, EN_DEBUGGING)
    assert d.should_ask is False
    assert d.trigger == TRIGGER_REUSE
    assert d.window_dominant == "en"
    assert d.window_share is not None and d.window_share >= _PRESENT


def test_danish_technical_writing_in_a_danish_window_reuses():
    """The case that decides whether the feature is worth having. The top label
    is unstable across these messages; the Danish share is not."""
    d = _decide(DA_WINDOW, DA_WITH_ENGLISH_NOUNS)
    assert d.should_ask is False
    assert d.trigger == TRIGGER_REUSE


def test_a_short_message_in_the_same_language_reuses():
    """Short Latin text spreads its confidence thinly across the languages that
    share the script, so an absolute floor reads an unchanged conversation as a
    shift. `what do I do for work` scores 0.106 for English and `kan du
    hjaelpe mig med det her` 0.106 for Danish -- in both the window's own
    language is still the strongest candidate by a wide margin, which is what
    the test has to read."""
    english = _decide([EN_PROSE, "tell me where I live"],
                      "what do I do for work")
    assert english.should_ask is False, english.trigger

    danish = _decide([DA_PROSE, DA_WITH_ENGLISH_NOUNS],
                     "kan du hjaelpe mig med det her")
    assert danish.should_ask is False, danish.trigger


def test_a_change_of_language_asks():
    """DA_PROSE itself names a language (`dansk` occurs mid-sentence), which
    would trip the name check before the shift test ever ran -- not what this
    case means to exercise. DA_WITH_ENGLISH_NOUNS carries the same Danish
    share without naming anything, so this isolates the shift path."""
    d = _decide(EN_WINDOW, DA_WITH_ENGLISH_NOUNS)
    assert d.should_ask is True
    assert d.trigger == TRIGGER_SHIFT
    assert d.window_dominant == "en"
    assert d.window_share is not None and d.window_share < _PRESENT


DA_TRANSLATE_REQUEST_UNNAMED = (
    "kan du oversaette dette til mig: Jeg vil gerne vide hvor lang tid det "
    "tager."
)


def test_a_translate_request_asks():
    """Mostly Danish tokens asking (in Danish) to have the reply translated,
    in an English conversation. Naming the target language, as a real
    `translate to X` request would, is a separate signal the name check
    already owns -- this text avoids naming one so the case in point stays
    isolated: the detector reads Danish, which is a shift, so the classifier
    gets the question it is actually equipped to answer."""
    d = _decide(EN_WINDOW, DA_TRANSLATE_REQUEST_UNNAMED)
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


def test_a_changed_profile_asks_even_without_a_shift():
    """The operator changed their declared reply languages on `/profile` and
    kept writing in the same language: no shift, nothing named -- the caller's
    profile comparison is the only thing that can see this."""
    d = _decide(EN_WINDOW, EN_DEBUGGING, profile_languages_changed=True)
    assert d.should_ask is True
    assert d.trigger == TRIGGER_PROFILE_CHANGED


def test_an_unchanged_profile_still_reuses():
    """The default: the caller's comparison found nothing new, so the request
    is judged on the messages exactly as before."""
    d = _decide(EN_WINDOW, EN_DEBUGGING, profile_languages_changed=False)
    assert d.should_ask is False
    assert d.trigger == TRIGGER_REUSE


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


def test_detector_asked_and_found_nothing():
    """When the detector is asked (text meets LETTER_FLOOR) but returns no
    language, the gate asks the classifier with a shift trigger and zero share.

    Uses Old Italic script (𐌀𐌁𐌂𐌃𐌄𐌅𐌆𐌇𐌈𐌉𐌊𐌋𐌌𐌍𐌎𐌏) which the lingua
    detector cannot classify, producing Detection.undetected=True.
    """
    # This text has 16 letters but lingua cannot classify it as any language.
    undetectable = "𐌀𐌁𐌂𐌃𐌄𐌅𐌆𐌇𐌈𐌉𐌊𐌋𐌌𐌍𐌎𐌏"
    d = _decide(EN_WINDOW, undetectable)
    assert d.should_ask is True
    assert d.trigger == TRIGGER_SHIFT
    assert d.window_dominant == "en"
    assert d.window_share == 0.0
    assert d.request_letters == 16


def test_the_canonical_translate_request_asks():
    """The case cited in the module docstring: 'translate to english: <text>'
    with non-English source text. The request must always ask the classifier,
    whether through the name check (if `english` is found) or the shift test
    (if the source language is detected).

    This test is pinned on the outcome (should_ask=True), not the route: the
    name check currently catches this text first (token `english`), triggering
    via TRIGGER_NAMED_LANGUAGE rather than TRIGGER_SHIFT, but both are correct.
    A future simplification might remove the name check or change this request
    to avoid naming the target language -- the invariant that matters is that
    asking never regresses.
    """
    d = _decide(EN_WINDOW, DA_TRANSLATE_REQUEST, has_previous=True)
    assert d.should_ask is True
