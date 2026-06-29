"""Regression corpus: rejected wrong facts must never reappear active via the
model write path. Each wrong fact is first recorded by an explicit human command
then immediately rejected (writing a tombstone). A subsequent model-inferred write
attempt for the same fact must be refused (outcome == "refused_tombstone").

UUID fix vs brief: brief had created_by_uuid="a" which Postgres rejects for UUID
columns. Replaced with str(uuid4()) stub per the implementation instructions.
"""
import pytest
from uuid import uuid4
import db
from db import MemoryClaim, MemoryEvidence
from db.models import MemoryRejectedValue

WRONG = ["xa is wrong", "yb prefers poison", "zc uses malware"]

# Model-path evidence — source_type="chat_message" requires source_id, excerpt,
# AND created_by_uuid (all three mandatory per evidence matrix). created_by_uuid
# must be a valid UUID string; the brief had "a" which Postgres rejects.
_STUB_USER_UUID = str(uuid4())
MEV = {
    "provenance": "inferred_by_model",
    "source_type": "chat_message",
    "source_id": "stub-message-id",
    "excerpt": "model excerpt",
    "created_by_uuid": _STUB_USER_UUID,
}

# Human-path evidence — source_type="manual" only requires excerpt.
EV = {"provenance": "confirmed_by_user", "source_type": "manual", "excerpt": "x"}


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    try:
        yield app
    finally:
        ctx.pop()


def test_rejected_wrong_facts_never_reappear_via_model(app_ctx):
    room = uuid4()
    # Phase 1: record each wrong fact as a human, then reject it (writes tombstone).
    for text in WRONG:
        c = db.record_belief(
            actor="explicit_human_command",
            scope="room",
            kind="fact",
            text=text,
            confidence=1.0,
            room_uuid=room,
            evidence=EV,
        ).claim
        db.reject_memory(c.uuid, {"provenance": "confirmed_by_user",
                                  "source_type": "manual", "excerpt": "no"})

    # Phase 2: the model tries to re-learn each wrong fact — must be refused.
    for text in WRONG:
        r = db.record_belief(
            actor="model_inferred",
            scope="room",
            kind="fact",
            text=text,
            confidence=0.9,
            room_uuid=room,
            evidence=MEV,
        )
        assert r.outcome == "refused_tombstone", (
            f"Expected refused_tombstone for {text!r}, got {r.outcome!r}"
        )

    # Phase 3: no active claims should remain in the marker room.
    active = db.db.session.query(MemoryClaim).filter_by(
        room_uuid=room, status="active"
    ).count()
    assert active == 0

    # Teardown: remove every row this test created so the shared sandbox DB
    # stays clean. Order matters — evidence first (FK to claim), then claims,
    # then tombstones (keyed by room_uuid).
    db.db.session.query(MemoryEvidence).filter(
        MemoryEvidence.memory_uuid.in_(
            db.db.session.query(MemoryClaim.uuid).filter_by(room_uuid=room)
        )
    ).delete(synchronize_session=False)
    db.db.session.query(MemoryClaim).filter_by(room_uuid=room).delete()
    db.db.session.query(MemoryRejectedValue).filter_by(room_uuid=room).delete()
    db.db.session.commit()
