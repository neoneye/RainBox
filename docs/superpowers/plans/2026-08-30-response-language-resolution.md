# Response-Language Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve the assistant's reply language deterministically from the room's
recent languages and the operator's declared profile, calling
`response_language_classifier` only where a computed answer would be a guess.

**Architecture:** One backward scan over a room's operator messages fills four
language slots (the profile's primary and secondary, pinned, plus the two most
recently used others) and weighs which language the conversation is running in.
Two detectors do different jobs: unrestricted answers *is this one of ours?*,
restricted to the four slots answers *which one?* — and being restricted is what
makes its confidences sharp. The reply language is then constructed, not looked
up: nothing is stored and nothing is reused, so nothing can go stale.

**Tech Stack:** Python 3.14, Flask-SQLAlchemy, pytest,
`lingua-language-detector==2.2.0`, `language_data` (CLDR names).

**Design spec:** `docs/superpowers/specs/2026-08-30-response-language-resolution-design.md`.
Read it before Task 1 — it carries the measurements behind every constant here,
and the traps section names three changes that look like improvements and are not.

## Global Constraints

- Work from `source/` with that directory's venv: `cd source && ./venv/bin/python -m pytest ...`
- `source/conftest.py` forces pytest onto `rainbox_claude`. For any ad-hoc script
  set `DATABASE_URL=postgresql+psycopg://localhost/rainbox_claude` explicitly.
  **Never run anything against `rainbox_production`.**
- Any test that changes a setting must restore it — `set_setting` commits, and a
  leaked value has broken an unrelated test file on this branch before.
- The switch `assistant.response_language_gate` stays **default off**. Every
  existing test must see today's behaviour.
- Constants, never inline literals: `LANGUAGE_SLOTS = 4`, `LETTER_FLOOR = 16`,
  `CONFIDENCE_FLOOR = 0.20`, `WINDOW_HALF_LIFE = 3.0`, `NAME_MIN_LETTERS = 4`,
  `NAME_LONG_CODE_MIN_LETTERS = 6`, `MAX_LANGUAGE_ROWS = 6`.
- Language codes compare on the base subtag (`en`, not `en-US`) everywhere except
  the constructed answer, which carries the profile's exact declared tags.
- **No hardcoded language tables.** Every language name comes from CLDR via
  `language_data`. Danish, English, Chinese and Japanese appear in tests because
  they are the measured cases, never in shipped defaults.
- The gate fails open: any exception means run the classifier.
- Comments and docstrings describe how the code works NOW — no migration notes,
  no "previously", no references to this plan.
- Commit messages end with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
  Never amend; each revision is its own commit.

## File structure

| file | responsibility after this plan |
|---|---|
| `source/agents/response_language_gate.py` | detection, the slot scan, the name check, and the resolution decision. Stays a pure function of message text plus a caller-supplied language set — no settings, no database. |
| `source/agents/assistant.py` | reads the switch and the profile, gathers the room's messages, calls the module, constructs the ranked answer, records the step row. |
| `source/db/profile_languages.py` | `MAX_LANGUAGE_ROWS` drops to 6. |
| `source/user_profile/formatting.py` | suppresses the guide's language line when the room has no history. |

---

### Task 1: The room's language slots

**Files:**
- Modify: `source/agents/response_language_gate.py`
- Test: `source/agents/test_response_language_gate.py`

**Interfaces:**
- Consumes: `detect(text) -> Detection` and `WINDOW_HALF_LIFE`, both already in the module.
- Produces: `LANGUAGE_SLOTS = 4` and
  `language_slots(texts: Sequence[str], pinned: Sequence[str]) -> tuple[str, ...]`
  — the room's candidate languages, most recently used first, at most
  `LANGUAGE_SLOTS` of them, with `pinned` always present.

This replaces `window_dominant`, which answered only "what is the conversation
in" and had no notion of slots. One backward traversal now does both jobs; the
dominant language is Task 2.

- [ ] **Step 1: Write the failing test**

Add to `source/agents/test_response_language_gate.py`:

```python
from agents.response_language_gate import LANGUAGE_SLOTS, language_slots


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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd source && ./venv/bin/python -m pytest agents/test_response_language_gate.py -q -k "slots or pinned"`
Expected: FAIL — `ImportError: cannot import name 'language_slots'`

- [ ] **Step 3: Write the implementation**

Add to `source/agents/response_language_gate.py`, replacing `window_dominant`:

