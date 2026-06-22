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


def test_sensitivity_change(client):
    c = _claim(sensitivity="private")
    try:
        r = _post(client, c.uuid, "sensitivity", sensitivity="public",
                  expected_updated_at=c.updated_at.isoformat())
        assert r.status_code == 200
        assert db.get_memory_claim(c.uuid).sensitivity == "public"
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
