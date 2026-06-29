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


# ---------------------------------------------------------------------------
# P1 dedupe: correct_belief must corroborate an existing claim instead of
# creating a duplicate when new_text matches an already-active claim.
# This repros the bug in the pre-fix correct_belief which always calls
# create_memory_claim directly, bypassing the dedupe check.
# ---------------------------------------------------------------------------

def test_correct_belief_dedupes_against_existing_active_claim(app_ctx):
    """P1 dedupe repro: correct A→Y when B(Y) is already active must leave
    exactly ONE active claim with text Y (B is corroborated, not duplicated)."""
    from db.memory import correct_belief, KEY_VERSION
    room = uuid4()
    marker = f"dedup-{uuid4().hex[:8]}"
    text_x = f"{marker} is red"
    text_y = f"{marker} is blue"
    evidence = {"provenance": "confirmed_by_user", "source_type": "manual",
                "excerpt": "dedupe test"}

    # Create claim A (text X) and claim B (text Y) both active in same scope
    a_result = record_belief(
        actor="explicit_human_command", scope="room", kind="fact",
        text=text_x, confidence=1.0, room_uuid=room, evidence=EV,
    )
    a = a_result.claim

    b_result = record_belief(
        actor="explicit_human_command", scope="room", kind="fact",
        text=text_y, confidence=1.0, room_uuid=room, evidence=EV,
    )
    b = b_result.claim

    a_uuid = a.uuid
    b_uuid = b.uuid
    result_uuid = None
    try:
        # correct A -> Y (same text as B which is already active)
        result_claim = correct_belief(
            a_uuid, text_y,
            actor="explicit_human_command",
            evidence=evidence,
        )
        result_uuid = result_claim.uuid if result_claim is not None else None

        # A must be superseded
        a_reloaded = db.get_memory_claim(a_uuid)
        assert a_reloaded.status == "superseded", \
            f"Expected A to be superseded, got {a_reloaded.status!r}"

        # Exactly ONE active claim with text_y (B must NOT have been duplicated)
        active_y = (
            db.db.session.query(MemoryClaim)
            .filter(MemoryClaim.text == text_y, MemoryClaim.status == "active")
            .all()
        )
        assert len(active_y) == 1, (
            f"Expected exactly 1 active claim with text {text_y!r}, "
            f"found {len(active_y)} — pre-fix code creates a duplicate"
        )

        # The returned claim must be B (corroborated, not a new duplicate)
        assert result_uuid == b_uuid, \
            f"Expected result to be B ({b_uuid}), got {result_uuid}"

    finally:
        uuids = {a_uuid, b_uuid}
        if result_uuid is not None:
            uuids.add(result_uuid)
        _cleanup_correct(list(uuids))


# ---------------------------------------------------------------------------
# P3 global tombstone scoped-exception: correct_belief must mirror
# record_belief's global-tombstone handling: the global tombstone stays intact
# and the new room-scoped claim has evidence annotated with the scoped-exception
# note (just as record_belief does when a human creates a room claim over a
# global tombstone).
# ---------------------------------------------------------------------------

def test_correct_belief_global_tombstone_scoped_exception(app_ctx):
    """P3: correct_belief(old_room_claim, new_text_matching_global_tombstone)
    must: (a) supersede the old claim, (b) still create the room-scoped claim
    (human override), (c) leave the global tombstone intact, (d) annotate
    evidence with the scoped-exception note."""
    from db.memory import correct_belief, belief_keys, KEY_VERSION
    room = uuid4()
    marker = f"gtomb-{uuid4().hex[:8]}"
    text_global = f"{marker} is forbidden"
    text_old_room = f"{marker} is allowed"

    # Build a global tombstone: create+reject a global claim with text_global
    g_result = record_belief(
        actor="explicit_human_command", scope="global", kind="fact",
        text=text_global, confidence=1.0, room_uuid=room, evidence=EV,
    )
    g = g_result.claim
    g_uuid = g.uuid  # capture before session state may change
    db.reject_memory(g_uuid, {"provenance": "confirmed_by_user",
                               "source_type": "manual", "excerpt": "reject global"})

    # Verify tombstone is in place at global scope
    sp, val = belief_keys(None, None, None, text_global)
    global_tomb = db.check_tombstone("global", None, None, sp, val)
    assert global_tomb is not None, "Setup: global tombstone must exist"

    # Create an existing room-scoped active claim (something to correct FROM)
    old_room_result = record_belief(
        actor="explicit_human_command", scope="room", kind="fact",
        text=text_old_room, confidence=1.0, room_uuid=room, evidence=EV,
    )
    old_room = old_room_result.claim
    old_room_uuid = old_room.uuid  # capture before session state may change
    assert old_room.status == "active"

    all_created_uuids = [g_uuid, old_room_uuid]
    try:
        # Now correct old_room -> text_global (which has a global tombstone)
        # A human actor should get a scoped exception (claim created at room scope)
        new_claim = correct_belief(
            old_room_uuid, text_global,
            actor="explicit_human_command",
            evidence={"provenance": "confirmed_by_user", "source_type": "manual",
                      "excerpt": "correction to globally-tombstoned text"},
        )

        new_uuid = new_claim.uuid if new_claim is not None else None
        if new_uuid is not None:
            all_created_uuids.append(new_uuid)

        # (a) old_room must be superseded
        old_reloaded = db.get_memory_claim(old_room_uuid)
        assert old_reloaded.status == "superseded", \
            f"Expected old room claim to be superseded, got {old_reloaded.status!r}"

        # (b) a new room-scoped claim with text_global must exist and be active
        assert new_uuid is not None, "Expected a new claim (human scoped exception)"
        new_reloaded = db.get_memory_claim(new_uuid)
        assert new_reloaded is not None, "New claim must exist in DB"
        assert new_reloaded.status == "active", \
            f"Expected active new claim, got {new_reloaded.status!r}"
        assert new_reloaded.scope == "room" or new_reloaded.scope == old_room_result.claim.scope

        # (c) global tombstone must still exist (not cleared by correct_belief)
        global_tomb_after = db.check_tombstone("global", None, None, sp, val)
        assert global_tomb_after is not None, \
            "Global tombstone must survive a room-scope correction (scoped exception)"

        # (d) evidence on the new claim must contain the scoped-exception annotation
        ev_rows = (
            db.db.session.query(MemoryEvidence)
            .filter_by(memory_uuid=new_uuid)
            .all()
        )
        ev_texts = " ".join((e.excerpt or "") + " " + (e.provenance or "")
                            for e in ev_rows)
        assert "scoped exception" in ev_texts.lower(), (
            f"Expected scoped-exception note in evidence, got: {ev_texts!r}"
        )

    finally:
        _cleanup_correct(all_created_uuids)
        # Also clean up the global tombstone created from g
        db.db.session.query(MemoryRejectedValue).filter_by(
            created_from_uuid=g_uuid
        ).delete()
        db.db.session.commit()
