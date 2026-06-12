"""Tests for the memory_claim / memory_evidence tables in db.py.

Uses the live local Postgres database (db.psycopg_dsn()). Every test
deletes the rows it inserted so artifacts don't accumulate.
"""

from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

import db
from db import MemoryClaim, MemoryEvidence


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


@pytest.fixture
def fresh_uuid():
    """Per-test marker UUID so a teardown can target only this test's rows
    via supersedes_uuid / room_uuid (rather than the global memory_claim
    table). Tests that need to seed multiple claims can use this as a
    tagging UUID in room_uuid."""
    return uuid4()


def _cleanup_claims_by_room(room_uuid: UUID) -> None:
    db.db.session.query(MemoryClaim).filter(
        MemoryClaim.room_uuid == room_uuid
    ).delete()
    db.db.session.commit()


def test_create_memory_claim_persists_all_required_fields(app_ctx, fresh_uuid):
    room_uuid = fresh_uuid
    try:
        claim = db.create_memory_claim(
            scope="room",
            kind="fact",
            text="the sky is blue",
            confidence=0.85,
            status="candidate",
            sensitivity="public",
            room_uuid=room_uuid,
            subject="sky",
            predicate="color_is",
            object="blue",
        )
        # Re-fetch fresh from the DB to confirm the row was persisted, not
        # just held in the session.
        db.db.session.expire_all()
        reloaded = db.get_memory_claim(claim.uuid)
        assert reloaded is not None
        assert reloaded.scope == "room"
        assert reloaded.kind == "fact"
        assert reloaded.text == "the sky is blue"
        assert abs(reloaded.confidence - 0.85) < 1e-9
        assert reloaded.status == "candidate"
        assert reloaded.sensitivity == "public"
        assert reloaded.room_uuid == room_uuid
        assert reloaded.subject == "sky"
        assert reloaded.predicate == "color_is"
        assert reloaded.object == "blue"
        assert reloaded.created_at is not None
        assert reloaded.updated_at is not None
    finally:
        _cleanup_claims_by_room(room_uuid)


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("scope", "nonsense"),
        ("kind", "nonsense"),
        ("status", "nonsense"),
        ("sensitivity", "nonsense"),
    ],
)
def test_invalid_enum_values_rejected_by_db(app_ctx, fresh_uuid, field, bad_value):
    room_uuid = fresh_uuid
    args = dict(
        scope="room", kind="fact", text="x", confidence=0.5,
        status="candidate", sensitivity="public", room_uuid=room_uuid,
    )
    args[field] = bad_value
    try:
        with pytest.raises(sa.exc.IntegrityError):
            db.create_memory_claim(**args)
    finally:
        # IntegrityError leaves the session in a failed-transaction state;
        # roll back before cleanup runs.
        db.db.session.rollback()
        _cleanup_claims_by_room(room_uuid)


def test_invalid_confidence_rejected_by_db(app_ctx, fresh_uuid):
    room_uuid = fresh_uuid
    try:
        with pytest.raises(sa.exc.IntegrityError):
            db.create_memory_claim(
                scope="room", kind="fact", text="x", confidence=1.5,
                status="candidate", sensitivity="public", room_uuid=room_uuid,
            )
    finally:
        db.db.session.rollback()
        _cleanup_claims_by_room(room_uuid)


def _cleanup_claims_and_evidence(room_uuid: UUID) -> None:
    """Delete claims for this test's room_uuid; their evidence cascades."""
    db.db.session.query(MemoryClaim).filter(
        MemoryClaim.room_uuid == room_uuid
    ).delete()
    db.db.session.commit()


def test_add_memory_evidence_persists_provenance_and_source(app_ctx, fresh_uuid):
    room_uuid = fresh_uuid
    try:
        claim = db.create_memory_claim(
            scope="room", kind="fact", text="x", confidence=0.6,
            status="candidate", sensitivity="public", room_uuid=room_uuid,
        )
        ev = db.add_memory_evidence(
            memory_uuid=claim.uuid,
            provenance="inferred_by_model",
            source_type="chat_message",
            source_id="abc-123",
            excerpt="the user said x",
        )
        db.db.session.expire_all()
        reloaded = db.db.session.query(MemoryEvidence).filter_by(uuid=ev.uuid).first()
        assert reloaded is not None
        assert reloaded.memory_uuid == claim.uuid
        assert reloaded.provenance == "inferred_by_model"
        assert reloaded.source_type == "chat_message"
        assert reloaded.source_id == "abc-123"
        assert reloaded.excerpt == "the user said x"
        assert reloaded.created_at is not None
    finally:
        _cleanup_claims_and_evidence(room_uuid)


