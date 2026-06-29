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


# ---------------------------------------------------------------------------
# correct_belief: P1b — keys must be derived from new_text, NOT copied from old
# ---------------------------------------------------------------------------

def _cleanup_correct(uuids):
    """Delete all claims+evidence by uuid, then tombstones by created_from_uuid."""
    db.db.session.query(MemoryEvidence).filter(
        MemoryEvidence.memory_uuid.in_(uuids)
    ).delete(synchronize_session=False)
    db.db.session.query(MemoryClaim).filter(
        MemoryClaim.uuid.in_(uuids)
    ).delete(synchronize_session=False)
    db.db.session.query(MemoryRejectedValue).filter(
        MemoryRejectedValue.created_from_uuid.in_(uuids)
    ).delete(synchronize_session=False)
    db.db.session.commit()


def test_correct_belief_keys_derived_from_new_text(app_ctx):
    """P1b: correct_belief must derive value_key/subj_pred_key from new_text,
    not copy them from the old claim. Old='probe-X is red', new='probe-X is blue'
    => new claim's value_key must end with 'blue', NOT 'red'."""
    from db.memory import correct_belief, KEY_VERSION
    marker = f"probe-{uuid4().hex[:8]}"
    text_old = f"{marker} is red"
    text_new = f"{marker} is blue"
    evidence = {"provenance": "confirmed_by_user", "source_type": "manual",
                "excerpt": "test correction"}

    old = db.create_memory_claim(
        scope="global", kind="fact", text=text_old,
        confidence=1.0, status="active", sensitivity="private",
        subj_pred_key=marker + "\x1fis", value_key="red", key_version=KEY_VERSION,
    )
    try:
        new = correct_belief(
            old.uuid, text_new,
            actor="explicit_human_command",
            evidence=evidence,
        )
        db.db.session.expire_all()

        # New claim must be active with correct lineage
        assert new.status == "active", f"Expected active, got {new.status!r}"
        assert new.supersedes_uuid == old.uuid, "new.supersedes_uuid must point to old"

        # Old must be superseded
        old_reloaded = db.get_memory_claim(old.uuid)
        assert old_reloaded.status == "superseded", \
            f"Expected old to be superseded, got {old_reloaded.status!r}"

        # Keys must be derived from new_text ('blue'), not copied from old ('red')
        assert new.value_key == "blue", \
            f"value_key must be 'blue' (from new_text), got {new.value_key!r}"
        assert new.subj_pred_key and "\x1fis" in new.subj_pred_key, \
            f"subj_pred_key must contain 'is', got {new.subj_pred_key!r}"
        assert new.key_version == KEY_VERSION, \
            f"key_version must be {KEY_VERSION}, got {new.key_version!r}"

        # Structured columns must match new_text
        assert new.object == "blue", \
            f"object must be 'blue' (from new_text), got {new.object!r}"

    finally:
        uuids = [old.uuid]
        new_uuid = getattr(new, "uuid", None) if "new" in dir() else None
        if new_uuid:
            uuids.append(new_uuid)
        _cleanup_correct(uuids)


def test_correct_belief_atomicity_one_active_claim(app_ctx):
    """correct_belief must leave exactly ONE active claim with new_text and the old
    superseded — no duplicate active claims."""
    from db.memory import correct_belief, KEY_VERSION
    marker = f"atomic-{uuid4().hex[:8]}"
    text_old = f"{marker} prefers cats"
    text_new = f"{marker} prefers dogs"
    evidence = {"provenance": "confirmed_by_user", "source_type": "manual",
                "excerpt": "atomicity test"}

    old = db.create_memory_claim(
        scope="global", kind="fact", text=text_old,
        confidence=1.0, status="active", sensitivity="private",
        subj_pred_key=marker + "\x1fprefers", value_key="cats", key_version=KEY_VERSION,
    )
    try:
        new = correct_belief(
            old.uuid, text_new,
            actor="explicit_human_command",
            evidence=evidence,
        )
        db.db.session.expire_all()

        # Exactly one active claim with new_text
        active_new = (
            db.db.session.query(MemoryClaim)
            .filter(MemoryClaim.text == text_new, MemoryClaim.status == "active")
            .all()
        )
        assert len(active_new) == 1, \
            f"Expected exactly 1 active claim with new text, found {len(active_new)}"

        # Old must be superseded (not active, not deleted)
        old_reloaded = db.get_memory_claim(old.uuid)
        assert old_reloaded is not None
        assert old_reloaded.status == "superseded"

    finally:
        uuids = [old.uuid]
        new_uuid = getattr(new, "uuid", None) if "new" in dir() else None
        if new_uuid:
            uuids.append(new_uuid)
        _cleanup_correct(uuids)


def test_correct_belief_bad_actor_raises(app_ctx):
    """correct_belief with a non-override actor raises ValueError."""
    from db.memory import correct_belief, KEY_VERSION
    marker = f"badactor-{uuid4().hex[:8]}"
    old = db.create_memory_claim(
        scope="global", kind="fact", text=f"{marker} is red",
        confidence=1.0, status="active", sensitivity="private",
    )
    try:
        with pytest.raises(ValueError, match="override"):
            correct_belief(
                old.uuid, f"{marker} is blue",
                actor="model_inferred",
                evidence={"provenance": "confirmed_by_user", "source_type": "manual",
                          "excerpt": "test"},
            )
    finally:
        db.db.session.query(MemoryEvidence).filter_by(memory_uuid=old.uuid).delete()
        db.db.session.query(MemoryClaim).filter_by(uuid=old.uuid).delete()
        db.db.session.commit()


def test_correct_belief_stale_guard_raises(app_ctx):
    """correct_belief raises StaleWriteError when expected_updated_at mismatches."""
    from datetime import timedelta
    from db.memory import correct_belief, KEY_VERSION
    from db import StaleWriteError
    marker = f"stale-{uuid4().hex[:8]}"
    old = db.create_memory_claim(
        scope="global", kind="fact", text=f"{marker} is red",
        confidence=1.0, status="active", sensitivity="private",
    )
    stale_time = old.updated_at - timedelta(days=1)
    try:
        with pytest.raises(StaleWriteError):
            correct_belief(
                old.uuid, f"{marker} is blue",
                actor="explicit_human_command",
                evidence={"provenance": "confirmed_by_user", "source_type": "manual",
                          "excerpt": "test"},
                expected_updated_at=stale_time,
            )
    finally:
        db.db.session.query(MemoryEvidence).filter_by(memory_uuid=old.uuid).delete()
        db.db.session.query(MemoryClaim).filter_by(uuid=old.uuid).delete()
        db.db.session.commit()