```python
#: How many languages a room detects against. The restricted detector's
#: sharpness decays with the size of the set -- measured, `hvor bor jeg?` reads
#: 0.98 against two languages, 0.94 against four, 0.72 against eight and 0.42
#: against twelve, against 0.25 unrestricted. Four is at the knee.
LANGUAGE_SLOTS = 4


def language_slots(
    texts: Sequence[str], pinned: Sequence[str],
) -> tuple[str, ...]:
    """The room's candidate languages, most recently used first.

    `texts` are the operator's messages, oldest last is not assumed -- they
    arrive oldest first and are walked backwards, so the first language found is
    the most recent. That ordering is the eviction order: a fifth language
    displaces the least recently used one.

    `pinned` are the profile's primary and secondary languages. They are
    candidates whether or not they were used lately, because a declaration is a
    standing statement of intent rather than an observation. Only two are
    pinned, not every declared language: a profile may declare more languages
    than there are slots, and holding them all would give back the sharpness the
    cap exists to protect.

    Language-poor messages claim no slot -- an acknowledgement is not evidence
    that a language belongs to the room.
    """
    slots: list[str] = [code for code in pinned if code]
    for text in reversed(list(texts)):
        if len(slots) >= LANGUAGE_SLOTS:
            break
        detection = detect(text)
        if detection.language_poor or detection.undetected or not detection.top:
            continue
        if detection.top not in slots:
            slots.append(detection.top)
    return tuple(slots[:LANGUAGE_SLOTS])
```

Note the ordering consequence, and leave it as it is: pinned languages occupy
their slots first, so the returned tuple is "pinned, then recent". The tests
above assert membership rather than a full ordering for exactly that reason.

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd source && ./venv/bin/python -m pytest agents/test_response_language_gate.py -q -k "slots or pinned"`
Expected: PASS, 5 tests.

- [ ] **Step 5: Remove `window_dominant` and its tests**

Delete `window_dominant` from the module and delete the tests naming it:
`test_window_dominant_reads_a_uniform_conversation`,
`test_short_messages_do_not_enter_the_window`,
`test_a_window_of_only_short_messages_is_empty`,
`test_a_tie_breaks_on_the_language_code`,
`test_the_window_keeps_only_the_most_recent_messages`,
`test_a_long_message_cannot_single_handedly_define_the_window`,
`test_the_window_follows_a_conversation_that_switched`,
`test_a_confident_language_does_not_outvote_a_diffuse_one`.

Their behaviour is re-established by Task 2's dominant-language tests, which
cover the same properties against the new mechanism. Remove `WINDOW_MESSAGES`
too — the scan is bounded by `LANGUAGE_SLOTS`, not by a message count.

- [ ] **Step 6: Run the module's whole test file**

Run: `cd source && ./venv/bin/python -m pytest agents/test_response_language_gate.py -q`
Expected: PASS. Failures naming `window_dominant` mean a test was missed in step 5.

- [ ] **Step 7: Commit**

```bash
git add source/agents/response_language_gate.py source/agents/test_response_language_gate.py
git commit -m "feat(assistant): fill a room's language slots from one backward scan"
```

---

### Task 2: The dominant language, detected against the slots

**Files:**
- Modify: `source/agents/response_language_gate.py`
- Test: `source/agents/test_response_language_gate.py`

**Interfaces:**
- Consumes: `language_slots(texts, pinned) -> tuple[str, ...]`, `LANGUAGE_SLOTS`,
  `WINDOW_HALF_LIFE`, `detect(text) -> Detection`.
- Produces: `detect_within(text: str, slots: Sequence[str]) -> str | None` and
  `dominant_language(texts: Sequence[str], slots: Sequence[str]) -> str | None`.

- [ ] **Step 1: Write the failing test**

```python
from agents.response_language_gate import detect_within, dominant_language


def test_detecting_within_the_slots_is_sharp():
    """Restricting to the room's own languages is what makes confidence usable:
    the same Danish sentence reads 0.25 against every language lingua knows and
    0.94 against four."""
    assert detect_within("hvor bor jeg?", ("en", "da")) == "da"
    assert detect_within("explain the joke", ("en", "da")) == "en"


def test_detecting_within_an_empty_slot_set_is_undecided():
    assert detect_within("hvor bor jeg?", ()) is None


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


def test_a_room_with_nothing_to_weigh_is_undecided():
    assert dominant_language(["ok", "tak"], ("en", "da")) is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd source && ./venv/bin/python -m pytest agents/test_response_language_gate.py -q -k "within or dominant"`
Expected: FAIL — `ImportError: cannot import name 'detect_within'`

- [ ] **Step 3: Write the implementation**

```python
@functools.lru_cache(maxsize=64)
def _restricted_detector(slots: tuple[str, ...]):
    """A detector over just these languages, built once per distinct set.

    Restriction is what makes confidence usable rather than merely available:
    the same Danish sentence reads 0.25 against every language lingua knows and
    0.94 against four. It also force-fits anything outside the set, which is why
    `decide` asks the unrestricted detector whether the request belongs to these
    languages before believing this one's answer.
    """
    from lingua import IsoCode639_1, LanguageDetectorBuilder

    codes = []
    for code in slots:
        try:
            codes.append(IsoCode639_1[code.upper()])
        except KeyError:
            continue
    if len(codes) < 2:
        # lingua needs two languages to choose between. One slot is not a
        # choice, and zero is not a question.
        return None
    return LanguageDetectorBuilder.from_iso_codes_639_1(*codes).build()


def detect_within(text: str, slots: Sequence[str]) -> str | None:
    """Which of `slots` this text is, or None when there is nothing to choose."""
    unique = tuple(dict.fromkeys(code for code in slots if code))
    if not unique:
        return None
    if len(unique) == 1:
        return unique[0]
    detector = _restricted_detector(unique)
    if detector is None:
        return None
    values = detector.compute_language_confidence_values(text or "")
    if not values:
        return None
    return values[0].language.iso_code_639_1.name.lower()


