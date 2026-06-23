"""DB helpers backing the /memory review UI: sensitivity/expiry edits, reactivate
(rejected/expired -> active), a detail assembler (claim + evidence + supersession
lineage), the stale-row guard, and the `claim_stale` predicate. Model-free."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

import db
from db import MemoryClaim, MemoryEvidence
from db.memory import (
    StaleWriteError,
    claim_stale,
    find_equivalent_claim,
    memory_claim_detail,
    normalize_claim_text,
    reactivate_memory_claim,
    set_memory_expiry,
    set_memory_sensitivity,
)


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


def _claim(text="ui helper claim", status="active", sensitivity="private",
           expires_at=None):
    return db.create_memory_claim(
        scope="global", kind="fact", text=f"{text} {uuid4().hex[:6]}",
        confidence=1.0, status=status, sensitivity=sensitivity,
        subject="ui-test", expires_at=expires_at)


def _cleanup(*uuids):
    for u in uuids:
        db.db.session.query(MemoryEvidence).filter_by(memory_uuid=u).delete()
        db.db.session.query(MemoryClaim).filter_by(uuid=u).delete()
    db.db.session.commit()


def test_set_sensitivity_updates_field_and_timestamp(app_ctx):
    c = _claim(sensitivity="private")
    before = c.updated_at
    try:
        set_memory_sensitivity(c.uuid, "secret", expected_updated_at=before)
        got = db.get_memory_claim(c.uuid)
        assert got.sensitivity == "secret"
        assert got.updated_at >= before
    finally:
        _cleanup(c.uuid)


def test_set_sensitivity_rejects_bad_value(app_ctx):
    c = _claim()
    try:
        with pytest.raises(ValueError):
            set_memory_sensitivity(c.uuid, "nonsense", expected_updated_at=c.updated_at)
        assert db.get_memory_claim(c.uuid).sensitivity == "private"  # unchanged
    finally:
        _cleanup(c.uuid)


def test_set_sensitivity_stale_guard(app_ctx):
    c = _claim()
    stale = c.updated_at - timedelta(days=1)
    try:
        with pytest.raises(StaleWriteError):
            set_memory_sensitivity(c.uuid, "public", expected_updated_at=stale)
        assert db.get_memory_claim(c.uuid).sensitivity == "private"  # untouched
    finally:
        _cleanup(c.uuid)


def test_set_expiry_sets_and_clears(app_ctx):
    c = _claim()
    when = datetime.now(UTC) + timedelta(days=2)
    try:
        set_memory_expiry(c.uuid, when, expected_updated_at=c.updated_at)
        got = db.get_memory_claim(c.uuid)
        assert got.expires_at is not None
        set_memory_expiry(c.uuid, None, expected_updated_at=got.updated_at)
        assert db.get_memory_claim(c.uuid).expires_at is None
    finally:
        _cleanup(c.uuid)


def test_reactivate_from_rejected(app_ctx):
    c = _claim(status="rejected")
    try:
        reactivate_memory_claim(c.uuid, confirmed_by_uuid=None)
        assert db.get_memory_claim(c.uuid).status == "active"
        # a confirmation evidence row was recorded
        evs = db.db.session.query(MemoryEvidence).filter_by(memory_uuid=c.uuid).all()
        assert any(e.provenance == "confirmed_by_user" for e in evs)
    finally:
        _cleanup(c.uuid)


def test_reactivate_refuses_non_rejected(app_ctx):
    c = _claim(status="active")
    try:
        with pytest.raises(ValueError):
            reactivate_memory_claim(c.uuid, confirmed_by_uuid=None)
    finally:
        _cleanup(c.uuid)


def test_reactivate_stale_guard(app_ctx):
    c = _claim(status="rejected")
    stale = c.updated_at - timedelta(days=1)
    try:
        with pytest.raises(StaleWriteError):
            reactivate_memory_claim(c.uuid, confirmed_by_uuid=None, expected_updated_at=stale)
        assert db.get_memory_claim(c.uuid).status == "rejected"  # untouched
    finally:
        _cleanup(c.uuid)


def test_memory_claim_detail_includes_evidence_and_lineage(app_ctx):
    old = _claim(text="old belief", status="active")
    db.add_memory_evidence(memory_uuid=old.uuid, provenance="confirmed_by_user",
                           source_type="manual")
    new = db.supersede_memory(
        old.uuid,
        {"scope": "global", "kind": "fact", "text": f"new belief {uuid4().hex[:6]}",
         "confidence": 1.0, "sensitivity": "private"},
        {"provenance": "confirmed_by_user", "source_type": "manual"},
    )
    try:
        d_new = memory_claim_detail(new.uuid)
        assert d_new["claim"].uuid == new.uuid
        assert d_new["supersedes"] is not None and d_new["supersedes"].uuid == old.uuid
        assert d_new["superseded_by"] is None
        assert len(d_new["evidence"]) >= 1

        d_old = memory_claim_detail(old.uuid)
        assert d_old["claim"].status == "superseded"
        assert d_old["superseded_by"] is not None and d_old["superseded_by"].uuid == new.uuid
        assert len(d_old["evidence"]) >= 1
    finally:
        _cleanup(old.uuid, new.uuid)


def test_find_equivalent_claim_matches_normalized_text(app_ctx):
    room = uuid4()
    c = db.create_memory_claim(
        scope="room", kind="fact", text="Simon has a Triangle Draw mug",
        confidence=1.0, status="active", sensitivity="private", room_uuid=room)
    try:
        # whitespace + case differences still match
        hit = find_equivalent_claim("  simon   has a triangle draw MUG ",
                                    scope="room", room_uuid=room)
        assert hit is not None and hit.uuid == c.uuid
        # different text does not match
        assert find_equivalent_claim("Simon has a cat", scope="room", room_uuid=room) is None
        # different room does not match
        assert find_equivalent_claim("Simon has a Triangle Draw mug",
                                     scope="room", room_uuid=uuid4()) is None
    finally:
        _cleanup(c.uuid)


def test_find_equivalent_claim_ignores_rejected(app_ctx):
    """A previously forgotten (rejected) claim is not an equivalent — so
    re-remembering it creates a fresh claim rather than being blocked."""
    room = uuid4()
    c = db.create_memory_claim(
        scope="room", kind="fact", text="forgotten fact", confidence=1.0,
        status="rejected", sensitivity="private", room_uuid=room)
    try:
        assert find_equivalent_claim("forgotten fact", scope="room", room_uuid=room) is None
    finally:
        _cleanup(c.uuid)


def test_normalize_claim_text(app_ctx):
    assert normalize_claim_text("  A  B\tC ") == "a b c"
    assert normalize_claim_text("") == ""


def test_claim_stale_predicate(app_ctx):
    past = _claim(status="active", expires_at=datetime.now(UTC) - timedelta(hours=1))
    future = _claim(status="active", expires_at=datetime.now(UTC) + timedelta(hours=1))
    none = _claim(status="active")
    rejected_past = _claim(status="rejected",
                           expires_at=datetime.now(UTC) - timedelta(hours=1))
    try:
        assert claim_stale(db.get_memory_claim(past.uuid)) is True
        assert claim_stale(db.get_memory_claim(future.uuid)) is False
        assert claim_stale(db.get_memory_claim(none.uuid)) is False
        # stale only applies to active claims
        assert claim_stale(db.get_memory_claim(rejected_past.uuid)) is False
    finally:
        _cleanup(past.uuid, future.uuid, none.uuid, rejected_past.uuid)
