"""The persona block: a room's persona reaches the assistant's turn prompt,
ranks below the request, and is absent when no persona is linked."""
from dataclasses import replace
from uuid import UUID, uuid4

import pytest

import db
from agents.assistant import (
    ACCEPTANCE_CRITERIA_SOURCE_PRIORITY_SECTION,
    ASSISTANT_SYSTEM_PROMPT,
    SOURCE_PRIORITY_SECTION,
)


@pytest.fixture
def ctx():
    a = db.make_app()
    db.init_db(a)
    c = a.app_context()
    c.push()
    try:
        yield
    finally:
        c.pop()


def test_both_source_priority_variants_rank_the_persona():
    for section in (SOURCE_PRIORITY_SECTION,
                    ACCEPTANCE_CRITERIA_SOURCE_PRIORITY_SECTION):
        assert "persona" in section, section
    # Ranks stay dense and ordered in both variants.
    for section in (SOURCE_PRIORITY_SECTION,
                    ACCEPTANCE_CRITERIA_SOURCE_PRIORITY_SECTION):
        ranks = [int(line.split('rank="')[1].split('"')[0])
                 for line in section.splitlines() if 'rank="' in line]
        assert ranks == list(range(1, len(ranks) + 1)), section


def test_system_prompt_states_the_persona_boundary():
    assert "A persona changes voice and manner" in ASSISTANT_SYSTEM_PROMPT
    assert "never changes which actions are available" in ASSISTANT_SYSTEM_PROMPT


def test_persona_section_renders_when_the_room_links_one(ctx):
    from agents.assistant import AssistantAgent

    agent = AssistantAgent(agent_uuid=uuid4(), name="assistant", send=lambda m: None)
    agent._persona_block = "Dry, concrete, allergic to filler."
    prompt = agent._build_user_prompt(messages=[{"text": "hi", "sender_type": "human"}],
                                      scratchpad=[], step_index=0)
    assert '<persona authority="voice">' in prompt
    assert "Dry, concrete, allergic to filler." in prompt


def test_no_persona_section_when_unset(ctx):
    from agents.assistant import AssistantAgent

    agent = AssistantAgent(agent_uuid=uuid4(), name="assistant", send=lambda m: None)
    prompt = agent._build_user_prompt(messages=[{"text": "hi", "sender_type": "human"}],
                                      scratchpad=[], step_index=0)
    assert "<persona" not in prompt


def test_turn_log_records_the_persona_and_its_revision(ctx):
    from agents.assistant import AssistantAgent

    from agents.config import ASSISTANT_UUID

    p = db.persona_create(f"LogP-{uuid4().hex[:8]}", None)
    pu = UUID(p["uuid"])
    db.persona_update_content(pu, "log voice")
    room = db.create_chatroom(f"logroom-{uuid4().hex[:8]}",
                              db.get_human_user().uuid, [ASSISTANT_UUID])
    try:
        db.set_member_persona(room.uuid, ASSISTANT_UUID, persona_uuid=pu)
        resolution = db.resolve_member_persona(room.uuid, ASSISTANT_UUID)
        entries = AssistantAgent._build_turn_log(
            db.user_profile_context_stub() if hasattr(db, "user_profile_context_stub")
            else _profile_context(), False, False, resolution)
        persona_entry = next(e for e in entries if e["label"] == "persona")
        assert persona_entry["text"] == p["name"]
        assert persona_entry["href"] == f"/persona?id={p['uuid']}"
        assert persona_entry["revision"] == str(resolution.revision_uuid)
    finally:
        db.delete_chatroom(room.uuid)
        db.persona_delete(pu)


def _profile_context():
    """The turn log's first argument — a profile context with nothing selected."""
    import user_profile
    return user_profile.ProfileContext(profile_uuid=None, profile=None)