def dominant_language(
    texts: Sequence[str], slots: Sequence[str],
) -> str | None:
    """Which of `slots` the conversation is running in.

    Every message casts one vote for its own language and votes decay with age
    at WINDOW_HALF_LIFE, so the newest message weighs most and one foreign
    sentence does not move a conversation while a sustained switch does.

    Votes are equal rather than weighted by confidence or length. Neither
    compares between messages: a Danish sentence detects far higher than an
    equally clear English one, and a long quoted passage is not better evidence
    of what the operator is writing in than the sentence they just typed.
    """
    totals: dict[str, float] = {}
    for age, text in enumerate(reversed(list(texts))):
        detection = detect(text)
        if detection.language_poor or detection.undetected:
            continue
        code = detect_within(text, slots)
        if code is None:
            continue
        totals[code] = totals.get(code, 0.0) + 0.5 ** (age / WINDOW_HALF_LIFE)
    if not totals:
        return None
    # Ties break on the code itself, so the answer never depends on dict
    # insertion order -- which is the scan's own order, and incidental.
    return min(totals, key=lambda code: (-totals[code], code))
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd source && ./venv/bin/python -m pytest agents/test_response_language_gate.py -q`
Expected: PASS, whole file.

- [ ] **Step 5: Commit**

```bash
git add source/agents/response_language_gate.py source/agents/test_response_language_gate.py
git commit -m "feat(assistant): detect against a room's own languages"
```

---

### Task 3: The resolution decision

**Files:**
- Modify: `source/agents/response_language_gate.py`
- Test: `source/agents/test_response_language_gate.py`

**Interfaces:**
- Consumes: everything from Tasks 1-2, plus `names_a_language`, `detect`.
- Produces: `Resolution` (frozen dataclass: `ask: bool`, `trigger: str`,
  `language: str | None`, `slots: tuple[str, ...]`, `named_language: str | None`,
  `detector_ms: int`, `error: str | None`, and `as_args() -> dict`), plus
  `resolve(*, history: Sequence[str], request: str, pinned: Sequence[str],
  fallback: str) -> Resolution`, and the trigger constants
  `TRIGGER_NAMED_LANGUAGE`, `TRIGGER_FIRST_MESSAGE_UNMATCHED`,
  `TRIGGER_OUTSIDE_SLOTS`, `TRIGGER_DETECTORS_DISAGREE`,
  `TRIGGER_DETECTOR_ERROR`, `TRIGGER_RESOLVED`.

`fallback` is the language to use when nothing else decides — the caller supplies
it (Task 4) so this module stays free of profiles and settings.

- [ ] **Step 1: Write the failing test**

```python
from agents.response_language_gate import (
    TRIGGER_DETECTOR_ERROR,
    TRIGGER_DETECTORS_DISAGREE,
    TRIGGER_FIRST_MESSAGE_UNMATCHED,
    TRIGGER_NAMED_LANGUAGE,
    TRIGGER_OUTSIDE_SLOTS,
    TRIGGER_RESOLVED,
    Resolution,
    resolve,
)

