"""The /memory page's JSON API: list (with derived fields + secret masking),
detail (evidence + lineage), and the provenance-safe lifecycle actions
(activate / reject / reactivate / correct / sensitivity / expiry) with a per-row
stale guard (409). Model-free; conftest pins the DB to rainbox_claude."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

import db
from db import MemoryClaim, MemoryEvidence
from webapp import app


@pytest.fixture
def app_ctx():
    a = db.make_app()
    db.init_db(a)
    ctx = a.app_context()
    ctx.push()
    try:
        yield a
    finally:
        db.db.session.rollback()
        ctx.pop()


@pytest.fixture
def client(app_ctx):
    app.config.update(TESTING=True)
    return app.test_client()


def _claim(text="api claim", status="active", sensitivity="private"):
    return db.create_memory_claim(
        scope="global", kind="fact", text=f"{text} {uuid4().hex[:6]}",
        confidence=1.0, status=status, sensitivity=sensitivity, subject="api-test")


def _cleanup(*uuids):
    for u in uuids:
        db.db.session.query(MemoryEvidence).filter_by(memory_uuid=u).delete()
        db.db.session.query(MemoryClaim).filter_by(uuid=u).delete()
    db.db.session.commit()


def _find(rows, uuid):
    return next((r for r in rows if r["uuid"] == str(uuid)), None)


def test_list_returns_claims_with_derived_fields(client):
    c = _claim(status="active")
    try:
        r = client.get("/memory/api/claims")
        assert r.status_code == 200
        row = _find(r.get_json()["claims"], c.uuid)
        assert row is not None
        assert row["status"] == "active"
        assert "evidence_count" in row and "embedding_state" in row
        assert "updated_at" in row and "stale" in row
    finally:
        _cleanup(c.uuid)


def test_list_masks_secret_text(client):
    c = _claim(text="my password is hunter2", sensitivity="secret")
    try:
        row = _find(client.get("/memory/api/claims").get_json()["claims"], c.uuid)
        assert row["secret"] is True
        assert "hunter2" not in row["text"]
    finally:
        _cleanup(c.uuid)


def test_detail_unmasks_and_includes_evidence(client):
    c = _claim(text="topsecret value", sensitivity="secret")
    db.add_memory_evidence(memory_uuid=c.uuid, provenance="confirmed_by_user",
                           source_type="manual")
    try:
        r = client.get(f"/memory/api/claims/{c.uuid}")
        assert r.status_code == 200
        body = r.get_json()
        assert "topsecret value" in body["text"]          # detail reveals
        assert len(body["evidence"]) >= 1
        assert "embedding_state" in body
    finally:
        _cleanup(c.uuid)


def test_detail_includes_lineage(client):
    old = _claim(text="old", status="active")
    new = db.supersede_memory(
        old.uuid,
        {"scope": "global", "kind": "fact", "text": f"new {uuid4().hex[:6]}",
         "confidence": 1.0, "sensitivity": "private"},
        {"provenance": "confirmed_by_user", "source_type": "manual"})
    try:
        body = client.get(f"/memory/api/claims/{new.uuid}").get_json()
        assert body["supersedes"]["uuid"] == str(old.uuid)
        body_old = client.get(f"/memory/api/claims/{old.uuid}").get_json()
        assert body_old["superseded_by"]["uuid"] == str(new.uuid)
    finally:
        _cleanup(old.uuid, new.uuid)


def _post(client, uuid, action, **body):
    resp = client.post(f"/memory/api/claims/{uuid}/{action}", json=body)
    # The request commits in its own (app-context) session; expire the test
    # session's identity map so re-reads see the committed change rather than
    # the stale cached instance. (Test-only: production runs one session.)
    db.db.session.expire_all()
    return resp


def test_activate_candidate(client):
    c = _claim(status="candidate")
    try:
        r = _post(client, c.uuid, "activate", expected_updated_at=c.updated_at.isoformat())
        assert r.status_code == 200
        assert db.get_memory_claim(c.uuid).status == "active"
    finally:
        _cleanup(c.uuid)


def test_reject_active(client):
    c = _claim(status="active")
    try:
        r = _post(client, c.uuid, "reject", expected_updated_at=c.updated_at.isoformat())
        assert r.status_code == 200
        assert db.get_memory_claim(c.uuid).status == "rejected"
    finally:
        _cleanup(c.uuid)


def test_reactivate_rejected(client):
    c = _claim(status="rejected")
    try:
        r = _post(client, c.uuid, "reactivate", expected_updated_at=c.updated_at.isoformat())
        assert r.status_code == 200
        assert db.get_memory_claim(c.uuid).status == "active"
    finally:
        _cleanup(c.uuid)


def test_correct_supersedes(client):
    c = _claim(text="I like long answers", status="active")
    new_uuid = None
    try:
        r = _post(client, c.uuid, "correct", new_text="I like concise answers",
                  expected_updated_at=c.updated_at.isoformat())
        assert r.status_code == 200
        new_uuid = r.get_json()["new_uuid"]
        assert db.get_memory_claim(c.uuid).status == "superseded"
        new = db.get_memory_claim(new_uuid)
        assert new.status == "active" and new.text == "I like concise answers"
    finally:
        _cleanup(c.uuid, *( [new_uuid] if new_uuid else []))


def test_correct_produces_keyed_claim(client):
    """POST /memory/api/claims/<uuid>/correct via UI must produce a keyed claim
    (non-empty subj_pred_key/value_key/key_version), NOT an unkeyed one (P1b via UI)."""
    from db.memory import KEY_VERSION
    marker = f"ui-correct-{uuid4().hex[:8]}"
    # Create a structured claim via record_belief so it gets proper keys
    result = db.record_belief(
        actor="explicit_human_command", scope="global", kind="fact",
        text=f"{marker} is red", confidence=1.0, sensitivity="private",
        evidence={"provenance": "confirmed_by_user", "source_type": "manual",
                  "excerpt": "setup"},
    )
    c = result.claim
    new_uuid = None
    try:
        r = _post(client, c.uuid, "correct",
                  new_text=f"{marker} is blue",
                  expected_updated_at=c.updated_at.isoformat())
        assert r.status_code == 200
        new_uuid = r.get_json()["new_uuid"]
        db.db.session.expire_all()
        new = db.get_memory_claim(new_uuid)
        assert new is not None
        assert new.status == "active"
        assert new.subj_pred_key, \
            f"subj_pred_key must be non-empty (keyed); got {new.subj_pred_key!r}"
        assert new.value_key == "blue", \
            f"value_key must be 'blue' (derived from new text); got {new.value_key!r}"
        assert new.key_version == KEY_VERSION, \
            f"key_version must be {KEY_VERSION}; got {new.key_version!r}"
    finally:
        uuids = [c.uuid]
        if new_uuid:
            uuids.append(new_uuid)
        # Clean up evidence, claims, tombstones (by created_from_uuid)
        from db.models import MemoryRejectedValue
        db.db.session.query(MemoryEvidence).filter(
            MemoryEvidence.memory_uuid.in_(uuids)
        ).delete(synchronize_session=False)
        db.db.session.query(MemoryRejectedValue).filter(
            MemoryRejectedValue.created_from_uuid.in_(uuids)
        ).delete(synchronize_session=False)
        db.db.session.query(MemoryClaim).filter(
            MemoryClaim.uuid.in_(uuids)
        ).delete(synchronize_session=False)
        db.db.session.commit()


def test_sensitivity_change(client):
    c = _claim(sensitivity="private")
    try:
        r = _post(client, c.uuid, "sensitivity", sensitivity="public",
                  expected_updated_at=c.updated_at.isoformat())
        assert r.status_code == 200
        assert db.get_memory_claim(c.uuid).sensitivity == "public"
    finally:
        _cleanup(c.uuid)


def test_scope_change_room_to_global(client):
    from uuid import uuid4 as _uuid4
    c = db.create_memory_claim(
        scope="room", kind="fact", text=f"api room claim {_uuid4().hex[:6]}",
        confidence=1.0, status="active", sensitivity="private",
        subject="api-test", room_uuid=_uuid4())
    try:
        r = _post(client, c.uuid, "scope", scope="global",
                  expected_updated_at=c.updated_at.isoformat())
        assert r.status_code == 200
        assert db.get_memory_claim(c.uuid).scope == "global"
        # Keyless narrowing is a 400, not a silent no-op.
        got = db.get_memory_claim(c.uuid)
        r2 = _post(client, c.uuid, "scope", scope="project",
                   expected_updated_at=got.updated_at.isoformat())
        assert r2.status_code == 400
    finally:
        _cleanup(c.uuid)


def test_expiry_set_and_clear(client):
    c = _claim(status="active")
    when = (datetime.now(UTC) + timedelta(days=3)).isoformat()
    try:
        r = _post(client, c.uuid, "expiry", expires_at=when,
                  expected_updated_at=c.updated_at.isoformat())
        assert r.status_code == 200
        got = db.get_memory_claim(c.uuid)
        assert got.expires_at is not None
        r2 = _post(client, c.uuid, "expiry", expires_at=None,
                   expected_updated_at=got.updated_at.isoformat())
        assert r2.status_code == 200
        assert db.get_memory_claim(c.uuid).expires_at is None
    finally:
        _cleanup(c.uuid)


def test_stale_guard_returns_409(client):
    c = _claim(status="candidate")
    try:
        stale = (c.updated_at - timedelta(days=1)).isoformat()
        r = _post(client, c.uuid, "activate", expected_updated_at=stale)
        assert r.status_code == 409
        assert db.get_memory_claim(c.uuid).status == "candidate"  # untouched
    finally:
        _cleanup(c.uuid)


def test_bad_sensitivity_returns_400(client):
    c = _claim()
    try:
        r = _post(client, c.uuid, "sensitivity", sensitivity="nope",
                  expected_updated_at=c.updated_at.isoformat())
        assert r.status_code == 400
    finally:
        _cleanup(c.uuid)


def test_missing_claim_returns_404(client):
    r = client.get(f"/memory/api/claims/{uuid4()}")
    assert r.status_code == 404


# --- resolve conflict endpoint ------------------------------------------------

_MEV_USER = "00000000-0000-0000-0000-000000000001"


def _cleanup_room(room_uuid):
    """Delete all memory rows seeded for a test room (claims, evidence, tombstones)."""
    from db import MemoryClaim, MemoryEvidence
    from db.models import MemoryRejectedValue
    db.db.session.query(MemoryEvidence).filter(MemoryEvidence.memory_uuid.in_(
        db.db.session.query(MemoryClaim.uuid).filter_by(room_uuid=room_uuid)
    )).delete(synchronize_session=False)
    db.db.session.query(MemoryClaim).filter_by(room_uuid=room_uuid).delete()
    db.db.session.query(MemoryRejectedValue).filter_by(room_uuid=room_uuid).delete()
    db.db.session.commit()


def _seed_conflict(room):
    """Seed an active claim + a model conflict candidate in `room`. Returns the candidate claim."""
    db.record_belief(
        actor="explicit_human_command", scope="room", kind="preference",
        text="gus prefers tea", confidence=1.0, room_uuid=room,
        subject="gus", predicate="prefers", object="tea",
        evidence={"provenance": "confirmed_by_user", "source_type": "manual", "excerpt": "x"})
    result = db.record_belief(
        actor="model_inferred", scope="room", kind="preference",
        text="gus prefers coffee", confidence=0.6, room_uuid=room,
        subject="gus", predicate="prefers", object="coffee",
        evidence={"provenance": "inferred_by_model", "source_type": "chat_message",
                  "source_id": "m", "excerpt": "e", "created_by_uuid": _MEV_USER})
    assert result.outcome == "conflict_candidate"
    return result.claim


def test_resolve_conflict_not_conflict(client):
    """POST /api/memory/<cand>/resolve with resolution=not_conflict activates the candidate."""
    room = uuid4()
    cand = _seed_conflict(room)
    try:
        resp = client.post(f"/api/memory/{cand.uuid}/resolve",
                           json={"resolution": "not_conflict"})
        db.db.session.expire_all()
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert body["status"] == "active"
        assert db.get_memory_claim(cand.uuid).status == "active"
    finally:
        _cleanup_room(room)


def test_resolve_conflict_reject(client):
    """POST /api/memory/<cand>/resolve with resolution=reject rejects the candidate and creates a tombstone."""
    from db.models import MemoryRejectedValue
    room = uuid4()
    cand = _seed_conflict(room)
    try:
        resp = client.post(f"/api/memory/{cand.uuid}/resolve",
                           json={"resolution": "reject"})
        db.db.session.expire_all()
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert body["status"] == "rejected"
        assert db.get_memory_claim(cand.uuid).status == "rejected"
        # a tombstone must exist for the rejected value
        assert db.check_tombstone("room", room, None,
                                  cand.subj_pred_key, cand.value_key) is not None
    finally:
        _cleanup_room(room)


def test_resolve_conflict_invalid_resolution_returns_400(client):
    """POST /api/memory/<cand>/resolve with an unknown resolution returns 400."""
    room = uuid4()
    cand = _seed_conflict(room)
    try:
        resp = client.post(f"/api/memory/{cand.uuid}/resolve",
                           json={"resolution": "totally_invalid"})
        assert resp.status_code == 400
        assert "error" in resp.get_json()
    finally:
        _cleanup_room(room)


def test_list_includes_conflicts_with_uuid(client):
    """GET /memory/api/claims returns conflicts_with_uuid on each claim row."""
    room = uuid4()
    cand = _seed_conflict(room)
    try:
        r = client.get("/memory/api/claims")
        assert r.status_code == 200
        row = _find(r.get_json()["claims"], cand.uuid)
        assert row is not None
        assert "conflicts_with_uuid" in row
        assert row["conflicts_with_uuid"] == str(cand.conflicts_with_uuid)
    finally:
        _cleanup_room(room)


def test_list_includes_tombstone_hits_summary(client):
    """GET /memory/api/claims returns a top-level tombstone_hits list."""
    r = client.get("/memory/api/claims")
    assert r.status_code == 200
    body = r.get_json()
    assert "tombstone_hits" in body


def test_detail_includes_conflicts_with_uuid(client):
    """GET /memory/api/claims/<uuid> returns conflicts_with_uuid on a conflict candidate."""
    room = uuid4()
    cand = _seed_conflict(room)
    try:
        r = client.get(f"/memory/api/claims/{cand.uuid}")
        assert r.status_code == 200
        body = r.get_json()
        assert "conflicts_with_uuid" in body
        assert body["conflicts_with_uuid"] == str(cand.conflicts_with_uuid)
    finally:
        _cleanup_room(room)


# ---------------------------------------------------------------------------
# P3: tombstone-hit rows must include subj_pred_key; claim detail must too
# ---------------------------------------------------------------------------

def test_tombstone_hit_row_includes_subj_pred_key(client):
    """GET /memory/api/claims returns tombstone_hits whose rows include subj_pred_key
    so the frontend can filter suppressions to the specific (subject, predicate) pair."""
    from db.models import MemoryRejectedValue
    room = uuid4()
    # Create a claim to tombstone and bump hit_count > 0 so it appears in the list.
    c = db.create_memory_claim(
        scope="room", kind="fact", text="bob is tall",
        confidence=1.0, status="active", sensitivity="private",
        room_uuid=room, subject="bob", predicate="is", object="tall",
    )
    # Write a tombstone with a non-zero hit_count so list_tombstones_with_hits returns it.
    from db.memory import write_tombstone
    tomb = write_tombstone(c, reason="superseded", commit=True)
    # Bump hit_count to 1 so it shows in the list.
    from db.memory import record_tombstone_hit
    record_tombstone_hit(tomb, commit=True)
    db.db.session.expire_all()

    try:
        r = client.get("/memory/api/claims")
        assert r.status_code == 200
        hits = r.get_json().get("tombstone_hits", [])
        # Find our tombstone row by room_uuid or claim_text
        our_hit = next(
            (t for t in hits if t.get("claim_text") == "bob is tall"), None
        )
        assert our_hit is not None, \
            f"tombstone for 'bob is tall' not found in tombstone_hits; got: {hits}"
        assert "subj_pred_key" in our_hit, \
            f"subj_pred_key missing from tombstone_hit_row; row keys: {list(our_hit.keys())}"
    finally:
        db.db.session.query(MemoryEvidence).filter_by(memory_uuid=c.uuid).delete()
        db.db.session.query(MemoryClaim).filter_by(uuid=c.uuid).delete()
        db.db.session.query(MemoryRejectedValue).filter_by(room_uuid=room).delete()
        db.db.session.commit()


def test_claim_detail_includes_subj_pred_key(client):
    """GET /memory/api/claims/<uuid> returns subj_pred_key on the claim detail
    so the frontend tombstoneHitsHtml can match suppression rows to this specific claim."""
    room = uuid4()
    # Use record_belief so subj_pred_key is automatically computed and stored.
    result = db.record_belief(
        actor="explicit_human_command", scope="room", kind="fact",
        text="carol is short", confidence=1.0, sensitivity="private",
        room_uuid=room, subject="carol", predicate="is", object="short",
        evidence={"provenance": "confirmed_by_user", "source_type": "manual",
                  "excerpt": "test"},
    )
    c = result.claim
    assert c is not None and c.subj_pred_key, \
        f"Setup failed: claim has no subj_pred_key (outcome={result.outcome!r})"
    try:
        r = client.get(f"/memory/api/claims/{c.uuid}")
        assert r.status_code == 200
        body = r.get_json()
        assert "subj_pred_key" in body, \
            f"subj_pred_key missing from claim detail; keys: {list(body.keys())}"
        # The key must be non-empty for a structured (subject+predicate) claim.
        assert body["subj_pred_key"], \
            f"subj_pred_key is empty for structured claim; got {body['subj_pred_key']!r}"
    finally:
        db.db.session.query(MemoryEvidence).filter_by(memory_uuid=c.uuid).delete()
        db.db.session.query(MemoryClaim).filter_by(uuid=c.uuid).delete()
        from db.models import MemoryRejectedValue
        db.db.session.query(MemoryRejectedValue).filter_by(room_uuid=room).delete()
        db.db.session.commit()
