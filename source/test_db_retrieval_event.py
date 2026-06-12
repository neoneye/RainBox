"""Tests for the retrieval_event table + record_retrieval_event helper."""

from uuid import uuid4

import pytest

import db
from db import RetrievalEvent


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
def fresh_tag() -> str:
    return f"test-{uuid4().hex[:8]}"


def _cleanup(prefix: str) -> None:
    db.db.session.query(RetrievalEvent).filter(
        RetrievalEvent.target_id.like(f"{prefix}%")
    ).delete(synchronize_session=False)
    db.db.session.commit()


def test_record_retrieval_event_writes_a_row(app_ctx, fresh_tag):
    try:
        event = db.record_retrieval_event(
            target_type="qa_entry",
            target_id=f"{fresh_tag}.example",
            stage="retrieved",
            query="what time is it",
            room_uuid=uuid4(),
            agent_uuid=uuid4(),
            journal_id=42,
            source="query_filter_router",
            retrieval_rank=0,
            retrieval_score=0.87,
            filter_label=None,
            metadata={"k": 5},
        )
        assert event.uuid is not None
        reloaded = db.db.session.query(RetrievalEvent).filter_by(
            uuid=event.uuid
        ).first()
        assert reloaded.target_type == "qa_entry"
        assert reloaded.stage == "retrieved"
        assert reloaded.retrieval_rank == 0
        assert reloaded.retrieval_score == pytest.approx(0.87)
        assert reloaded.metadata_ == {"k": 5}
        assert reloaded.filter_label is None
    finally:
        _cleanup(fresh_tag)


def test_record_retrieval_event_accepts_all_five_stages(app_ctx, fresh_tag):
    """All five stages from the spec must be valid."""
    try:
        for stage in ("retrieved", "accepted", "rejected", "used", "downvoted"):
            db.record_retrieval_event(
                target_type="qa_entry",
                target_id=f"{fresh_tag}.{stage}",
                stage=stage,
                source="query_filter_router",
            )
        rows = db.db.session.query(RetrievalEvent).filter(
            RetrievalEvent.target_id.like(f"{fresh_tag}%")
        ).all()
        assert {r.stage for r in rows} == {
            "retrieved", "accepted", "rejected", "used", "downvoted",
        }
    finally:
        _cleanup(fresh_tag)


def test_record_retrieval_event_rejects_unknown_stage(app_ctx, fresh_tag):
    """The CHECK constraint must reject made-up stage values."""
    from sqlalchemy.exc import IntegrityError
    try:
        with pytest.raises(IntegrityError):
            db.record_retrieval_event(
                target_type="qa_entry",
                target_id=f"{fresh_tag}.bogus",
                stage="not_a_real_stage",
                source="query_filter_router",
            )
        db.db.session.rollback()
    finally:
        _cleanup(fresh_tag)


def test_record_retrieval_event_rejects_unknown_target_type(
    app_ctx, fresh_tag,
):
    from sqlalchemy.exc import IntegrityError
    try:
        with pytest.raises(IntegrityError):
            db.record_retrieval_event(
                target_type="bogus_target",
                target_id=f"{fresh_tag}.x",
                stage="retrieved",
                source="query_filter_router",
            )
        db.db.session.rollback()
    finally:
        _cleanup(fresh_tag)


def test_record_retrieval_event_filter_label_constraint(
    app_ctx, fresh_tag,
):
    """filter_label is nullable but must be one of the enum values when set."""
    from sqlalchemy.exc import IntegrityError
    try:
        # Valid labels pass.
        for label in (None, "relevant", "irrelevant", "unknown"):
            db.record_retrieval_event(
                target_type="qa_entry",
                target_id=f"{fresh_tag}.label-{label}",
                stage="accepted",
                filter_label=label,
            )
        # Invalid label fails.
        with pytest.raises(IntegrityError):
            db.record_retrieval_event(
                target_type="qa_entry",
                target_id=f"{fresh_tag}.label-bogus",
                stage="accepted",
                filter_label="probably_relevant",
            )
        db.db.session.rollback()
    finally:
        _cleanup(fresh_tag)


def test_list_retrieval_events_filter_by_target(app_ctx, fresh_tag):
    try:
        for i in range(3):
            db.record_retrieval_event(
                target_type="qa_entry",
                target_id=f"{fresh_tag}.target-a",
                stage="retrieved",
                retrieval_rank=i,
            )
        db.record_retrieval_event(
            target_type="qa_entry",
            target_id=f"{fresh_tag}.target-b",
            stage="retrieved",
        )

        rows = db.list_retrieval_events(
            target_type="qa_entry",
            target_id=f"{fresh_tag}.target-a",
        )
        assert len(rows) == 3
        assert all(r.target_id == f"{fresh_tag}.target-a" for r in rows)
    finally:
        _cleanup(fresh_tag)


def test_record_retrieval_event_commit_false_defers_commit(
    app_ctx, fresh_tag,
):
    """With commit=False, the row is flushed (visible in-session) but
    not committed. The caller must commit explicitly."""
    try:
        event = db.record_retrieval_event(
            target_type="qa_entry",
            target_id=f"{fresh_tag}.deferred",
            stage="retrieved",
            commit=False,
        )
        # Flush gave us a uuid/id, and the row is visible in this session.
        assert event.uuid is not None
        in_session = db.db.session.query(RetrievalEvent).filter_by(
            uuid=event.uuid
        ).first()
        assert in_session is not None
        # Caller commits to make it durable.
        db.db.session.commit()
        # Verify post-commit it's still there.
        post_commit = db.db.session.query(RetrievalEvent).filter_by(
            uuid=event.uuid
        ).first()
        assert post_commit is not None
    finally:
        _cleanup(fresh_tag)


def test_list_retrieval_events_default_limit_is_1000(app_ctx, fresh_tag):
    """The default `limit` should be 1000, not unbounded. None means
    'unbounded, caller knows what they're doing.'"""
    import inspect
    sig = inspect.signature(db.list_retrieval_events)
    assert sig.parameters["limit"].default == 1000, sig.parameters["limit"]