EN_HISTORY = [EN_PROSE, EN_DEBUGGING]
DA_HISTORY = [DA_PROSE, "det virker ikke rigtigt"]


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
    model -- the detector can see which one it is."""
    r = _resolve(EN_HISTORY, DA_PROSE)
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


def test_a_raising_detector_asks():
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
```

Add `import pytest` to the test file's imports if it is not already there.

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd source && ./venv/bin/python -m pytest agents/test_response_language_gate.py -q -k resolve`
Expected: FAIL — `ImportError: cannot import name 'resolve'`

- [ ] **Step 3: Write the implementation**

Replace the `TRIGGER_*` constants, `GateDecision` and `decide` with:

```python
TRIGGER_NAMED_LANGUAGE = "named_language"
TRIGGER_FIRST_MESSAGE_UNMATCHED = "first_message_unmatched"
TRIGGER_OUTSIDE_SLOTS = "outside_slots"
TRIGGER_DETECTORS_DISAGREE = "detectors_disagree"
TRIGGER_DETECTOR_ERROR = "detector_error"
TRIGGER_RESOLVED = "resolved"


@dataclass(frozen=True)
class Resolution:
    """What the turn decided, and what it read to decide it.

    Recorded whole on the step row: a turn that skipped the assistant's most
    expensive call has to say what it saw and what it concluded, or a reply in
    the wrong language leaves the operator guessing.
    """

    ask: bool
    trigger: str
    language: str | None = None
    slots: tuple[str, ...] = ()
    named_language: str | None = None
    detector_ms: int = 0
    error: str | None = None

    def as_args(self) -> dict:
        args = {
            "ask": self.ask,
            "trigger": self.trigger,
            "language": self.language,
            "slots": list(self.slots),
            "named_language": self.named_language,
            "detector_ms": self.detector_ms,
        }
        if self.error:
            args["error"] = self.error
        return args


def resolve(
    *,
    history: Sequence[str],
    request: str,
    pinned: Sequence[str],
    fallback: str,
) -> Resolution:
    """Decide the reply language, or that only a model can.

    `history` is the operator's earlier messages, oldest first; `request` is the
    message being answered and is excluded from the history it is judged
    against. `pinned` is the profile's primary and secondary; `fallback` is what
    to use when nothing else decides.

    Every uncertainty resolves toward asking. A needless ask costs latency; a
    wrong resolution costs one reply in the wrong language, which is visible and
    corrects on the next turn.
    """
    started = time.perf_counter()

    def elapsed() -> int:
        return int((time.perf_counter() - started) * 1000)

    try:
        # Cheapest, and independent of everything else: an instruction naming a
        # language is invisible to detection, because it is written in whatever
        # language the operator is already using.
        named = names_a_language(request)
        if named:
            return Resolution(
                ask=True, trigger=TRIGGER_NAMED_LANGUAGE,
                named_language=named[0], detector_ms=elapsed())

        slots = language_slots(history, pinned)
        detection = detect(request)

        if detection.language_poor or detection.undetected:
            # Nothing in the request to read. Keep the conversation if there is
            # one, and otherwise take the fallback -- there is nothing here for
            # a model to decide between either.
            language = dominant_language(history, slots) or fallback
            return Resolution(
                ask=False, trigger=TRIGGER_RESOLVED, language=language,
                slots=slots, detector_ms=elapsed())

        if detection.top not in slots:
            # The unrestricted detector says this is not one of the room's
            # languages. Restricted detection would force-fit it to one, so the
            # model decides -- and on a first message that is also the only way
            # to tell a foreign language from nonsense.
            trigger = (TRIGGER_FIRST_MESSAGE_UNMATCHED if not slots
                       or not history else TRIGGER_OUTSIDE_SLOTS)
            return Resolution(
                ask=True, trigger=trigger, slots=slots,
                detector_ms=elapsed())

        within = detect_within(request, slots)
        if within is None:
            return Resolution(
                ask=True, trigger=TRIGGER_DETECTORS_DISAGREE, slots=slots,
                detector_ms=elapsed())
        if within != detection.top:
            # The two instruments disagree about which of the room's languages
            # this is. Neither is authoritative over the other, so the model
            # settles it.
            return Resolution(
                ask=True, trigger=TRIGGER_DETECTORS_DISAGREE, slots=slots,
                detector_ms=elapsed())

        return Resolution(
            ask=False, trigger=TRIGGER_RESOLVED, language=within,
            slots=slots, detector_ms=elapsed())
    except Exception as exc:
        logger.warning("response-language resolution failed open", exc_info=True)
        return Resolution(
            ask=True, trigger=TRIGGER_DETECTOR_ERROR, detector_ms=elapsed(),
            error=f"{type(exc).__name__}: {exc}")
```

Delete `SHIFT_RATIO` and the `Detection.share` method if nothing else uses them.

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd source && ./venv/bin/python -m pytest agents/test_response_language_gate.py -q`
Expected: PASS, whole file.

- [ ] **Step 5: Commit**

```bash
git add source/agents/response_language_gate.py source/agents/test_response_language_gate.py
git commit -m "feat(assistant): resolve the reply language, or defer to the model"
```

---

### Task 4: Constructing the answer

**Files:**
- Modify: `source/agents/assistant.py`
- Test: `source/agents/test_assistant_response_language_gate.py`

**Interfaces:**
- Consumes: `ResponseLanguageClassification`, `ResponseLanguageItem` (already in
  `agents/assistant.py`), `user_profile.declared_language_candidates`,
  `user_profile.formatting.valid_profile_languages`.
- Produces: `AssistantAgent._constructed_classification(language, profile) ->
  ResponseLanguageClassification`.

**Why this can be constructed at all:** `_format_reply_language_markdown` is
score-free — it sorts by score and emits an ordered list of tags with a one-line
reason, so numbers never reach a prompt. What downstream consumes is a ranking.

- [ ] **Step 1: Write the failing test**

```python
def test_a_constructed_classification_ranks_the_resolved_language_first():
    """The resolved language leads; the profile's other declared languages
    follow in their own preference order."""
    agent = _agent()
    profile = {"data": {"languages": {"rows": [
        {"tag": "en-US", "level": "native", "stance": "prefer", "note": ""},
        {"tag": "da", "level": "native", "stance": "neutral", "note": ""},
    ]}}}
    result = agent._constructed_classification("da", profile)
    assert [item.code for item in result.languages] == ["da", "en-US"]
    assert "da" in agent._format_reply_language_markdown(result)


def test_a_constructed_classification_uses_the_declared_variant():
    """A base subtag from the detector becomes the profile's exact declared tag,
    so `en` resolves to `en-US` rather than losing the variant."""
    agent = _agent()
    profile = {"data": {"languages": {"rows": [
        {"tag": "en-US", "level": "native", "stance": "prefer", "note": ""},
    ]}}}
    result = agent._constructed_classification("en", profile)
    assert result.languages[0].code == "en-US"


def test_a_constructed_classification_says_it_was_not_a_model():
    """The reason travels into the prompt and onto the trace; it must not read
    as a model's verdict."""
    agent = _agent()
    result = agent._constructed_classification("en", None)
    assert result.languages[0].code == "en"
    assert "detect" in result.reason.lower()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd source && ./venv/bin/python -m pytest agents/test_assistant_response_language_gate.py -q -k constructed`
Expected: FAIL — `AttributeError: 'AssistantAgent' object has no attribute '_constructed_classification'`

- [ ] **Step 3: Write the implementation**

Add to `AssistantAgent`, beside `_format_reply_language_markdown`:

```python
    def _constructed_classification(
        self, language: str, profile: dict[str, Any] | None,
    ) -> ResponseLanguageClassification:
        """The turn's language decision, built rather than asked for.

        `language` is a base subtag from detection; the profile's declared tag
        for it wins, so a detected `en` becomes the declared `en-US` rather than
        losing the variant. The resolved language ranks first and the profile's
        other declared languages follow in their own preference order, which is
        the shape `_format_reply_language_markdown` reads.

        The scores here exist only to carry that order -- the Markdown
        projection is score-free, so no number reaches a prompt, and none is
        invented for one. The reason says the decision was made by detection so
        that neither a reader nor a later model mistakes it for a verdict.
        """
        declared = user_profile.declared_language_candidates(profile)
        codes: list[str] = []
        for row in declared:
            code = str(row.get("code") or "")
            if code and code.split("-")[0].lower() == language.lower():
                codes.append(code)
        if not codes:
            codes.append(language)
        for row in declared:
            code = str(row.get("code") or "")
            if code and code not in codes:
                codes.append(code)
        top = max(2, min(5, len(codes)))
        return ResponseLanguageClassification(
            reason=(
                f"Resolved by detection: the request is in {codes[0]}, which "
                "the conversation or the profile already establishes. No model "
                "was asked."
            ),
            languages=[
                ResponseLanguageItem(code=code, score=max(1, top - index))
                for index, code in enumerate(codes)
            ],
        )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd source && ./venv/bin/python -m pytest agents/test_assistant_response_language_gate.py -q -k constructed`
Expected: PASS, 3 tests.

- [ ] **Step 5: Commit**

```bash
git add source/agents/assistant.py source/agents/test_assistant_response_language_gate.py
git commit -m "feat(assistant): construct a language decision without a model"
```

---

### Task 5: Wiring the resolution into the turn

**Files:**
- Modify: `source/agents/assistant.py`
- Test: `source/agents/test_assistant_response_language_gate.py`

**Interfaces:**
- Consumes: `resolve(...)`, `Resolution`, the trigger constants (Task 3);
  `_constructed_classification` (Task 4); `_response_language_gate_enabled`.
- Produces: `AssistantAgent._resolve_response_language(messages, profile) ->
  Resolution | None`, and the resolved path inside
  `_run_response_language_classifier`.

- [ ] **Step 1: Write the failing test**

```python
EN_MESSAGES = [
    {"sender_type": "human", "text": "can you say something funny about AI"},
    {"sender_type": "agent", "text": "Here is a joke about testing."},
    {"sender_type": "human", "text": "explain the joke, the skeleton one"},
]


