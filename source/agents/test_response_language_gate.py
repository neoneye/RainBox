"""Tests for the response-language module: detection, the room's language
slots, the language name check, and `resolve()` — the function that decides a
turn's reply language, or that a model must.

It reads no settings and no database, so these tests need neither.
"""

import pytest

import agents.response_language_gate as response_language_gate
from agents.response_language_gate import (
    Detection,
    LANGUAGE_SLOTS,
    LETTER_FLOOR,
    SCAN_HORIZON_MESSAGES,
    TRIGGER_DETECTOR_ERROR,
    TRIGGER_DETECTORS_DISAGREE,
    TRIGGER_FIRST_MESSAGE_UNMATCHED,
    TRIGGER_NAMED_LANGUAGE,
    TRIGGER_OUTSIDE_SLOTS,
    TRIGGER_RESOLVED,
    TRIGGER_RESTRICTED_UNDECIDED,
    TRIGGER_UNDETECTED,
    Resolution,
    detect,
    detect_within,
    dominant_language,
    language_slots,
    names_a_language,
    resolve,
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


def test_duplicate_pinned_codes_do_not_double_book_a_slot():
    """A repeated pinned code must collapse to one slot, not two -- a doubled
    entry would otherwise waste a slot on a language the room already holds,
    and a wasted slot is a language the room can no longer detect against.
    The cap is what keeps detection sharp, so a bookkeeping duplicate must not
    be allowed to spend it."""
    slots = language_slots([], pinned=("en", "en"))
    assert slots == ("en",)


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


def test_the_scan_does_not_read_past_the_horizon(monkeypatch):
    """A monolingual room never fills its four slots, so unbounded the scan
    would walk the whole history on every turn -- 500 messages cost 0.4s
    cold, against the 11s call this design replaces, but it is still
    unbounded cost with no reason to pay it."""
    calls: list[str] = []
    real_detect = response_language_gate.detect

    def spy(text):
        calls.append(text)
        return real_detect(text)

    monkeypatch.setattr(response_language_gate, "detect", spy)
    texts = ["ok"] * 500 + [EN_PROSE]
    language_slots(texts, pinned=())
    assert len(calls) <= SCAN_HORIZON_MESSAGES


def test_pinned_languages_survive_an_empty_room():
    assert language_slots([], pinned=("en", "da")) == ("en", "da")


def test_detecting_within_the_slots_is_sharp():
    """Restricting to the room's own languages is what makes confidence usable:
    the same Danish sentence reads 0.25 against every language lingua knows and
    0.94 against four."""
    assert detect_within("hvor bor jeg?", ("en", "da")) == "da"
    assert detect_within("explain the joke", ("en", "da")) == "en"


def test_detecting_within_an_empty_slot_set_is_undecided():
    assert detect_within("hvor bor jeg?", ()) is None


def test_detecting_within_a_single_slot_returns_it_without_consulting_the_detector():
    """With one candidate there is no choice to make, so the detector has
    nothing to contribute. The only slot wins by default, regardless of what
    the text actually says. This is correct: the caller has already narrowed
    the field to this one language and is asking which of the candidates it
    is -- implicitly, one of them."""
    # Even though the text is clearly Danish, the single slot is English.
    # The short-circuit returns it anyway.
    assert detect_within("hvor bor jeg?", ("en",)) == "en"
    # With duplicates that reduce to one, still returns the sole code.
    assert detect_within("hvor bor jeg?", ("en", "en")) == "en"


def test_the_dominant_language_follows_a_sustained_switch():
    """Three English messages after a long, confident Danish one. Danish detects
    far higher than English ever does, so weighting by raw confidence keeps
    answering Danish however long the operator writes in English."""
    texts = [
        "du skrev en meget lang joke paa dansk om skelettet der ikke ville "
        "slaas med nogen som helst, og jeg forstod den simpelthen ikke",
        "can you say something funny about AI and testing",
        "explain the joke",
        "explain the joke, I meant the one about the skeleton",
    ]
    assert dominant_language(texts, ("en", "da")) == "en"


def test_one_foreign_sentence_does_not_move_the_conversation():
    """The half-life is the only knob deciding how many messages make a switch
    real, and one is not enough."""
    texts = [DA_PROSE, "det virker ikke rigtigt", "hvad er klokken",
             "Proceed with 5W1H"]
    assert dominant_language(texts, ("en", "da")) == "da"


def test_a_very_long_history_gives_the_same_answer_as_the_recent_tail():
    """Prepending hundreds of messages far beyond WINDOW_HALF_LIFE's reach
    must not change what the room resolves to -- the scan bound and the
    decay agree on what no longer matters."""
    tail = [
        "du skrev en meget lang joke paa dansk om skelettet der ikke ville "
        "slaas med nogen som helst, og jeg forstod den simpelthen ikke",
        "can you say something funny about AI and testing",
        "explain the joke",
        "explain the joke, I meant the one about the skeleton",
    ]
    long_history = ["ok"] * 500 + tail
    assert (dominant_language(long_history, ("en", "da"))
            == dominant_language(tail, ("en", "da")) == "en")


def test_dominant_language_does_not_read_past_the_horizon(monkeypatch):
    """`dominant_language` always scans -- unlike `language_slots` it never
    stops early when slots fill -- so an unbounded scan costs a detection on
    every message in a room's history, every turn."""
    calls: list[str] = []
    real_detect = response_language_gate.detect

    def spy(text):
        calls.append(text)
        return real_detect(text)

    monkeypatch.setattr(response_language_gate, "detect", spy)
    texts = ["ok"] * 500 + [EN_PROSE]
    dominant_language(texts, ("en", "da"))
    assert len(calls) <= SCAN_HORIZON_MESSAGES


def test_a_language_evicted_from_the_slots_casts_no_vote():
    """Four German messages, then Spanish and French evict German from the
    room's four slots. Restricted detection force-fits anything outside the
    slots to one of them -- unguarded, the German messages would each read as
    confident Danish votes and outnumber the French message that is actually
    in the slots. A message in a language the slots can no longer express is
    evidence about nothing the slots can express, so it must cast no vote
    rather than an invented one."""
    de_messages = [
        "Ich wollte nur kurz fragen, ob das System schon bereit ist fuer "
        "den naechsten Schritt.",
        "Koenntest du bitte noch einmal ueberpruefen, warum der Test "
        "fehlschlaegt.",
        "Das ist wirklich frustrierend, weil es gestern noch funktioniert "
        "hat.",
        "Ich denke, wir sollten das Problem heute noch loesen, bevor es "
        "schlimmer wird.",
    ]
    es_message = (
        "Quiero saber si el sistema ya esta listo para el siguiente paso, "
        "por favor."
    )
    fr_message = (
        "Je voudrais savoir si tu peux verifier pourquoi le test echoue "
        "maintenant."
    )
    texts = de_messages + [es_message, fr_message]
    slots = language_slots(texts, pinned=("en", "da"))
    assert "de" not in slots
    assert dominant_language(texts, slots) not in (None, "da")


def test_a_room_with_nothing_to_weigh_is_undecided():
    assert dominant_language(["ok", "tak"], ("en", "da")) is None


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


EN_HISTORY = [EN_PROSE, EN_DEBUGGING]


def _resolve(history, request, pinned=("en", "da"), fallback="en"):
    return resolve(history=history, request=request,
                   pinned=pinned, fallback=fallback)


def test_an_ordinary_turn_resolves_without_the_model():
    """The case that must never cost a model call: a message in a language the
    room already speaks."""
    r = _resolve(EN_HISTORY, "what do I do for work")
    assert r.ask is False
    assert r.trigger == TRIGGER_RESOLVED
    assert r.language == "en"


def test_a_switch_to_another_slot_language_resolves():
    """Switching to a language the room already speaks is not a question for a
    model -- the detector can see which one it is.

    Uses DA_WITH_ENGLISH_NOUNS rather than DA_PROSE: DA_PROSE contains
    `dansk` mid-sentence, which the name check would catch first, testing
    TRIGGER_NAMED_LANGUAGE instead of the detection path this case means to
    exercise."""
    r = _resolve(EN_HISTORY, DA_WITH_ENGLISH_NOUNS)
    assert r.ask is False
    assert r.language == "da"


def test_a_named_language_asks():
    r = _resolve(EN_HISTORY, "Please answer in French from now on.")
    assert r.ask is True
    assert r.trigger == TRIGGER_NAMED_LANGUAGE
    assert r.named_language == "French"


def test_a_language_outside_the_slots_asks():
    """Restricted detection would force-fit this to a slot language. The
    unrestricted detector is what notices it does not belong."""
    r = _resolve(EN_HISTORY, "Haluaisin etta vastaat minulle suomeksi kiitos")
    assert r.ask is True
    assert r.trigger == TRIGGER_OUTSIDE_SLOTS


def test_a_first_message_matching_no_declared_language_asks():
    """Nonsense and a genuine foreign first contact are one input to the
    detector -- `osuf ljweroiux jsdfoij wnoer` reads as Dutch at 0.215 and 0.899
    against a declared set. Only the model can tell them apart."""
    r = _resolve([], "osuf ljweroiux jsdfoij wnoer")
    assert r.ask is True
    assert r.trigger == TRIGGER_FIRST_MESSAGE_UNMATCHED


def test_a_conversation_of_only_short_messages_is_not_a_first_message():
    """`slots` can be empty in the middle of a conversation -- nothing pinned
    and every earlier message too short to claim a slot -- and that must not
    be read as a first message. The trigger is what a run says about itself,
    so it has to say `outside_slots`, not `first_message_unmatched`, for a
    request that plainly has history behind it."""
    r = _resolve(["ok", "sure"], FI_PROSE, pinned=())
    assert r.ask is True
    assert r.trigger == TRIGGER_OUTSIDE_SLOTS


def test_a_first_message_in_a_declared_language_resolves():
    """The annoyance this design exists to remove: waiting on a model to be
    told yes, this is English."""
    r = _resolve([], "can you say something funny about AI and testing")
    assert r.ask is False
    assert r.language == "en"


def test_a_language_poor_first_message_takes_the_fallback():
    """No history and nothing in the request to read. There is nothing for a
    model to decide between, so it is not asked."""
    r = _resolve([], "ok", pinned=(), fallback="en")
    assert r.ask is False
    assert r.language == "en"


def test_a_language_poor_request_keeps_the_conversation():
    """`ok` in an English conversation does not restart the question."""
    r = _resolve(EN_HISTORY, "ok")
    assert r.ask is False
    assert r.language == "en"


def test_an_undetected_request_asks():
    """`language_poor` and `undetected` are deliberately separate: the first
    means the message is too short to be worth classifying, the second means
    the detector was asked and genuinely found nothing. Only the second is a
    real message the model should be given -- `detect` is memoised, so this
    drives it the same way the raising-detector test above does, by
    monkeypatching the module-level name rather than the real detector."""
    import agents.response_language_gate as gate

    undetected = Detection(
        letters=20, language_poor=False, undetected=True, top=None)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(gate, "detect", lambda _text: undetected)
    try:
        r = _resolve(EN_HISTORY, "xzq wvbt qplk fjhr mnop")
    finally:
        monkeypatch.undo()
    assert r.ask is True
    assert r.trigger == TRIGGER_UNDETECTED


def test_the_two_detectors_disagreeing_asks(monkeypatch):
    """The unrestricted detector already places this message in a slot
    language; monkeypatching `detect_within` makes the restricted detector
    name a different one. Neither detector is authoritative over the other,
    so the model settles it."""
    import agents.response_language_gate as gate

    monkeypatch.setattr(gate, "detect_within", lambda _text, _slots: "da")
    r = _resolve(EN_HISTORY, "what do I do for work")
    assert r.ask is True
    assert r.trigger == TRIGGER_DETECTORS_DISAGREE


def test_the_restricted_detector_failing_to_decide_asks(monkeypatch):
    """Distinct from disagreement: here the restricted detector answers
    nothing at all, rather than answering with a different language, so there
    is no second opinion for the model to weigh -- only an absence."""
    import agents.response_language_gate as gate

    monkeypatch.setattr(gate, "detect_within", lambda _text, _slots: None)
    r = _resolve(EN_HISTORY, "what do I do for work")
    assert r.ask is True
    assert r.trigger == TRIGGER_RESTRICTED_UNDECIDED


def test_a_raising_detector_asks_when_resolving():
    """A resolver that cannot decide has decided to ask."""
    import agents.response_language_gate as gate

    def boom(_text):
        raise RuntimeError("detector unavailable")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(gate, "detect", boom)
    try:
        r = _resolve(EN_HISTORY, EN_PROSE)
    finally:
        monkeypatch.undo()
    assert r.ask is True
    assert r.trigger == TRIGGER_DETECTOR_ERROR
    assert "detector unavailable" in r.as_args()["error"]


def test_the_decision_records_what_it_read():
    args = _resolve(EN_HISTORY, "what do I do for work").as_args()
    assert args["trigger"] == TRIGGER_RESOLVED
    assert args["ask"] is False
    assert args["language"] == "en"
    assert set(args["slots"]) >= {"en", "da"}
    assert isinstance(args["detector_ms"], int)
