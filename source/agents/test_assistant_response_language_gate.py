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
    whatever language a failed call happened to leave behind."""
    _record_classification(
        room.uuid,
        ResponseLanguageClassification(
            reason="never ran",
            languages=[ResponseLanguageItem(code="en-GB", score=5)]),
        phase="failed")
    assert _agent()._previous_room_classification(room.uuid) is None