def test_the_resolver_is_not_consulted_when_the_switch_is_off(room, monkeypatch):
    import agents.response_language_gate as gate

    db.set_setting("assistant.response_language_gate", False)
    called = []
    monkeypatch.setattr(gate, "resolve", lambda **kw: called.append(kw))
    assert _agent()._resolve_response_language(EN_MESSAGES, None) is None
    assert called == []


def test_only_the_operator_s_messages_are_read(room):
    """A reply is written in whatever language a previous decision chose, so
    letting replies vote would let one wrong decision justify itself."""
    db.set_setting("assistant.response_language_gate", True)
    messages = [
        {"sender_type": "human", "text": DA_PROSE_TEXT},
        {"sender_type": "agent",
         "text": "This English reply must not count as evidence about what "
                 "language the operator is writing in at all."},
        {"sender_type": "human", "text": "det virker ikke rigtigt"},
    ]
    r = _agent()._resolve_response_language(messages, _DA_EN_PROFILE)
    assert r is not None
    assert r.language == "da"


def test_the_request_is_not_part_of_its_own_history(room):
    db.set_setting("assistant.response_language_gate", True)
    messages = [
        {"sender_type": "human", "text": "det virker ikke rigtigt her"},
        {"sender_type": "human", "text": "can you say something funny about AI"},
    ]
    r = _agent()._resolve_response_language(messages, _DA_EN_PROFILE)
    assert r is not None
    assert r.ask is False


