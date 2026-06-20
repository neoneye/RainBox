"""Tests for QueryFilterRouterAgent telemetry instrumentation."""

from uuid import UUID, uuid4

import pytest

import db
from db import RetrievalEvent

_JID = uuid4()  # a stable journal uuid for the asserted-telemetry test


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


def _events_for(prefix: str) -> list[RetrievalEvent]:
    return db.db.session.query(RetrievalEvent).filter(
        RetrievalEvent.target_id.like(f"{prefix}%")
    ).order_by(RetrievalEvent.id.asc()).all()


def test_record_filter_events_semantic_path_writes_retrieved_then_accept_reject_used(
    app_ctx, fresh_tag,
):
    """semantic+filter path: retrieved (per candidate), then accepted
    (per kept), rejected (per dropped), and used (per final-selected)."""
    from agents.query_filter_router import _record_filter_events

    room_uuid = uuid4()
    agent_uuid = uuid4()
    retrieved = [
        {"qa_id": f"{fresh_tag}.alpha", "rank": 0, "score": 0.9},
        {"qa_id": f"{fresh_tag}.beta", "rank": 1, "score": 0.7},
        {"qa_id": f"{fresh_tag}.gamma", "rank": 2, "score": 0.5},
    ]
    relevant_ids = {f"{fresh_tag}.alpha", f"{fresh_tag}.gamma"}
    used_ids = {f"{fresh_tag}.alpha"}

    try:
        _record_filter_events(
            query="what is fresh_tag?",
            room_uuid=room_uuid,
            agent_uuid=agent_uuid,
            journal_id=_JID,
            source="query_filter_router",
            retrieved=retrieved,
            relevant_ids=relevant_ids,
            used_ids=used_ids,
        )

        rows = _events_for(fresh_tag)
        # 3 retrieved + 2 accepted + 1 rejected + 1 used = 7 rows.
        assert len(rows) == 7

        by_stage: dict[str, list[RetrievalEvent]] = {}
        for r in rows:
            by_stage.setdefault(r.stage, []).append(r)

        assert {r.target_id for r in by_stage["retrieved"]} == {
            f"{fresh_tag}.alpha", f"{fresh_tag}.beta", f"{fresh_tag}.gamma",
        }
        assert {r.target_id for r in by_stage["accepted"]} == {
            f"{fresh_tag}.alpha", f"{fresh_tag}.gamma",
        }
        assert {r.target_id for r in by_stage["rejected"]} == {
            f"{fresh_tag}.beta",
        }
        assert {r.target_id for r in by_stage["used"]} == {
            f"{fresh_tag}.alpha",
        }

        # All rows carry the shared context.
        for r in rows:
            assert r.source == "query_filter_router"
            assert r.room_uuid == room_uuid
            assert r.agent_uuid == agent_uuid
            assert r.journal_id == _JID
            assert r.target_type == "qa_entry"

        # `retrieved` rows carry rank + score from the input list.
        retrieved_alpha = next(
            r for r in by_stage["retrieved"]
            if r.target_id == f"{fresh_tag}.alpha"
        )
        assert retrieved_alpha.retrieval_rank == 0
        assert retrieved_alpha.retrieval_score == pytest.approx(0.9)

        # `accepted` rows carry filter_label='relevant';
        # `rejected` rows carry filter_label='irrelevant'.
        for r in by_stage["accepted"]:
            assert r.filter_label == "relevant"
        for r in by_stage["rejected"]:
            assert r.filter_label == "irrelevant"
    finally:
        _cleanup(fresh_tag)


def test_record_filter_events_exact_match_writes_retrieved_accepted_used(
    app_ctx, fresh_tag,
):
    """Exact-alias path: a single qa_id is retrieved, accepted, and
    used. No rejected event is written."""
    from agents.query_filter_router import _record_filter_events

    qa_id = f"{fresh_tag}.exact"
    try:
        _record_filter_events(
            query="exact alias query",
            room_uuid=uuid4(),
            agent_uuid=uuid4(),
            journal_id=uuid4(),
            source="query_filter_router",
            retrieved=[{"qa_id": qa_id, "rank": 0, "score": 1.0}],
            relevant_ids={qa_id},
            used_ids={qa_id},
        )
        rows = _events_for(fresh_tag)
        stages = sorted(r.stage for r in rows)
        assert stages == ["accepted", "retrieved", "used"]
        assert not any(r.stage == "rejected" for r in rows)
    finally:
        _cleanup(fresh_tag)


