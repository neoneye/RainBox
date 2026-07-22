"""assistant._action_remember creates a candidate (not active) and populates
evidence with source_id + excerpt — trust-hardening spec §3.1."""

import pytest
from uuid import uuid4

import db
from db import AssistantRun, MemoryClaim
from db.models import MemoryEvidence


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


def _cleanup(room):
    db.db.session.query(MemoryEvidence).filter(
        MemoryEvidence.memory_uuid.in_(
            db.db.session.query(MemoryClaim.uuid).filter_by(room_uuid=room)
        )
    ).delete(synchronize_session=False)
    db.db.session.query(MemoryClaim).filter_by(room_uuid=room).delete()
    db.db.session.commit()


def test_action_remember_creates_candidate_with_evidence(app_ctx):
    """_action_remember must create a candidate (not active) with source_id and
    excerpt populated — spec §3.1 (assistant_interpreted actor)."""
    from agents.assistant import _action_remember, AssistantActionContext

    room = uuid4()
    agent = uuid4()
    message = uuid4()
    ctx = AssistantActionContext(
        journal_id=None,
        room_uuid=room,
        agent_uuid=agent,
        step_index=0,
        message_uuid=message,
    )
    try:
        obs = _action_remember(ctx, {"text": "frank uses vim"})
        assert obs.ok is True

        claim = db.db.session.query(MemoryClaim).filter_by(room_uuid=room).first()
        assert claim is not None, "no MemoryClaim was created"
        assert claim.status == "candidate", (
            f"expected 'candidate', got {claim.status!r} — "
            "assistant_interpreted actor must not write active"
        )

        ev = (db.db.session.query(MemoryEvidence)
              .filter_by(memory_uuid=claim.uuid).first())
        assert ev is not None, "no MemoryEvidence was created"
        assert ev.source_id is not None and ev.source_id != "", (
            "evidence.source_id must be the triggering message uuid"
        )
        assert ev.excerpt and ev.excerpt != "", (
            "evidence.excerpt must carry the remembered text"
        )
    finally:
        _cleanup(room)


def test_handle_wires_message_uuid_into_evidence_source_id(app_ctx):
    """When handle() receives message_uuid in the payload (as chat_api enqueues
    it), the evidence source_id on the created candidate must equal that UUID —
    proving the field is no longer inert dead code."""
    from agents.assistant import AssistantActionContext, AssistantActionName, AssistantAgent, AssistantStepDecision
    from agents.assistant_fakes import scripted_decisions
    from agents.config import ASSISTANT_UUID

    def _agent():
        return AssistantAgent(agent_uuid=ASSISTANT_UUID, name="assistant", send=lambda _: None)

    def _decision(action, **args):
        if action is AssistantActionName.REPLY and "message" in args:
            args = {"1_specification": "en, metric", "2_message": args.pop("message"), "3_audit": "OK", **args}
        return AssistantStepDecision(reason="step", action=action, args=args)

    human = db.get_human_user()
    assert human is not None
    text = f"wired-test fact {uuid4().hex[:6]}"
    message = uuid4()
    room = db.create_chatroom(f"wire-test-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])
    try:
        agent = _agent()
        agent._decide_next_step = scripted_decisions(
            _decision(AssistantActionName.MEMORY_REMEMBER, text=text),
            _decision(AssistantActionName.REPLY, message="Noted."),
        )
        agent.handle(uuid4(), {"room_uuid": str(room.uuid), "message_uuid": str(message)})

        claim = db.db.session.query(MemoryClaim).filter(
            MemoryClaim.room_uuid == room.uuid,
            MemoryClaim.text == text,
        ).first()
        assert claim is not None, "no MemoryClaim was created"

        ev = db.db.session.query(MemoryEvidence).filter_by(memory_uuid=claim.uuid).first()
        assert ev is not None, "no MemoryEvidence was created"
        assert ev.source_id == str(message), (
            f"expected source_id={message!s}, got {ev.source_id!r} — "
            "message_uuid must be wired through handle() into the evidence"
        )
        assert ev.source_type == "chat_message"
    finally:
        _cleanup(room.uuid)
        db.db.session.query(AssistantRun).filter(
            AssistantRun.room_uuid == room.uuid
        ).delete()
        db.db.session.query(db.Chatroom).filter(db.Chatroom.uuid == room.uuid).delete()
        db.db.session.commit()
