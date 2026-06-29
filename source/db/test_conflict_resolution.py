# db/test_conflict_resolution.py
import pytest
from uuid import uuid4
import db
from db.memory import record_belief, resolve_conflict
from db import MemoryClaim, MemoryEvidence
from db.models import MemoryRejectedValue

EV = {"provenance": "confirmed_by_user", "source_type": "manual", "excerpt": "x"}
_MEV_USER = "00000000-0000-0000-0000-000000000001"
MEV = {"provenance": "inferred_by_model", "source_type": "chat_message",
       "source_id": "m", "excerpt": "e", "created_by_uuid": _MEV_USER}


@pytest.fixture
def app_ctx():
    app = db.make_app(); db.init_db(app)
    ctx = app.app_context(); ctx.push()
    try:
        yield app
    finally:
        ctx.pop()


def _cleanup(room):
    db.db.session.query(MemoryEvidence).filter(MemoryEvidence.memory_uuid.in_(
        db.db.session.query(MemoryClaim.uuid).filter_by(room_uuid=room))).delete(
        synchronize_session=False)
    db.db.session.query(MemoryClaim).filter_by(room_uuid=room).delete()
    db.db.session.query(MemoryRejectedValue).filter_by(room_uuid=room).delete()
    db.db.session.commit()


def _candidate(room):
    record_belief(actor="explicit_human_command", scope="room", kind="preference",
                  text="dee prefers tea", confidence=1.0, room_uuid=room,
                  subject="dee", predicate="prefers", object="tea", evidence=EV)
    b = record_belief(actor="model_inferred", scope="room", kind="preference",
                      text="dee prefers coffee", confidence=0.6, room_uuid=room,
                      subject="dee", predicate="prefers", object="coffee", evidence=MEV)
    assert b.outcome == "conflict_candidate"
    return b.claim


def test_supersede(app_ctx):
    room = uuid4(); cand = _candidate(room)
    rival_uuid = cand.conflicts_with_uuid
    out = resolve_conflict(cand.uuid, "supersede")
    assert out.status == "active" and out.conflicts_with_uuid is None
    assert db.get_memory_claim(rival_uuid).status == "superseded"
    _cleanup(room)


def test_reject_tombstones_candidate(app_ctx):
    room = uuid4(); cand = _candidate(room)
    out = resolve_conflict(cand.uuid, "reject")
    assert out.status == "rejected"
    assert db.check_tombstone("room", room, None, cand.subj_pred_key,
                              cand.value_key) is not None
    _cleanup(room)


def test_not_conflict_activates_both(app_ctx):
    room = uuid4(); cand = _candidate(room)
    rival_uuid = cand.conflicts_with_uuid
    out = resolve_conflict(cand.uuid, "not_conflict")
    assert out.status == "active" and out.conflicts_with_uuid is None
    assert db.get_memory_claim(rival_uuid).status == "active"
    assert db.check_tombstone("room", room, None, cand.subj_pred_key,
                              cand.value_key) is None
    _cleanup(room)


def test_resolution_noop_when_already_resolved(app_ctx):
    room = uuid4(); cand = _candidate(room)
    resolve_conflict(cand.uuid, "reject")
    out = resolve_conflict(cand.uuid, "supersede")   # stale: already rejected
    assert out.status == "rejected"                  # unchanged, no exception
    _cleanup(room)


# ---------------------------------------------------------------------------
# P2: resolve_conflict supersede must set supersedes_uuid lineage
# ---------------------------------------------------------------------------

def test_supersede_sets_supersedes_uuid_on_candidate(app_ctx):
    """After resolve_conflict(cand, 'supersede'), the activated candidate must
    have cand.supersedes_uuid == rival.uuid so lineage is preserved in the UI
    (memory_claim_detail / supersede_memory rely on supersedes_uuid)."""
    room = uuid4()
    cand = _candidate(room)
    rival_uuid = cand.conflicts_with_uuid
    assert rival_uuid is not None, "Test setup error: candidate has no conflicts_with_uuid"

    out = resolve_conflict(cand.uuid, "supersede")

    db.db.session.expire_all()
    refreshed = db.get_memory_claim(cand.uuid)
    assert refreshed is not None
    assert refreshed.status == "active", f"Expected active, got {refreshed.status!r}"
    assert refreshed.supersedes_uuid == rival_uuid, (
        f"Expected supersedes_uuid={rival_uuid}, got {refreshed.supersedes_uuid!r}"
    )
    _cleanup(room)