def test_an_ordinary_turn_records_a_resolved_row(room, monkeypatch):
    """The row is the point: the operator reads runs, and a turn that resolved
    without a model must say so, in place, with what it read."""
    db.set_setting("assistant.response_language_gate", True)
    agent = _agent()
    agent._run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=ASSISTANT_UUID)

    def fail(*a, **kw):
        raise AssertionError("the classifier must not run on a resolved turn")

    monkeypatch.setattr(
        agent, "_request_response_language_classification", fail)
    monkeypatch.setattr(
        agent, "_capture_profile_context", lambda: _DA_EN_CONTEXT)

    agent._run_response_language_classifier(
        step_index=0, messages=EN_MESSAGES, profile=_DA_EN_PROFILE)
    db.session.commit()

    row = (db.session.query(db.AssistantStep)
           .filter(db.AssistantStep.run_uuid == agent._run.uuid)
           .order_by(db.AssistantStep.id.desc()).first())
    assert row.phase == "skipped"
    assert row.args["gate"]["trigger"] == "resolved"
    assert row.args["gate"]["language"] == "en-US"
    assert row.system_prompt is None and row.user_prompt is None
    assert row.duration_ms is not None and row.duration_ms < 1000
    assert agent._response_language_classification is not None
    assert "en-US" in agent._reply_language_markdown
```

Define the fixtures near the top of the file:

```python
DA_PROSE_TEXT = ("Jeg vil gerne have at du svarer pa dansk naar jeg skriver "
                 "pa dansk til dig.")
_DA_EN_PROFILE = {"data": {"languages": {"rows": [
    {"tag": "en-US", "level": "native", "stance": "prefer", "note": ""},
    {"tag": "da", "level": "native", "stance": "neutral", "note": ""},
]}}}
_DA_EN_CONTEXT = user_profile.ProfileContext(profile=_DA_EN_PROFILE)
```

Import `user_profile` in the test module if it is not already imported.
`ProfileContext` is a dataclass with `profile_uuid`, `profile`,
`facts_invalidated_at` and `profile_changed_at`, all defaulted, so `profile=` on
its own is valid.

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd source && ./venv/bin/python -m pytest agents/test_assistant_response_language_gate.py -q -k "resolver or operator_s_messages or own_history or resolved_row"`
Expected: FAIL — `AttributeError: ... '_resolve_response_language'`

- [ ] **Step 3: Write the resolver helper**

Replace `_response_language_gate_decision` in `source/agents/assistant.py` with:

```python
    def _resolve_response_language(
        self, messages: list[dict[str, Any]], profile: dict[str, Any] | None,
    ) -> "response_language_gate.Resolution | None":
        """This turn's language decision, or None when the gate does not apply.

        None means "run the classifier as always": the switch is off, or there
        is no operator message to read.

        Only the operator's messages are read. A reply is written in whatever
        language a previous decision chose, so letting replies vote would let
        one wrong decision keep justifying itself. The final human message is
        the request being judged and is excluded from the history it is judged
        against.
        """
        from user_profile.formatting import valid_profile_languages

        if not self._response_language_gate_enabled():
            return None
        human_texts = [
            str(m.get("text") or "")
            for m in messages
            if self._message_role(m) == "user"
        ]
        if not human_texts:
            return None
        # The spec anchors the fallback on the profile of the room's FIRST
        # HUMAN WRITER. Today `chat_user` carries no link to a profile, so one
        # active profile serves every member and the two readings name the same
        # object. Written this way, wiring profiles to users later changes this
        # lookup rather than the rule.
        primary, secondary = valid_profile_languages(profile or {})
        pinned = tuple(
            code.split("-")[0].lower()
            for code in (primary, secondary) if code)
        fallback = primary or DEFAULT_REPLY_LANGUAGE
        return response_language_gate.resolve(
            history=human_texts[:-1],
            request=human_texts[-1],
            pinned=pinned,
            fallback=fallback,
        )
```

Add the module constant near the other assistant constants:

```python
#: The reply language when a profile declares none and nothing else decides.
#: This is CLDR's own answer for an unknown locale --
#: `Language.get("und").maximize()` is `en-Latn-US` -- rather than a preference
#: stated here. Region inference was considered and rejected: country is not
#: language, and a profile whose owner lives in Denmark may well prefer English.
DEFAULT_REPLY_LANGUAGE = "en"
```

- [ ] **Step 4: Wire the resolved path into the classifier method**

At the top of `_run_response_language_classifier`, before any prompt is built,
replace the existing gate block with:

```python
        # The resolution runs before anything is built: a resolved turn costs
        # neither the model call nor the prompt assembly. It applies only to a
        # real turn -- the eval harness runs with `self._run` None.
        self._response_language_gate_args = None
        resolution = None
        gate_started_at = None
        gate_started = None
        if self._run is not None:
            gate_started_at = datetime.now(UTC)
            gate_started = time.perf_counter()
            resolution = self._resolve_response_language(messages, profile)
        if resolution is not None:
            self._response_language_gate_args = {"gate": resolution.as_args()}
            if not resolution.ask and resolution.language:
                # The marker the renderers branch on, at the top level of
                # `args` beside `gate` — `db/assistant_log.py` and the
                # `_skipped` pane both read it from there. Without it a
                # resolved row renders as a call that was never made.
                self._response_language_gate_args["gate_replaced_call"] = True
                built = self._constructed_classification(
                    resolution.language, profile)
                self._response_language_classification = built
                self._reply_language_markdown = (
                    self._format_reply_language_markdown(built))
                self._response_language_classifier_meta = {
                    "duration_ms": int(
                        (time.perf_counter() - gate_started) * 1000),
                }
                self._record_response_language_classifier_step(
                    step_index=step_index,
                    phase="skipped",
                    reason=(
                        f"resolved to {built.languages[0].code} by detection; "
                        "no model was asked"
                    ),
                    observation_preview=json.dumps(
                        built.model_dump(), ensure_ascii=False, indent=1),
                    system_prompt=None,
                    user_prompt=None,
                    requested_at=gate_started_at,
                )
                return
```

