# db/test_record_belief_conflict.py
import pytest
from uuid import uuid4
import db
from db.memory import record_belief
from db import MemoryClaim, MemoryEvidence
from db.models import MemoryRejectedValue

EV = {"provenance": "confirmed_by_user", "source_type": "manual", "excerpt": "x"}
_MEV_USER = "00000000-0000-0000-0000-000000000001"
MEV = {"provenance": "inferred_by_model", "source_type": "chat_message",
       "source_id": "00000000-0000-0000-0000-000000000002", "excerpt": "e",
       "created_by_uuid": _MEV_USER}


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


def test_human_same_scope_conflict_supersedes(app_ctx):
    room = uuid4()
    a = record_belief(actor="explicit_human_command", scope="room", kind="preference",
                      text="alice prefers tea", confidence=1.0, room_uuid=room,
                      subject="alice", predicate="prefers", object="tea", evidence=EV)
    b = record_belief(actor="explicit_human_command", scope="room", kind="preference",
                      text="alice prefers coffee", confidence=1.0, room_uuid=room,
                      subject="alice", predicate="prefers", object="coffee", evidence=EV)
    assert b.outcome == "superseded"
    assert db.get_memory_claim(a.claim.uuid).status == "superseded"
    # rival's old value is now tombstoned
    assert db.check_tombstone("room", room, None, a.claim.subj_pred_key,
                              a.claim.value_key) is not None
    _cleanup(room)


def test_model_conflict_makes_candidate(app_ctx):
    room = uuid4()
    a = record_belief(actor="explicit_human_command", scope="room", kind="preference",
                      text="bob prefers tea", confidence=1.0, room_uuid=room,
                      subject="bob", predicate="prefers", object="tea", evidence=EV)
    b = record_belief(actor="model_inferred", scope="room", kind="preference",
                      text="bob prefers coffee", confidence=0.6, room_uuid=room,
                      subject="bob", predicate="prefers", object="coffee", evidence=MEV)
    assert b.outcome == "conflict_candidate"
    assert b.claim.status == "candidate"
    assert b.conflicts_with_uuid == a.claim.uuid
    assert db.get_memory_claim(a.claim.uuid).status == "active"   # rival stays
    _cleanup(room)


def test_room_human_vs_broader_global_rival_is_candidate(app_ctx):
    room = uuid4()
    g = record_belief(actor="explicit_human_command", scope="global", kind="preference",
                      text="carol prefers tea", confidence=1.0, room_uuid=room,
                      subject="carol", predicate="prefers", object="tea", evidence=EV)
    r = record_belief(actor="explicit_human_command", scope="room", kind="preference",
                      text="carol prefers coffee", confidence=1.0, room_uuid=room,
                      subject="carol", predicate="prefers", object="coffee", evidence=EV)
    assert r.outcome == "conflict_candidate"   # don't silently overturn a global belief
    assert db.get_memory_claim(g.claim.uuid).status == "active"
    _cleanup(room)


def test_reject_memory_tombstone_param_false_skips_tombstone(app_ctx):
    """reject_memory(..., tombstone=False) must NOT leave a tombstone row."""
    room = uuid4()
    claim = db.create_memory_claim(
        scope="room", kind="preference", text="dave prefers cola",
        confidence=1.0, status="active", room_uuid=room,
        subject="dave", predicate="prefers", object="cola",
        subj_pred_key="dave\x1fprefers", value_key="cola", key_version=1)
    db.reject_memory(claim.uuid, {"provenance": "confirmed_by_user",
                                  "source_type": "manual"}, tombstone=False)
    assert db.get_memory_claim(claim.uuid).status == "rejected"
    tomb = db.check_tombstone("room", room, None, "dave\x1fprefers", "cola")
    assert tomb is None, "tombstone=False must not create a tombstone"
    _cleanup(room)


def test_reject_memory_default_creates_tombstone(app_ctx):
    """reject_memory(...) (default tombstone=True) MUST leave a tombstone row."""
    room = uuid4()
    claim = db.create_memory_claim(
        scope="room", kind="preference", text="eve prefers juice",
        confidence=1.0, status="active", room_uuid=room,
        subject="eve", predicate="prefers", object="juice",
        subj_pred_key="eve\x1fprefers", value_key="juice", key_version=1)
    db.reject_memory(claim.uuid, {"provenance": "confirmed_by_user",
                                  "source_type": "manual"})
    assert db.get_memory_claim(claim.uuid).status == "rejected"
    tomb = db.check_tombstone("room", room, None, "eve\x1fprefers", "juice")
    assert tomb is not None, "default reject_memory must create a tombstone"
    _cleanup(room)


def test_supersede_memory_writes_tombstone_for_old_value(app_ctx):
    """supersede_memory must write a tombstone for the OLD/superseded value."""
    from db.memory import supersede_memory
    room = uuid4()
    old = db.create_memory_claim(
        scope="room", kind="preference", text="frank prefers water",
        confidence=1.0, status="active", room_uuid=room,
        subject="frank", predicate="prefers", object="water",
        subj_pred_key="frank\x1fprefers", value_key="water", key_version=1)
    new_args = dict(scope="room", kind="preference", text="frank prefers soda",
                    confidence=1.0, status="active", sensitivity="private", room_uuid=room,
                    subject="frank", predicate="prefers", object="soda",
                    subj_pred_key="frank\x1fprefers", value_key="soda", key_version=1)
    supersede_memory(old.uuid, new_args,
                     {"provenance": "confirmed_by_user", "source_type": "manual",
                      "excerpt": "changed"})
    assert db.get_memory_claim(old.uuid).status == "superseded"
    # tombstone for old value must exist
    tomb = db.check_tombstone("room", room, None, "frank\x1fprefers", "water")
    assert tomb is not None, "supersede_memory must tombstone the old value"
    _cleanup(room)