def test_claim_can_have_multiple_evidence_with_different_provenance(app_ctx, fresh_uuid):
    room_uuid = fresh_uuid
    try:
        claim = db.create_memory_claim(
            scope="room", kind="fact", text="x", confidence=0.6,
            status="candidate", sensitivity="public", room_uuid=room_uuid,
        )
        db.add_memory_evidence(
            memory_uuid=claim.uuid, provenance="inferred_by_model",
            source_type="chat_message", source_id="m1",
        )
        db.add_memory_evidence(
            memory_uuid=claim.uuid, provenance="confirmed_by_user",
            source_type="manual", source_id=None,
        )
        rows = (
            db.db.session.query(MemoryEvidence)
            .filter_by(memory_uuid=claim.uuid)
            .order_by(MemoryEvidence.id.asc())
            .all()
        )
        assert [r.provenance for r in rows] == [
            "inferred_by_model", "confirmed_by_user",
        ]
    finally:
        _cleanup_claims_and_evidence(room_uuid)


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("provenance", "nonsense"),
        ("source_type", "nonsense"),
    ],
)
def test_invalid_evidence_enum_values_rejected(app_ctx, fresh_uuid, field, bad_value):
    room_uuid = fresh_uuid
    try:
        claim = db.create_memory_claim(
            scope="room", kind="fact", text="x", confidence=0.5,
            status="candidate", sensitivity="public", room_uuid=room_uuid,
        )
        args = dict(
            memory_uuid=claim.uuid,
            provenance="inferred_by_model",
            source_type="manual",
        )
        args[field] = bad_value
        with pytest.raises(sa.exc.IntegrityError):
            db.add_memory_evidence(**args)
    finally:
        db.db.session.rollback()
        _cleanup_claims_and_evidence(room_uuid)


def test_deleting_claim_cascades_to_evidence(app_ctx, fresh_uuid):
    room_uuid = fresh_uuid
    try:
        claim = db.create_memory_claim(
            scope="room", kind="fact", text="x", confidence=0.5,
            status="candidate", sensitivity="public", room_uuid=room_uuid,
        )
        db.add_memory_evidence(
            memory_uuid=claim.uuid, provenance="inferred_by_model",
            source_type="chat_message", source_id="m1",
        )
        assert db.db.session.query(MemoryEvidence).filter_by(
            memory_uuid=claim.uuid
        ).count() == 1

        # Cascade: deleting the claim removes its evidence.
        db.db.session.query(MemoryClaim).filter_by(uuid=claim.uuid).delete()
        db.db.session.commit()

        assert db.db.session.query(MemoryEvidence).filter_by(
            memory_uuid=claim.uuid
        ).count() == 0
    finally:
        _cleanup_claims_and_evidence(room_uuid)


def test_list_memory_claims_filters_by_scope_status_and_room(app_ctx, fresh_uuid):
    room_uuid = fresh_uuid
    try:
        a = db.create_memory_claim(
            scope="room", kind="fact", text="a", confidence=0.5,
            status="candidate", sensitivity="public", room_uuid=room_uuid,
        )
        b = db.create_memory_claim(
            scope="room", kind="fact", text="b", confidence=0.5,
            status="active", sensitivity="public", room_uuid=room_uuid,
        )
        # Same scope/kind, but a different room — should not appear in
        # the room-scoped query.
        other_room = uuid4()
        c = db.create_memory_claim(
            scope="room", kind="fact", text="c", confidence=0.5,
            status="active", sensitivity="public", room_uuid=other_room,
        )
        in_room = db.list_memory_claims(scope="room", room_uuid=room_uuid)
        uuids = {x.uuid for x in in_room}
        assert a.uuid in uuids and b.uuid in uuids
        assert c.uuid not in uuids

        actives = db.list_memory_claims(room_uuid=room_uuid, status="active")
        assert [x.uuid for x in actives] == [b.uuid]
    finally:
        # cleanup both rooms
        db.db.session.query(MemoryClaim).filter(
            MemoryClaim.room_uuid.in_([room_uuid, other_room])
        ).delete()
        db.db.session.commit()