Delete `_previous_room_classification` and `_profile_languages_changed`, and the
tests that exercise them: nothing is reused, so there is nothing to read back and
nothing to invalidate.

- [ ] **Step 5: Run the tests**

Run: `cd source && ./venv/bin/python -m pytest agents/test_assistant_response_language_gate.py -q`
Expected: PASS. Failures naming `_previous_room_classification` or
`_profile_languages_changed` mean a test was missed in step 4.

- [ ] **Step 6: Run the regression suite**

Run: `cd source && ./venv/bin/python -m pytest agents/ webapp/ db/ -q`
Expected: PASS. The switch is default off, so every existing test sees today's
behaviour; a failure here is a real regression.

- [ ] **Step 7: Commit**

```bash
git add source/agents/assistant.py source/agents/test_assistant_response_language_gate.py
git commit -m "feat(assistant): resolve the reply language without a model call"
```

---

### Task 6: The rendered row, and the guide's language line

**Files:**
- Modify: `source/webapp/assistant_components.py`
- Modify: `source/user_profile/formatting.py`
- Test: `source/webapp/test_assistant_log_view.py`
- Test: `source/user_profile/test_formatting.py`

**Interfaces:**
- Consumes: the `args["gate"]` shape from Task 3 (`ask`, `trigger`, `language`,
  `slots`, `named_language`, `detector_ms`).
- Produces: `format_formatting_guide(profile, now=None, *, has_history=True)`.

- [ ] **Step 1: Write the failing tests**

In `source/webapp/test_assistant_log_view.py`:

```python
def test_a_resolved_row_shows_what_it_read():
    """The row replaces a 9-18s model call, so it has to say which language it
    chose and which languages it chose between."""
    step = _step("response_language_classifier", at=_at(1), ms=12)
    step.phase = "skipped"
    step.duration_ms = 12
    # The marker sits at the TOP LEVEL of args, beside `gate` -- that is where
    # db/assistant_log.py:560 and the `_skipped` pane both read it.
    step.args = {
        "gate_replaced_call": True,
        "gate": {"ask": False, "trigger": "resolved", "language": "en-US",
                 "slots": ["en", "da"], "named_language": None,
                 "detector_ms": 11},
    }
    step.observation_preview = (
        '{"reason": "Resolved by detection.", "languages": ['
        '{"code": "en-US", "score": 5}]}')
    view = log_view(_run(), [step])
    event = next(e for e in view["events"] if e["kind"] == "skipped")
    assert event["duration_ms"] == 12
    assert "en-US" in event["detail_html"]
    assert "da" in event["detail_html"]
    assert "never made" not in event["detail_html"]
```

`_step(action, *, at, ms, phases=None, code_driven=True)`, `_run(finished=None)`
and `_at(seconds)` are that file's existing helpers; use them rather than adding
new ones.

In `source/user_profile/test_formatting.py`:

```python
def test_the_language_line_is_dropped_when_there_is_no_history():
    """With no conversation to mirror, `reply in the language of the current
    message` points at something unknowable and forbids the fallback."""
    profile = {"data": {"languages": {"rows": [
        {"tag": "en-US", "level": "native", "stance": "prefer", "note": ""},
    ]}}}
    with_history = format_formatting_guide(profile, has_history=True)
    without = format_formatting_guide(profile, has_history=False)
    assert "language of the current message" in with_history
    assert "language of the current message" not in without


def test_the_rest_of_the_guide_survives_without_history():
    """Only the language line is conditional; units, currency and the rest are
    not about the conversation."""
    profile = {"data": {"units": "metric", "languages": {"rows": [
        {"tag": "en-US", "level": "native", "stance": "prefer", "note": ""},
    ]}}}
    without = format_formatting_guide(profile, has_history=False)
    assert "Units" in without
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd source && ./venv/bin/python -m pytest webapp/test_assistant_log_view.py user_profile/test_formatting.py -q -k "resolved_row or no_history or survives"`
Expected: FAIL — `TypeError: format_formatting_guide() got an unexpected keyword argument 'has_history'`, and the log-view assertion.

- [ ] **Step 3: Make the guide's language line conditional**

In `source/user_profile/formatting.py`, change the signature and guard the
language line:

```python
def format_formatting_guide(profile: dict[str, Any],
                            now: datetime | None = None,
                            *, has_history: bool = True) -> str:
```

and wrap the existing language block:

```python
    language, secondary_language = valid_profile_languages(profile)
    if language is not None and has_history:
```

Add to that block's existing comment, in the file's own voice, that with no
conversation to mirror the line points at something unknowable and forbids the
preferred language the operator asked to fall back to.

Then find every caller of `format_formatting_guide` — `grep -rn
"format_formatting_guide" source/ --include=*.py` — and pass `has_history` from
the ones that know it. `build_formatting_guide` and any test caller keep the
default.

