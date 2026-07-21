"""Tests for the assistant's context-invalidation marker: a one-time notice
posted into a room after a facts/Q&A change or a profile.current switch, so
the model re-checks profile-dependent assumptions instead of reusing an
earlier answer from the transcript. One marker checkpoints both event stamps;
legacy facts-only markers stay recognized."""
from uuid import UUID, uuid4

import pytest

import db
import user_profile
from agents.assistant import (
    AssistantAgent,
    FACTS_INVALIDATION_NOTICE,
    _demote_trailing_context_marker,
    _is_context_marker,
)
from agents.config import ASSISTANT_UUID

KEYS = ("profile.current", "qa.facts_invalidated_at", "profile.current_changed_at")


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    saved = {}
    for key in KEYS:
        row = db.db.session.query(db.AppSetting).filter_by(key=key).one_or_none()
        saved[key] = row.value if row is not None else None
    try:
        yield app
    finally:
        db.db.session.rollback()
        for key, value in saved.items():
            row = db.db.session.query(db.AppSetting).filter_by(key=key).one_or_none()
            if row is not None:
                row.value = value
        db.db.session.commit()
        ctx.pop()


@pytest.fixture
def room(app_ctx):
    human = db.get_human_user()
    room = db.create_chatroom(f"cm-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])
    db.post_chat_message(room.uuid, human.uuid, "hi")
    for key in KEYS:
        db.set_setting(key, None)
    try:
        yield room
    finally:
        db.db.session.rollback()
        db.db.session.query(db.ChatMessage).filter(
            db.ChatMessage.room_uuid == room.uuid).delete()
        db.db.session.query(db.Chatroom).filter(
            db.Chatroom.uuid == room.uuid).delete()
        db.db.session.commit()


def _agent():
    return AssistantAgent(agent_uuid=ASSISTANT_UUID, name="assistant",
                          send=lambda _: None)


def _markers(room_uuid):
    return [m for m in db.list_room_messages(room_uuid) if _is_context_marker(m)]


def _post(agent, room_uuid):
    """Capture a fresh context snapshot and run the marker check with it."""
    return agent._maybe_post_context_marker(
        room_uuid, user_profile.current_profile_context())


def _template_uuid(index=0):
    return db.profile_templates_entries()[index]["uuid"]


def _template_name(index=0):
    return db.profile_templates_entries()[index]["name"]


# ---- facts-only events (the legacy behavior, generalized meta) -------------

def test_facts_only_event_posts_generic_notice_once(room):
    agent = _agent()
    assert _post(agent, room.uuid) is False          # nothing pending
    assert _markers(room.uuid) == []

    stamp = db.mark_facts_invalidated()
    assert _post(agent, room.uuid) is True
    marks = _markers(room.uuid)
    assert len(marks) == 1
    assert marks[0]["kind"] == "message"
    assert marks[0]["text"] == FACTS_INVALIDATION_NOTICE
    meta = marks[0]["meta"]
    assert meta["context_invalidation"] is True
    assert meta["facts_invalidation"] == stamp
    assert meta["profile_context_changed"] is None
    assert meta["profile_switch_uuid"] is None

    assert _post(agent, room.uuid) is False          # same stamp → dedup
    assert len(_markers(room.uuid)) == 1

    db.mark_facts_invalidated()                      # a new event → a new marker
    assert _post(agent, room.uuid) is True
    assert len(_markers(room.uuid)) == 2


# ---- profile switches ------------------------------------------------------

def test_profile_switch_posts_tailored_notice(room):
    agent = _agent()
    stamp = db.set_current_profile(_template_uuid(0))
    assert _post(agent, room.uuid) is True
    marks = _markers(room.uuid)
    assert len(marks) == 1
    assert f"switched to {_template_name(0)}" in marks[0]["text"]
    assert "room history is preserved" in marks[0]["text"]
    meta = marks[0]["meta"]
    assert meta["facts_invalidation"] is None      # a switch never stamps facts
    assert meta["profile_context_changed"] == stamp
    assert meta["profile_switch_uuid"] == _template_uuid(0)
    # The same pair of stamps never posts twice in this room.
    assert _post(agent, room.uuid) is False
    assert len(_markers(room.uuid)) == 1


def test_profile_unset_posts_unset_notice(room):
    agent = _agent()
    db.set_current_profile(_template_uuid(0))
    _post(agent, room.uuid)
    db.set_current_profile(None)
    assert _post(agent, room.uuid) is True
    assert "the active profile was unset" in _markers(room.uuid)[-1]["text"]
    assert _markers(room.uuid)[-1]["meta"]["profile_switch_uuid"] is None


def test_several_switches_coalesce_to_latest_profile(room):
    agent = _agent()
    db.set_current_profile(_template_uuid(0))
    db.set_current_profile(_template_uuid(1))
    db.set_current_profile(_template_uuid(2))
    assert _post(agent, room.uuid) is True
    marks = _markers(room.uuid)
    assert len(marks) == 1                            # one marker, latest state
    assert f"switched to {_template_name(2)}" in marks[0]["text"]
    assert marks[0]["meta"]["profile_switch_uuid"] == _template_uuid(2)
    assert _post(agent, room.uuid) is False


def test_marker_label_with_special_characters_is_safe(room):
    """A profile whose name carries markup must land verbatim in the plain
    notice text (chat renders it as text; the marker is filtered from model
    history anyway)."""
    agent = _agent()
    name = 'Böse <script>"& profile'
    profile_uuid = uuid4()
    row = db.Profile(uuid=profile_uuid, name=name, folder_uuid=None, position=0)
    db.db.session.add(row)
    db.db.session.commit()
    try:
        db.set_current_profile(str(profile_uuid))
        assert _post(agent, room.uuid) is True
        assert f"switched to {name}." in _markers(room.uuid)[0]["text"]
    finally:
        db.set_current_profile(None)
        db.db.session.query(db.Profile).filter(
            db.Profile.uuid == profile_uuid).delete()
        db.db.session.commit()


# ---- combined causes -------------------------------------------------------

def test_profile_then_qa_before_room_runs_posts_one_combined_marker(room):
    agent = _agent()
    db.set_current_profile(_template_uuid(0))
    db.mark_facts_invalidated()                       # a distinct later event
    assert _post(agent, room.uuid) is True
    marks = _markers(room.uuid)
    assert len(marks) == 1
    text = marks[0]["text"]
    assert f"switched to {_template_name(0)}" in text
    assert "stored facts or the Q&A knowledge base also changed" in text
    assert _post(agent, room.uuid) is False           # both stamps checkpointed


def test_qa_then_profile_before_room_runs_posts_one_combined_marker(room):
    """A Q&A event followed by a switch must surface BOTH causes — the switch
    never absorbs a still-unacknowledged facts invalidation."""
    agent = _agent()
    stamp_facts = db.mark_facts_invalidated()
    db.set_current_profile(_template_uuid(1))
    assert _post(agent, room.uuid) is True
    marks = _markers(room.uuid)
    assert len(marks) == 1
    text = marks[0]["text"]
    assert f"switched to {_template_name(1)}" in text
    assert "stored facts or the Q&A knowledge base also changed" in text
    assert marks[0]["meta"]["facts_invalidation"] == stamp_facts
    assert _post(agent, room.uuid) is False        # both stamps checkpointed


def test_qa_change_after_acknowledged_switch_returns_to_generic_notice(room):
    agent = _agent()
    db.set_current_profile(_template_uuid(0))
    _post(agent, room.uuid)
    db.mark_facts_invalidated()                       # unrelated later Q&A event
    assert _post(agent, room.uuid) is True
    marks = _markers(room.uuid)
    assert len(marks) == 2
    assert marks[-1]["text"] == FACTS_INVALIDATION_NOTICE
    assert marks[-1]["meta"]["profile_switch_uuid"] is None


# ---- snapshot semantics ----------------------------------------------------

def test_marker_uses_captured_context_not_fresh_settings(room):
    """A switch committed AFTER the turn captured its context must not leak
    into this turn's marker — it applies on the next turn."""
    agent = _agent()
    context = user_profile.current_profile_context()   # captured: nothing pending
    db.set_current_profile(_template_uuid(0))          # committed after capture
    assert agent._maybe_post_context_marker(room.uuid, context) is False
    assert _markers(room.uuid) == []
    # The next turn's capture sees the complete new state.
    assert _post(agent, room.uuid) is True


def test_context_snapshot_reads_pointer_and_stamps_atomically(app_ctx):
    for key in KEYS:
        db.set_setting(key, None)
    stamp = db.set_current_profile(_template_uuid(0))
    context = user_profile.current_profile_context()
    assert context.profile_uuid == UUID(_template_uuid(0))
    assert context.profile is not None
    assert context.profile["name"] == _template_name(0)
    assert context.facts_invalidated_at is None    # a switch never stamps facts
    assert context.profile_changed_at == stamp
    db.set_current_profile(None)
    empty = user_profile.current_profile_context()
    assert empty.profile is None and empty.profile_uuid is None


# ---- legacy markers, demotion, filtering -----------------------------------

def test_legacy_facts_marker_still_acknowledges_its_stamp(room):
    agent = _agent()
    stamp = db.mark_facts_invalidated()
    # A room that already carries the pre-generalization marker shape.
    db.post_chat_message(room.uuid, ASSISTANT_UUID, FACTS_INVALIDATION_NOTICE,
                         kind="message", meta={"facts_invalidation": stamp})
    assert _post(agent, room.uuid) is False           # acked by the legacy marker


def test_is_context_marker_recognizes_both_shapes():
    assert _is_context_marker({"meta": {"context_invalidation": True,
                                        "facts_invalidation": None,
                                        "profile_context_changed": "s"}})
    assert _is_context_marker({"meta": {"facts_invalidation": "s"}})   # legacy
    assert not _is_context_marker({"meta": {}})
    assert not _is_context_marker({})


def test_demote_trailing_context_marker_keeps_operator_message_current():
    user = {"sender_type": "human", "text": "what is X?", "kind": "message", "meta": {}}
    marker = {"sender_type": "agent", "text": "notice", "kind": "message",
              "meta": {"context_invalidation": True,
                       "profile_context_changed": "2026-07-21T00:00:00+00:00"}}
    out = _demote_trailing_context_marker([user, marker])
    assert out[-1] is user           # operator message is Current
    assert out[-2] is marker         # marker demoted into history
    legacy = {"sender_type": "agent", "text": "notice", "kind": "message",
              "meta": {"facts_invalidation": "2026-07-06T00:00:00+00:00"}}
    out = _demote_trailing_context_marker([user, legacy])
    assert out[-1] is user


def test_demote_trailing_context_marker_noop_without_trailing_marker():
    a = {"sender_type": "human", "text": "a", "kind": "message", "meta": {}}
    b = {"sender_type": "agent", "text": "b", "kind": "message", "meta": {}}
    assert _demote_trailing_context_marker([a, b]) == [a, b]
