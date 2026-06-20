"""Integration: the AssistantAgent injects retrieved *active* skills into its
prompt, and a candidate (model-written, unreviewed) skill never influences a
turn — the "candidates are inert" contract, proven end to end.

The real _decide_next_step builds the prompt; we capture it by stubbing the
model call (_structured_completion) rather than the decision seam.
"""

from uuid import uuid4

import pytest

import db
from db import AssistantRun, RetrievalEvent
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


def test_active_skill_injected_candidate_inert(app_ctx, tmp_path, monkeypatch):
    # A skills dir with one active and one candidate skill, both matching.
    _write(
        tmp_path, "active.md",
        "id: widget-active\nstatus: active\ncreated_by: human\nretrieval_tags: [widget]",
        "# Widget how-to\n\nInspect widgets carefully before answering.",
    )
    _write(
        tmp_path, "candidate.md",
        "id: widget-candidate\nstatus: candidate\ncreated_by: assistant\nretrieval_tags: [widget]",
        "# Widget candidate\n\nUnreviewed widget guidance that must not be used.",
    )
    monkeypatch.setattr(skills_loader, "SKILLS_DIR", tmp_path)

    human = db.get_human_user()
    assert human is not None
    chatroom = db.create_chatroom(f"skill-test-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])
    db.post_chat_message(chatroom.uuid, human.uuid, "tell me about the widget")

    agent = AssistantAgent(agent_uuid=ASSISTANT_UUID, name="assistant", send=lambda _: None)
    captured: dict = {}

    def fake_completion(*, system_prompt, user_prompt, response_model, validator=None):
        captured["user_prompt"] = user_prompt
        return AssistantStepDecision(
            reason="answer", action=AssistantActionName.REPLY, args={"message": "ok"}
        )

    agent._structured_completion = fake_completion

    try:
        agent.handle(0, {"room_uuid": str(chatroom.uuid)})
        prompt = captured["user_prompt"]
        assert "Widget how-to" in prompt          # active skill injected
        assert "Inspect widgets carefully" in prompt
        assert "Widget candidate" not in prompt    # candidate is inert
        assert "must not be used" not in prompt
    finally:
        db.db.session.query(AssistantRun).filter(
            AssistantRun.room_uuid == chatroom.uuid
        ).delete()
        db.db.session.query(RetrievalEvent).filter(
            RetrievalEvent.target_id.in_(["widget-active", "widget-candidate"])
        ).delete()
        db.db.session.query(db.Chatroom).filter(
            db.Chatroom.uuid == chatroom.uuid
        ).delete()
        db.db.session.commit()