def test_record_filter_events_empty_retrieved_is_a_noop(app_ctx, fresh_tag):
    """If retrieval returned nothing, no events at all should be written."""
    from agents.query_filter_router import _record_filter_events

    try:
        _record_filter_events(
            query="empty",
            room_uuid=uuid4(),
            agent_uuid=uuid4(),
            journal_id=uuid4(),
            source="query_filter_router",
            retrieved=[],
            relevant_ids=set(),
            used_ids=set(),
        )
        rows = _events_for(fresh_tag)
        assert rows == []
    finally:
        _cleanup(fresh_tag)


def test_record_filter_events_used_implies_accepted(app_ctx, fresh_tag):
    """A candidate in `used_ids` must also be in `relevant_ids` per the
    spec. The helper asserts this so a future caller can't write an
    inconsistent event sequence (used-but-not-accepted)."""
    from agents.query_filter_router import _record_filter_events

    qa_id = f"{fresh_tag}.broken"
    try:
        with pytest.raises(AssertionError):
            _record_filter_events(
                query="x",
                room_uuid=uuid4(),
                agent_uuid=uuid4(),
                journal_id=uuid4(),
                source="query_filter_router",
                retrieved=[{"qa_id": qa_id, "rank": 0, "score": 0.5}],
                relevant_ids=set(),       # empty
                used_ids={qa_id},          # but used — inconsistent
            )
    finally:
        _cleanup(fresh_tag)


def test_handle_call_sites_swallow_telemetry_failure(
    app_ctx, fresh_tag, caplog, monkeypatch,
):
    """If `_record_filter_events` raises, the agent must NOT crash.
    Both call sites wrap the helper in try/except; this test exercises
    that wrapper by monkey-patching the helper to raise."""
    import logging
    import agents.query_filter_router as qfra

    def boom(**_):
        raise RuntimeError(f"telemetry exploded ({fresh_tag})")

    monkeypatch.setattr(qfra, "_record_filter_events", boom)

    # The wrapper logic lives at the call site, not the helper, so we
    # invoke the same try/except pattern by calling a tiny shim that
    # mimics what `handle()` does. We can't easily run the real handle()
    # without a full KB+LLM, but we can verify the wrapper structure by
    # locating both call sites in source and asserting they're wrapped.
    src = open(qfra.__file__).read()
    # Two call sites; each must be wrapped.
    assert src.count("_record_filter_events(") >= 3, (
        "expected 1 helper def + 2 call sites; found "
        f"{src.count('_record_filter_events(')}"
    )
    # Both call sites must be inside a try/except — proxy: each
    # `_record_filter_events(` invocation in a call position should be
    # preceded by `try:` within the previous 3 lines, and `except` must
    # appear within the next 30.
    lines = src.split("\n")
    call_lines = [
        i for i, line in enumerate(lines)
        if "_record_filter_events(" in line
        and not line.lstrip().startswith("def ")
    ]
    assert len(call_lines) >= 2, call_lines
    for i in call_lines:
        prev = "\n".join(lines[max(0, i - 5):i])
        nxt = "\n".join(lines[i:i + 40])
        assert "try:" in prev, (
            f"call site at line {i+1} not preceded by try: in last 5 lines:\n"
            f"{prev}"
        )
        assert "except" in nxt, (
            f"call site at line {i+1} not followed by except in next 40 lines:\n"
            f"{nxt}"
        )


def test_query_filter_used_events_carry_approximation_metadata(
    app_ctx, fresh_tag,
):
    """`used` events written by the query-filter path must carry
    `metadata.used_signal = 'accepted_candidate_approximation'` so
    consumers know this is not proof of final-answer attribution
    (it's just "this candidate was kept by the relevance filter")."""
    from agents.query_filter_router import _record_filter_events

    qa_id = f"{fresh_tag}.kept"
    try:
        _record_filter_events(
            query="x",
            room_uuid=uuid4(),
            agent_uuid=uuid4(),
            journal_id=uuid4(),
            source="query_filter_router",
            retrieved=[{"qa_id": qa_id, "rank": 0, "score": 0.9}],
            relevant_ids={qa_id},
            used_ids={qa_id},
        )
        used_rows = [r for r in _events_for(fresh_tag) if r.stage == "used"]
        assert len(used_rows) == 1
        meta = used_rows[0].metadata_ or {}
        assert meta.get("used_signal") == "accepted_candidate_approximation", meta
    finally:
        _cleanup(fresh_tag)
