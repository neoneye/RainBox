"""Integration: the AssistantAgent renders identity + formatting guide from
ONE declared-profile context snapshot per turn and injects them in order
(identity → formatting_guide → operator_profile), with no per-turn
`profile.current` setting lookup on the handle path. The assembled prompt is
captured by stubbing the model call (_structured_completion)."""

from uuid import uuid4

import pytest

import db
from agents.assistant import AssistantActionName, AssistantAgent, AssistantStepDecision
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
    room = db.create_chatroom(f"fg-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])
    db.post_chat_message(room.uuid, human.uuid, "how far is 100 km?")
    try:
        yield room
    finally:
        db.db.session.rollback()
        db.db.session.query(db.AssistantRun).filter(
            db.AssistantRun.room_uuid == room.uuid).delete()
        db.db.session.query(db.ChatMessage).filter(
            db.ChatMessage.room_uuid == room.uuid).delete()
        db.db.session.query(db.Chatroom).filter(
            db.Chatroom.uuid == room.uuid).delete()
        db.db.session.commit()


def _germany_uuid():
    return next(e for e in db.profile_templates_entries()
                if e["name"] == "Germany")["uuid"]


def _run_capture(room):
    agent = AssistantAgent(agent_uuid=ASSISTANT_UUID, name="assistant",
                           send=lambda _: None)
    captured = {}

    def fake_completion(*, system_prompt, user_prompt, response_model, validator=None):
        captured["system_prompt"] = system_prompt
        captured["user_prompt"] = user_prompt
        return AssistantStepDecision(
            reason="answer", action=AssistantActionName.REPLY,
            args={"message": "ok"})

    agent._structured_completion = fake_completion
    agent.handle(uuid4(), {"room_uuid": str(room.uuid)})
    return captured


def test_formatting_guide_injected_after_identity(room):
    db.set_current_profile(_germany_uuid())
    prompt = _run_capture(room)["user_prompt"]
    assert '<operator_identity authority="context"' in prompt
    assert '<formatting_guide authority="instructions">' in prompt
    assert "Use these defaults unless the current request" in prompt
    assert "- Numbers: decimal comma with point grouping" in prompt
    assert prompt.index("<operator_identity") < prompt.index("<formatting_guide")
    # The switch marker itself is filtered from model history.
    assert "switched to Germany" not in prompt


def test_unset_profile_emits_neither_block(room):
    db.set_current_profile(None)
    prompt = _run_capture(room)["user_prompt"]
    assert "<operator_identity" not in prompt
    assert "<formatting_guide" not in prompt


def test_handle_path_never_rereads_profile_current(room, monkeypatch):
    """The one-snapshot seam owns the lookup: get_setting("profile.current")
    must not run anywhere on the handle path."""
    db.set_current_profile(_germany_uuid())
    seen: list[str] = []
    real = db.get_setting

    def spy(key):
        seen.append(key)
        return real(key)

    monkeypatch.setattr(db, "get_setting", spy)
    import agents.assistant as assistant_mod
    monkeypatch.setattr(assistant_mod.db, "get_setting", spy)
    prompt = _run_capture(room)["user_prompt"]
    assert "<formatting_guide" in prompt              # blocks still rendered
    assert "profile.current" not in seen
    assert "profile.current_changed_at" not in seen
    assert "qa.facts_invalidated_at" not in seen


def test_formatting_failure_empties_only_its_block(room, monkeypatch):
    db.set_current_profile(_germany_uuid())
    import agents.assistant as assistant_mod

    def boom(profile):
        raise RuntimeError("renderer exploded")

    monkeypatch.setattr(assistant_mod.user_profile, "format_formatting_guide", boom)
    prompt = _run_capture(room)["user_prompt"]
    assert "<operator_identity" in prompt             # identity unaffected
    assert "<formatting_guide" not in prompt


def test_system_prompt_names_the_new_blocks(room):
    db.set_current_profile(None)
    system = _run_capture(room)["system_prompt"]
    assert "formatting_guide" in system
    assert "knowledge_calibration" in system
    assert 'authority="context"' in system            # non-executable policy
    assert "not an audience boundary" in system


def test_profile_switch_field_changes_only_its_directive(room):
    """Counterfactual: switching Germany → US changes the formatting guide's
    directives, while the guide's code-owned frame stays identical."""
    db.set_current_profile(_germany_uuid())
    german = _run_capture(room)["user_prompt"]
    us_uuid = next(e for e in db.profile_templates_entries()
                   if e["name"] == "US")["uuid"]
    db.set_current_profile(us_uuid)
    # The switch marker posts on the next turn; capture again.
    american = _run_capture(room)["user_prompt"]
    assert "decimal comma with point grouping" in german
    assert "decimal point with comma grouping" in american
    assert "MM/DD/YYYY" in american and "DD.MM.YYYY" in german
