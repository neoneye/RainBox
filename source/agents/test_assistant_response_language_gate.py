"""Tests for the assistant's side of the response-language gate: the switch,
the previous-classification read, and the skipped step row.
"""

from uuid import uuid4

import pytest

import db
from agents.assistant import (
    AssistantAgent,
    ResponseLanguageClassification,
    ResponseLanguageItem,
)
from agents.config import ASSISTANT_UUID

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

EN_LONG = (
    "The margin rule is dropped because the window already supplies the "
    "stability that restriction was buying, and the letter floor keeps "
    "acknowledgements out of the comparison entirely."
)

# A short window message and a much longer, confidently-Danish request: long
# enough that its own letter weight would win the window vote outright if it
# were wrongly counted as part of its own window (see
# test_the_current_request_is_not_part_of_its_own_window below).
EN_SHORT = "The migration finished early this morning."
DA_LONG = (
    "Kan du lige tjekke om den her classifier stadig kalder LLM en paa "
    "hver eneste turn, og om svaret bliver gemt korrekt bagefter?"
)


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    yield app
    db.db.session.rollback()
    ctx.pop()


@pytest.fixture(autouse=True)
def _restore_gate_setting(app_ctx):
    """set_setting commits, so a test in this file that flips the gate
    changes it for every later test in the session and for the next run of
    the suite. Capture the row before each test and put it back after,
    regardless of what the test sets it to or how it fails."""
    row = db.db.session.query(db.AppSetting).filter_by(
        key="assistant.response_language_gate").one_or_none()
    saved = row.value if row is not None else None
    try:
        yield
    finally:
        db.db.session.rollback()
        row = db.db.session.query(db.AppSetting).filter_by(
            key="assistant.response_language_gate").one_or_none()
        if row is not None:
            row.value = saved
        db.db.session.commit()


def _agent() -> AssistantAgent:
    return AssistantAgent(
        agent_uuid=ASSISTANT_UUID, name="assistant", send=lambda _: None)


def test_the_gate_is_registered_and_defaults_off(app_ctx):
    """Default off: the gate ships dormant and the operator turns it on when
    they want to compare runs."""
    assert "assistant.response_language_gate" in db.SETTINGS
    setting = db.SETTINGS["assistant.response_language_gate"]
    assert setting.type == "bool"
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
    whatever language a failed call happened to leave behind.

    This test covers both `failed` and `skipped` phases: a later task will make
    the gate write real `skipped` rows that carry a valid, parseable classification
    in `observation_preview`. The `phase == "observed"` filter is what rejects them,
    not unparseable content."""
    # Test the `failed` phase
    _record_classification(
        room.uuid,
        ResponseLanguageClassification(
            reason="failed call",
            languages=[ResponseLanguageItem(code="en-GB", score=5)]),
        phase="failed")
    assert _agent()._previous_room_classification(room.uuid) is None

    # Clear the row for the next phase test
    db.db.session.query(db.AssistantStep).delete()
    db.db.session.query(db.AssistantRun).delete()
    db.db.session.commit()

    # Test the `skipped` phase with valid, parseable classification:
    # This proves the row is rejected on its phase, not on unparseable content.
    _record_classification(
        room.uuid,
        ResponseLanguageClassification(
            reason="skipped, reusing previous",
            languages=[ResponseLanguageItem(code="da", score=5)]),
        phase="skipped")
    assert _agent()._previous_room_classification(room.uuid) is None


def test_an_unparseable_row_is_treated_as_absent(room):
    """A row with invalid JSON in observation_preview is treated as absent.

    The `_previous_room_classification` method catches parse failures and returns
    None. This test exercises that path by writing an `observed` row whose
    `observation_preview` is not valid JSON — the only thing under test is the
    unparseable payload."""
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=ASSISTANT_UUID)
    db.append_assistant_step(
        run_uuid=run.uuid,
        step_index=0,
        phase="observed",
        action="response_language_classifier",
        reason="not JSON",
        observation_preview="this is not valid JSON",
        code_driven=True,
    )
    db.db.session.commit()

    assert _agent()._previous_room_classification(room.uuid) is None


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
    window it is compared against makes every turn look unchanged.

    `EN_SHORT` is short but well above the letter floor, and `DA_LONG` is long
    enough and confidently Danish enough that its own letter weight outvotes
    `EN_SHORT` outright. So if the request were wrongly folded into its own
    window — the regression this test exists to catch — the window would
    resolve to "da" with a high share of itself and the gate would reuse
    instead of asking: exactly the false-skip the docstring above warns about.
    Excluding it correctly, the window is `EN_SHORT` alone, dominant "en",
    against which the Danish request scores near zero and the gate asks.
    """
    db.set_setting("assistant.response_language_gate", True)
    _record_classification(room.uuid, ResponseLanguageClassification(
        reason="English conversation.",
        languages=[ResponseLanguageItem(code="en-GB", score=5)]))
    messages = [
        {"sender_type": "human", "text": EN_SHORT},
        {"sender_type": "human", "text": DA_LONG},
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
        # See test_the_current_request_is_not_part_of_its_own_window: this
        # message shifts language without naming one, so the gate's decision
        # comes from the window comparison this test is about, not the
        # cheaper named-language check.
        {"sender_type": "human", "text": DA_MESSAGES[2]["text"]},
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