@pytest.fixture
def persona_room(ctx):
    """A chatroom with the assistant, a linked persona, and one message —
    for driving a full agent.handle() run through the mid-run criteria
    refresh path."""
    from agents.config import ASSISTANT_UUID

    human = db.get_human_user()
    room = db.create_chatroom(
        f"persona-refresh-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])
    db.post_chat_message(room.uuid, human.uuid,
                          "remember that I prefer en-US")
    persona = db.persona_create(f"RefreshPersona-{uuid4().hex[:8]}", None)
    persona_uuid = UUID(persona["uuid"])
    db.persona_update_content(persona_uuid, "Dry, concrete, allergic to filler.")
    db.set_member_persona(room.uuid, ASSISTANT_UUID, persona_uuid=persona_uuid)
    try:
        yield room, persona_uuid, persona["name"]
    finally:
        db.db.session.rollback()
        db.db.session.query(db.AssistantRun).filter(
            db.AssistantRun.room_uuid == room.uuid).delete()
        db.db.session.query(db.ChatMessage).filter(
            db.ChatMessage.room_uuid == room.uuid).delete()
        db.db.session.query(db.Chatroom).filter(
            db.Chatroom.uuid == room.uuid).delete()
        db.db.session.commit()
        db.persona_delete(persona_uuid)


def test_persona_survives_a_mid_run_criteria_refresh(persona_room, monkeypatch):
    """A flagged preference write triggers `_refresh_acceptance_criteria`
    mid-run (agents/assistant.py ~line 3326: a successful non-confirm write
    whose capability sets `revises_acceptance_criteria`). The rebuilt turn
    log must still name the room's persona — the prompt still carries it,
    so a log entry of "(none)" here would be the debugging log lying about
    exactly what it exists to show."""
    import agents.assistant as assistant_module
    from agents.assistant import (
        AcceptanceCriteria,
        AssistantActionName,
        AssistantAgent,
        AssistantObservation,
        AssistantStepDecision,
    )
    from agents.config import ASSISTANT_UUID

    room, persona_uuid, persona_name = persona_room

    # Flag a write capability as revising acceptance criteria, exactly as
    # test_assistant_acceptance_criteria.py's
    # test_flagged_write_refreshes_criteria_and_replaces_the_section does.
    caps = dict(assistant_module.enabled_capabilities())
    cap = caps[AssistantActionName.MEMORY_REMEMBER]
    caps[AssistantActionName.MEMORY_REMEMBER] = replace(
        cap, revises_acceptance_criteria=True,
        action=lambda ctx, args: AssistantObservation(
            ok=True, text="preference updated", data={"noop": True}))
    monkeypatch.setattr(assistant_module, "enabled_capabilities", lambda: caps)

    agent = AssistantAgent(
        agent_uuid=ASSISTANT_UUID, name="assistant", send=lambda _: None)

    criteria_queue = [
        AcceptanceCriteria(processing="step0", formatting="f", assumptions="a"),
        AcceptanceCriteria(processing="refreshed", formatting="f", assumptions="a"),
    ]

    def fake_criteria(*, system_prompt, user_prompt):
        return criteria_queue.pop(0)

    agent._request_acceptance_criteria = fake_criteria

    decisions = [
        AssistantStepDecision(
            reason="store the preference",
            action=AssistantActionName.MEMORY_REMEMBER,
            args={"text": "preferred response language is en-US"}),
        AssistantStepDecision(
            reason="ready to answer", action=AssistantActionName.REPLY,
            args={"message": "Noted."}),
    ]

    def fake_completion(*, system_prompt, user_prompt, response_model,
                        validator=None):
        return decisions.pop(0)

    agent._structured_completion = fake_completion

    result = agent.handle(uuid4(), {"room_uuid": str(room.uuid)})
    assert result["status"] == "finished"

    rows = (
        db.db.session.query(db.AssistantStep)
        .filter(db.AssistantStep.run_uuid == result["assistant_run_uuid"])
        .order_by(db.AssistantStep.id)
        .all()
    )

    def persona_text(row):
        entries = row.log or []
        entry = next((e for e in entries if e["label"] == "persona"), None)
        assert entry is not None, f"no persona log entry on {row.action} row"
        return entry["text"]

    # The refresh's own code-driven criteria row (built from the rebuilt log)
    # and every step row after it still name the persona.
    refresh_row = next(
        s for s in rows
        if s.action == "acceptance_criteria" and s.code_driven
        and s.reason == "refreshed after memory_remember (code-driven)")
    assert persona_text(refresh_row) == persona_name

    reply_row = next(s for s in rows if s.action == "reply")
    assert persona_text(reply_row) == persona_name
