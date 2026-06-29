# db/test_record_belief.py
import pytest
from uuid import uuid4
import db
from db.memory import record_belief, validate_evidence
from db import MemoryClaim, MemoryEvidence
from db.models import MemoryRejectedValue

EV = {"provenance": "confirmed_by_user", "source_type": "manual", "excerpt": "x"}


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


def test_create_human_goes_active(app_ctx):
    room = uuid4()
    r = record_belief(actor="explicit_human_command", scope="room", kind="fact",
                      text="alice is happy", confidence=1.0, room_uuid=room, evidence=EV)
    assert r.outcome == "created" and r.claim.status == "active"
    assert r.claim.subj_pred_key and r.claim.key_version == db.KEY_VERSION
    _cleanup(room)


def test_create_model_is_candidate(app_ctx):
    room = uuid4()
    r = record_belief(actor="model_inferred", scope="room", kind="fact",
                      text="zeta is new", confidence=0.6, room_uuid=room,
                      evidence={"provenance": "inferred_by_model",
                                "source_type": "chat_message", "source_id": str(uuid4()),
                                "excerpt": "e", "created_by_uuid": str(uuid4())})
    assert r.outcome == "created" and r.claim.status == "candidate"
    _cleanup(room)


def test_dedupe_corroborates(app_ctx):
    room = uuid4()
    a = record_belief(actor="explicit_human_command", scope="room", kind="fact",
                      text="dup fact", confidence=1.0, room_uuid=room, evidence=EV)
    b = record_belief(actor="explicit_human_command", scope="room", kind="fact",
                      text="dup fact", confidence=1.0, room_uuid=room, evidence=EV)
    assert b.outcome == "corroborated" and b.claim.uuid == a.claim.uuid
    assert b.claim.support_count == 2
    _cleanup(room)


def test_model_blocked_by_tombstone(app_ctx):
    room = uuid4()
    a = record_belief(actor="explicit_human_command", scope="room", kind="fact",
                      text="bad is wrong", confidence=1.0, room_uuid=room, evidence=EV)
    db.reject_memory(a.claim.uuid, {"provenance": "confirmed_by_user",
                                    "source_type": "manual", "excerpt": "no"})
    r = record_belief(actor="model_inferred", scope="room", kind="fact",
                      text="bad is wrong", confidence=0.6, room_uuid=room,
                      evidence={"provenance": "inferred_by_model",
                                "source_type": "chat_message", "source_id": str(uuid4()),
                                "excerpt": "e", "created_by_uuid": str(uuid4())})
    assert r.outcome == "refused_tombstone" and r.claim is None
    t = db.check_tombstone("room", room, None, a.claim.subj_pred_key, a.claim.value_key)
    assert t.hit_count == 1
    _cleanup(room)


def test_human_overrides_same_scope_tombstone(app_ctx):
    room = uuid4()
    a = record_belief(actor="explicit_human_command", scope="room", kind="fact",
                      text="ok is fine", confidence=1.0, room_uuid=room, evidence=EV)
    db.reject_memory(a.claim.uuid, {"provenance": "confirmed_by_user",
                                    "source_type": "manual", "excerpt": "no"})
    r = record_belief(actor="explicit_human_command", scope="room", kind="fact",
                      text="ok is fine", confidence=1.0, room_uuid=room, evidence=EV)
    assert r.outcome == "created" and r.claim.status == "active"
    assert db.check_tombstone("room", room, None, a.claim.subj_pred_key,
                              a.claim.value_key) is None
    _cleanup(room)


def test_room_human_write_creates_scoped_exception_over_global_tombstone(app_ctx):
    room = uuid4()
    g = record_belief(actor="explicit_human_command", scope="global", kind="fact",
                      text="gx is one", confidence=1.0, room_uuid=room, evidence=EV)
    # tombstone at global scope (reject the global claim)
    db.reject_memory(g.claim.uuid, {"provenance": "confirmed_by_user",
                                    "source_type": "manual", "excerpt": "no"})
    r = record_belief(actor="explicit_human_command", scope="room", kind="fact",
                      text="gx is one", confidence=1.0, room_uuid=room, evidence=EV)
    assert r.outcome == "created" and r.claim.scope == "room"
    # global tombstone still there
    assert db.check_tombstone("global", None, None, g.claim.subj_pred_key,
                              g.claim.value_key) is not None
    _cleanup(room)


def test_validate_evidence_requires_chat_message_fields():
    with pytest.raises(ValueError):
        validate_evidence({"provenance": "inferred_by_model",
                           "source_type": "chat_message"})   # missing source_id/excerpt/created_by_uuid


def test_validate_evidence_manual_allows_missing_created_by():
    validate_evidence({"provenance": "confirmed_by_user", "source_type": "manual",
                       "excerpt": "reason text"})   # no raise
