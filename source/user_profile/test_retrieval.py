"""Tests for the user profile block: a query-independent digest of active
self-model memory claims, filtered before selection, budgeted, with telemetry.

Deterministic and model-free: selection is non-vector (confidence + recency +
kind preference), so no embedder is needed.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

import db
from db import MemoryClaim, RetrievalEvent
from user_profile.retrieval import (
    MAX_PROFILE_BLOCK_CHARS,
    build_profile_block,
    format_profile_context,
    select_profile_facts,
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


@pytest.fixture
def tag() -> str:
    return f"test-{uuid4()}"


def _claim(tag, text, *, kind="preference", status="active", sensitivity="public",
           confidence=0.9, expires_at=None, scope="global",
           agent_uuid=None, room_uuid=None):
    # `subject` carries the cleanup tag so a test only touches its own rows.
    return db.create_memory_claim(
        scope=scope, kind=kind, text=text, confidence=confidence,
        status=status, sensitivity=sensitivity, subject=tag,
        expires_at=expires_at, agent_uuid=agent_uuid, room_uuid=room_uuid,
    )


def _cleanup(tag):
    rows = db.db.session.query(MemoryClaim).filter(MemoryClaim.subject == tag).all()
    for r in rows:
        db.db.session.query(RetrievalEvent).filter(
            RetrievalEvent.target_id == str(r.uuid)
        ).delete()
    db.db.session.query(MemoryClaim).filter(MemoryClaim.subject == tag).delete()
    db.db.session.commit()


def _uuids(facts):
    return {f.uuid for f in facts}


def test_hard_filters_exclude_forbidden_claims(app_ctx, tag):
    """Secret, expired, candidate, and out-of-scope-room claims never enter the
    profile selection — the Phase 3 'filter before rank' gate."""
    public = _claim(tag, "prefers concise replies")
    secret = _claim(tag, "the api key is hunter2", sensitivity="secret")
    past = datetime.now(UTC) - timedelta(hours=1)
    expired = _claim(tag, "old preference", expires_at=past)
    candidate = _claim(tag, "unconfirmed preference", status="candidate")
    other_room = _claim(tag, "room-scoped preference", scope="room", room_uuid=uuid4())
    try:
        facts = select_profile_facts(agent_uuid=None, room_uuid=uuid4())
        ids = _uuids(facts)
        assert public.uuid in ids
        assert secret.uuid not in ids
        assert expired.uuid not in ids
        assert candidate.uuid not in ids
        assert other_room.uuid not in ids
    finally:
        _cleanup(tag)


def test_kind_preference_and_exclusions(app_ctx, tag):
    """preference / project_decision are self-model kinds and rank first; a plain
    fact is included but lower; episode_summary and procedure never appear."""
    pref = _claim(tag, "likes dark mode", kind="preference", confidence=0.5)
    decision = _claim(tag, "rainbox: facts in postgres", kind="project_decision", confidence=0.5)
    fact = _claim(tag, "lives in copenhagen", kind="fact", confidence=0.99)
    episode = _claim(tag, "summary of a chat", kind="episode_summary")
    procedure = _claim(tag, "how to summarize a pr", kind="procedure")
    try:
        facts = select_profile_facts(agent_uuid=None, room_uuid=None)
        ids = [f.uuid for f in facts]
        assert episode.uuid not in ids
        assert procedure.uuid not in ids
        assert {pref.uuid, decision.uuid, fact.uuid} <= set(ids)
        # Self-model kinds outrank a plain fact even when the fact has higher
        # confidence — kind preference dominates the sort.
        assert ids.index(pref.uuid) < ids.index(fact.uuid)
        assert ids.index(decision.uuid) < ids.index(fact.uuid)
    finally:
        _cleanup(tag)


def test_same_kind_ranked_by_confidence(app_ctx, tag):
    low = _claim(tag, "weak preference", confidence=0.3)
    high = _claim(tag, "strong preference", confidence=0.95)
    try:
        facts = select_profile_facts(agent_uuid=None, room_uuid=None)
        ids = [f.uuid for f in facts]
        assert ids.index(high.uuid) < ids.index(low.uuid)
    finally:
        _cleanup(tag)


def test_block_respects_char_budget(app_ctx, tag):
    """With more active facts than fit, the block stays under budget and includes
    the highest-confidence facts; the rest are dropped (not truncated mid-fact)."""
    big = "x" * 400  # ~4 of these blow MAX_PROFILE_BLOCK_CHARS (1500)
    claims = [
        _claim(tag, f"{big} {i}", confidence=0.9 - i * 0.05) for i in range(6)
    ]
    try:
        block, injected = build_profile_block(agent_uuid=None, room_uuid=None)
        assert len(block) <= MAX_PROFILE_BLOCK_CHARS
        assert 0 < len(injected) < len(claims)
        # Highest-confidence claim is kept; the lowest is dropped.
        inj_ids = _uuids(injected)
        assert claims[0].uuid in inj_ids
        assert claims[-1].uuid not in inj_ids
    finally:
        _cleanup(tag)


def test_empty_profile_yields_empty_block(app_ctx, tag):
    # No claims created for this tag — but other rows may exist, so assert on the
    # formatter contract directly too.
    assert format_profile_context([]) == ""


def test_records_considered_and_injected_telemetry(app_ctx, tag):
    claim = _claim(tag, "prefers metric units")
    try:
        build_profile_block(agent_uuid=None, room_uuid=None, journal_id=uuid4())
        events = (
            db.db.session.query(RetrievalEvent)
            .filter(
                RetrievalEvent.target_type == "memory_claim",
                RetrievalEvent.target_id == str(claim.uuid),
                RetrievalEvent.source == "user_profile.retrieval",
            )
            .all()
        )
        stages = {e.stage for e in events}
        assert "considered" in stages
        assert "injected" in stages
    finally:
        _cleanup(tag)


def test_injected_facts_are_explainable(app_ctx, tag):
    """Every injected fact carries the claim uuid so a trace/UI can link back."""
    claim = _claim(tag, "prefers async communication")
    try:
        _block, injected = build_profile_block(agent_uuid=None, room_uuid=None)
        assert claim.uuid in _uuids(injected)
        fact = next(f for f in injected if f.uuid == claim.uuid)
        assert fact.text == "prefers async communication"
        assert fact.kind == "preference"
    finally:
        _cleanup(tag)


def test_format_includes_kind_tag_and_header(app_ctx, tag):
    claim = _claim(tag, "prefers concise replies")
    try:
        facts = select_profile_facts(agent_uuid=None, room_uuid=None)
        mine = [f for f in facts if f.uuid == claim.uuid]
        block = format_profile_context(mine)
        assert "About the operator" in block
        assert "[preference]" in block
        assert "prefers concise replies" in block
    finally:
        _cleanup(tag)
