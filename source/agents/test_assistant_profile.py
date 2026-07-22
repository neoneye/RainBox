"""Integration: the AssistantAgent injects the user-profile block (active
self-model memory) into its prompt, before the skills block.

Like the skills integration test, we capture the assembled prompt by stubbing
the model call (_structured_completion) rather than the decision seam.
"""

from uuid import uuid4

import pytest

import db
from db import AssistantRun, MemoryClaim, RetrievalEvent
import skills.loader as skills_loader
from agents.assistant import AssistantActionName, AssistantAgent, AssistantStepDecision
from agents.config import ASSISTANT_UUID


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    try:
        yield app
    finally:
        db.db.session.rollback()
        ctx.pop()


def _write(d, name, frontmatter, body):
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(f"---\n{frontmatter}\n---\n{body}\n", encoding="utf-8")


def test_profile_block_injected_before_skills(app_ctx, tmp_path, monkeypatch):
    tag = f"profile-test-{uuid4()}"
    # An active operator preference (the profile) and an active skill that both
    # land in the prompt; the profile must come first.
    db.create_memory_claim(
        scope="global", kind="preference", text="prefers concise replies",
        confidence=0.9, status="active", sensitivity="public", subject=tag,
    )
    # A candidate (unreviewed) preference must never reach the prompt.
    db.create_memory_claim(
        scope="global", kind="preference", text="unconfirmed secret habit",
        confidence=0.9, status="candidate", sensitivity="public", subject=tag,
    )
    _write(
        tmp_path, "active.md",
        "id: widget-active\nstatus: active\ncreated_by: human\nretrieval_tags: [widget]",
        "# Widget how-to\n\nInspect widgets carefully before answering.",
    )
    monkeypatch.setattr(skills_loader, "SKILLS_DIR", tmp_path)

    human = db.get_human_user()
    assert human is not None
    chatroom = db.create_chatroom(f"prof-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])
    db.post_chat_message(chatroom.uuid, human.uuid, "tell me about the widget")

    agent = AssistantAgent(agent_uuid=ASSISTANT_UUID, name="assistant", send=lambda _: None)
    captured: dict = {}

    def fake_completion(*, system_prompt, user_prompt, response_model, validator=None):
        captured["user_prompt"] = user_prompt
        return AssistantStepDecision(
            reason="answer", action=AssistantActionName.REPLY, args={"message": "ok", "audit": "OK"}
        )

    agent._structured_completion = fake_completion

    try:
        agent.handle(uuid4(), {"room_uuid": str(chatroom.uuid)})
        prompt = captured["user_prompt"]
        assert "About the operator" in prompt           # profile injected
        assert "prefers concise replies" in prompt
        assert "unconfirmed secret habit" not in prompt  # candidate is inert
        assert "Widget how-to" in prompt                 # skill still injected
        # Profile (who you are) comes before skills (how to do the task).
        assert prompt.index("About the operator") < prompt.index("Relevant skills")
    finally:
        db.db.session.query(AssistantRun).filter(
            AssistantRun.room_uuid == chatroom.uuid
        ).delete()
        claims = db.db.session.query(MemoryClaim).filter(
            MemoryClaim.subject == tag
        ).all()
        for c in claims:
            db.db.session.query(RetrievalEvent).filter(
                RetrievalEvent.target_id == str(c.uuid)
            ).delete()
        db.db.session.query(MemoryClaim).filter(MemoryClaim.subject == tag).delete()
        db.db.session.query(RetrievalEvent).filter(
            RetrievalEvent.target_id == "widget-active"
        ).delete()
        db.db.session.query(db.Chatroom).filter(
            db.Chatroom.uuid == chatroom.uuid
        ).delete()
        db.db.session.commit()