def test_supersede_memory_marks_old_and_creates_new_active(app_ctx, fresh_uuid):
    room_uuid = fresh_uuid
    try:
        old = db.create_memory_claim(
            scope="room", kind="fact", text="old text", confidence=0.6,
            status="active", sensitivity="public", room_uuid=room_uuid,
        )
        new = db.supersede_memory(
            old.uuid,
            new_claim_args=dict(
                scope="room", kind="fact", text="new text", confidence=0.85,
                sensitivity="public", room_uuid=room_uuid,
            ),
            evidence_args=dict(
                provenance="confirmed_by_user", source_type="manual",
            ),
        )
        db.db.session.expire_all()
        old_reloaded = db.get_memory_claim(old.uuid)
        new_reloaded = db.get_memory_claim(new.uuid)
        assert old_reloaded is not None and old_reloaded.status == "superseded"
        assert new_reloaded is not None and new_reloaded.status == "active"
        assert new_reloaded.supersedes_uuid == old.uuid
        assert new_reloaded.text == "new text"
        # The supersession also persisted an evidence row on the new claim.
        ev = db.db.session.query(MemoryEvidence).filter_by(
            memory_uuid=new.uuid
        ).all()
        assert len(ev) == 1
        assert ev[0].provenance == "confirmed_by_user"
    finally:
        _cleanup_claims_and_evidence(room_uuid)


def test_reject_memory_marks_rejected_and_preserves_evidence(app_ctx, fresh_uuid):
    room_uuid = fresh_uuid
    try:
        claim = db.create_memory_claim(
            scope="room", kind="fact", text="dubious claim", confidence=0.7,
            status="candidate", sensitivity="public", room_uuid=room_uuid,
        )
        db.add_memory_evidence(
            memory_uuid=claim.uuid, provenance="inferred_by_model",
            source_type="chat_message", source_id="m1",
        )
        db.reject_memory(
            claim.uuid,
            evidence_args=dict(
                provenance="confirmed_by_user",
                source_type="manual",
                excerpt="operator says this is wrong",
            ),
        )
        db.db.session.expire_all()
        reloaded = db.get_memory_claim(claim.uuid)
        assert reloaded is not None and reloaded.status == "rejected"
        # Both the original inferred evidence AND the rejection evidence remain.
        ev = (
            db.db.session.query(MemoryEvidence)
            .filter_by(memory_uuid=claim.uuid)
            .order_by(MemoryEvidence.id.asc())
            .all()
        )
        assert [r.provenance for r in ev] == [
            "inferred_by_model", "confirmed_by_user",
        ]
    finally:
        _cleanup_claims_and_evidence(room_uuid)


def test_init_db_is_idempotent_against_existing_database(app_ctx):
    """Calling init_db a second time on an already-initialized DB must
    succeed without errors and not destroy data. Catches a class of
    regression where someone adds a non-idempotent ALTER or seed."""
    # Drop a sentinel claim, then re-run init_db twice. The claim must
    # survive both calls.
    sentinel_room = uuid4()
    sentinel = db.create_memory_claim(
        scope="room", kind="fact", text="idempotency sentinel", confidence=0.5,
        status="candidate", sensitivity="public", room_uuid=sentinel_room,
    )
    try:
        db.init_db(app_ctx)
        db.init_db(app_ctx)  # second call must also succeed
        db.db.session.expire_all()
        reloaded = db.get_memory_claim(sentinel.uuid)
        assert reloaded is not None, "init_db erased existing rows"
        assert reloaded.text == "idempotency sentinel"
    finally:
        db.db.session.query(MemoryClaim).filter(
            MemoryClaim.room_uuid == sentinel_room
        ).delete()
        db.db.session.commit()