- [ ] **Step 4: Render the resolved row**

In `source/webapp/assistant_components.py`, the `_skipped` pane already branches
on `payload.get("gate_replaced_call")`. Extend the branch that renders a gate row
so it shows the resolved language and the slots it chose between, using the
file's existing `_block` helper and matching the surrounding style. Update that
pane's note so it describes a resolution rather than a reuse.

In `source/db/assistant_log.py`, the skipped-event builder copies `gate` and
`observation_preview` into the payload when the marker is set. Keep that; the
`gate` dict's contents changed but its position did not.

- [ ] **Step 5: Run the tests**

Run: `cd source && ./venv/bin/python -m pytest webapp/ user_profile/ -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add source/webapp/assistant_components.py source/user_profile/formatting.py source/webapp/test_assistant_log_view.py source/user_profile/test_formatting.py
git commit -m "feat(assistant): show what a resolved turn read, and drop the guide's language line without history"
```

---

### Task 7: Cap the profile's language list at six

**Files:**
- Modify: `source/db/profile_languages.py:23`
- Test: `source/db/test_profile_languages.py`

- [ ] **Step 1: Write the failing test**

Add to `source/db/test_profile_languages.py`, following that file's existing
patterns for building rows and asserting a validation error:

```python
def test_a_language_list_longer_than_the_cap_is_rejected():
    """Four languages reach the detector and the rest only inform the model on
    the rare turn it runs, so an unbounded list has no consumer."""
    rows = [{"tag": tag, "level": "fluent", "stance": "neutral", "note": ""}
            for tag in ("en", "da", "de", "fr", "es", "it", "nl")]
    with pytest.raises(ValueError):
        validate_language_rows(rows)


def test_a_language_list_at_the_cap_is_accepted():
    rows = [{"tag": tag, "level": "fluent", "stance": "neutral", "note": ""}
            for tag in ("en", "da", "de", "fr", "es", "it")]
    assert len(validate_language_rows(rows)) == 6
```

`validate_language_rows` is the real name. The module enforces the cap at
`source/db/profile_languages.py:172` and separately rejects a request carrying
more than `10 * MAX_LANGUAGE_ROWS` rows at line 69 — lowering the constant tightens
both, which is intended: sixty rows in one request is still far more than a
six-row list can store.

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd source && ./venv/bin/python -m pytest db/test_profile_languages.py -q -k cap`
Expected: FAIL — seven rows are accepted, because the cap is 100.

- [ ] **Step 3: Lower the cap**

In `source/db/profile_languages.py`:

```python
#: Four languages reach the detector and the rest only inform the classifier on
#: the rare turn it runs, so a longer list has no consumer -- and a list long
#: enough to page through is one nobody curates.
MAX_LANGUAGE_ROWS = 6
```

- [ ] **Step 4: Run the tests**

Run: `cd source && ./venv/bin/python -m pytest db/ -q`
Expected: PASS. No stored profile exceeds six, so nothing existing becomes
unsaveable.

- [ ] **Step 5: Check the UI states the limit**

Find where `/profile` renders the language rows — `grep -rn "MAX_LANGUAGE_ROWS"
source/webapp/ source/static/` — and make sure the limit the operator sees
matches. If the page hardcodes a number, change it; if it reads the constant,
nothing to do. Say which in your report.

- [ ] **Step 6: Commit**

```bash
git add source/db/profile_languages.py source/db/test_profile_languages.py
git commit -m "feat(profile): cap the declared language list at six"
```

---

### Task 8: Documentation

**Files:**
- Modify: `source/notes/assistant-design.md`
- Modify: `docs/superpowers/specs/2026-08-27-response-language-shift-gate-design.md`

- [ ] **Step 1: Rewrite the gate passage in the design notes**

`source/notes/assistant-design.md` has a "Gating the classifier" passage
describing the shift gate. Replace it with the resolution design, in that file's
own voice and heading style. Cover: the four slots and how they are filled; the
two detectors and why both are needed; that the answer is constructed rather than
stored, so a profile edit takes effect immediately with no invalidation; the
cold-start fallback and that it is scoped to the vacuum; and the five cases that
still call the model.

Describe how it works now. No migration notes, no "previously", no reference to
this plan.

- [ ] **Step 2: Mark the superseded spec**

Add a line under the shift-gate spec's `**Status:**` recording that it is
superseded by `docs/superpowers/specs/2026-08-30-response-language-resolution-design.md`,
and leave the rest of that document alone — its measurements are the reasoning
behind constants this design still uses.

- [ ] **Step 3: Commit**

```bash
git add source/notes/ docs/superpowers/specs/
git commit -m "docs(assistant): describe response-language resolution"
```

---

## After the plan

The switch is still `assistant.response_language_gate`, still default off. Turn
it on and read some runs: an ordinary turn should now show a sub-second
`resolved` row naming the language and the slots it chose between, where it
previously showed a 9-18s model call.

The measurement nobody has taken is still the one worth taking — the skip rate on
representative traffic. Every row records its trigger, so a week of ordinary use
answers it without new instrumentation.
