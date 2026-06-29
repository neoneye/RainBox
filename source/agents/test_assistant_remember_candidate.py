"""assistant._action_remember creates a candidate (not active) and populates
evidence with source_id + excerpt — trust-hardening spec §3.1."""

import pytest
from uuid import uuid4

import db
from db import MemoryClaim
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
