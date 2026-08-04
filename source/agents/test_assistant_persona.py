"""The persona block: a room's persona reaches the assistant's turn prompt,
ranks below the request, and is absent when no persona is linked."""
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
