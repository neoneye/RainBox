"""Tests for memory_retrieval: deterministic token-overlap retrieval and
formatting. No LM Studio dependency.

Per-test cleanup tags rows with `subject="test-<uuid>"` and deletes by
that tag (cascade removes evidence rows).
"""

import json  # noqa: E402 — used by Task 2 tests
from uuid import UUID, uuid4

import pytest

import db
from db import MemoryClaim

from memory.retrieval import RetrievedMemory, retrieve_memories


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
def fresh_subject() -> str:
    return f"test-{uuid4()}"


def _cleanup(subject: str) -> None:
    db.db.session.query(MemoryClaim).filter(
        MemoryClaim.subject == subject
    ).delete()
    db.db.session.commit()


def _claim_uuids(memories: list[RetrievedMemory]) -> set[UUID]:
    return {m.uuid for m in memories}


def test_retrieve_returns_only_active(app_ctx, fresh_subject):
    try:
        active = db.create_memory_claim(
            scope="global", kind="fact",
            text="cats are mammals",
            confidence=0.9, status="active", sensitivity="public",
            subject=fresh_subject,
        )
        # Each of these non-active rows should be excluded regardless of text overlap.
        for status in ("candidate", "superseded", "rejected", "expired"):
            db.create_memory_claim(
                scope="global", kind="fact",
                text=f"cats are mammals ({status})",
                confidence=0.9, status=status, sensitivity="public",
                subject=fresh_subject,
            )
        out = retrieve_memories("cats", agent_uuid=None, room_uuid=None)
        ours = [m for m in out if m.uuid == active.uuid]
        non_active = [
            m for m in out
            if m.uuid != active.uuid
            and m.text.startswith("cats are mammals (")
        ]
        assert len(ours) == 1
        assert non_active == []
    finally:
        _cleanup(fresh_subject)


def test_retrieve_excludes_secret_unless_flagged(app_ctx, fresh_subject):
    try:
        secret_claim = db.create_memory_claim(
            scope="global", kind="fact",
            text="the launch codes are 1234",
            confidence=1.0, status="active", sensitivity="secret",
            subject=fresh_subject,
        )
        # Default call: secret excluded.
        out = retrieve_memories("launch codes", agent_uuid=None, room_uuid=None)
        assert secret_claim.uuid not in _claim_uuids(out)
        # Explicit opt-in: secret included.
        out_secret = retrieve_memories(
            "launch codes", agent_uuid=None, room_uuid=None,
            include_secret=True,
        )
        assert secret_claim.uuid in _claim_uuids(out_secret)
    finally:
        _cleanup(fresh_subject)


def test_room_scoped_memories_outrank_global(app_ctx, fresh_subject):
    try:
        room = uuid4()
        # Two identical-text memories — one room-scoped, one global.
        room_claim = db.create_memory_claim(
            scope="room", kind="fact",
            text="paris is the capital of france",
            confidence=0.5, status="active", sensitivity="public",
            room_uuid=room, subject=fresh_subject,
        )
        global_claim = db.create_memory_claim(
            scope="global", kind="fact",
            text="paris is the capital of france",
            confidence=0.9, status="active", sensitivity="public",
            subject=fresh_subject,
        )
        out = retrieve_memories(
            "paris", agent_uuid=None, room_uuid=room,
        )
        order = [m.uuid for m in out if m.uuid in {room_claim.uuid, global_claim.uuid}]
        # Even though the global claim has higher confidence, the room match wins.
        assert order[0] == room_claim.uuid
    finally:
        _cleanup(fresh_subject)


def test_higher_confidence_outranks_lower_when_relevance_equal(app_ctx, fresh_subject):
    try:
        high = db.create_memory_claim(
            scope="global", kind="fact",
            text="dogs bark",
            confidence=0.95, status="active", sensitivity="public",
            subject=fresh_subject,
        )
        low = db.create_memory_claim(
            scope="global", kind="fact",
            text="dogs bark",
            confidence=0.3, status="active", sensitivity="public",
            subject=fresh_subject,
        )
        out = retrieve_memories("dogs", agent_uuid=None, room_uuid=None)
        order = [m.uuid for m in out if m.uuid in {high.uuid, low.uuid}]
        assert order == [high.uuid, low.uuid]
    finally:
        _cleanup(fresh_subject)


def test_retrieval_limit_default_is_six(app_ctx, fresh_subject):
    try:
        for i in range(10):
            db.create_memory_claim(
                scope="global", kind="fact",
                text=f"fact about widgets number {i}",
                confidence=0.5, status="active", sensitivity="public",
                subject=fresh_subject,
            )
        out = retrieve_memories(
            "widgets", agent_uuid=None, room_uuid=None,
        )
        ours = [m for m in out if m.text.startswith("fact about widgets")]
        assert len(ours) <= 6
    finally:
        _cleanup(fresh_subject)


def test_retrieval_returns_empty_when_no_overlap(app_ctx, fresh_subject):
    try:
        db.create_memory_claim(
            scope="global", kind="fact", text="cats are mammals",
            confidence=1.0, status="active", sensitivity="public",
            subject=fresh_subject,
        )
        out = retrieve_memories(
            "lasagna recipe steps", agent_uuid=None, room_uuid=None,
        )
        ours = [m for m in out if m.text == "cats are mammals"]
        assert ours == []
    finally:
        _cleanup(fresh_subject)


from memory.retrieval import format_memory_context, record_memory_use  # noqa: E402


def _retrieved(
    *,
    text: str,
    kind: str = "fact",
    scope: str = "global",
    confidence: float = 1.0,
    sensitivity: str = "private",
    evidence: list[str] | None = None,
    reason: str = "token_overlap",
) -> RetrievedMemory:
    return RetrievedMemory(
        uuid=uuid4(),
        text=text,
        kind=kind,
        scope=scope,
        confidence=confidence,
        sensitivity=sensitivity,
        reason=reason,
        evidence_summary=evidence or [],
    )


def test_format_memory_context_includes_provenance_labels():
    memories = [
        _retrieved(
            text="Username prefers concise technical answers.",
            kind="preference",
            sensitivity="private",
            evidence=["confirmed_by_user"],
        ),
        _retrieved(
            text="QueryAgent answers from JSONL plus pgvector.",
            kind="project_decision",
            sensitivity="public",
            evidence=["observed_from_source"],
        ),
    ]
    out = format_memory_context(memories)
    assert "Relevant remembered facts:" in out
    assert "confirmed_by_user" in out
    assert "observed_from_source" in out
    assert "Username prefers concise technical answers." in out
    assert "QueryAgent answers from JSONL plus pgvector." in out


def test_format_memory_context_returns_empty_string_when_no_memories():
    assert format_memory_context([]) == ""


def test_format_memory_context_includes_uuid_only_when_requested():
    """The chat-context block omits uuids (noise for a reply), but the assistant's
    query_memory needs them to point at a specific memory (e.g. to forget it)."""
    m = _retrieved(text="I prefer pasta.")
    assert str(m.uuid) not in format_memory_context([m])            # default: clean
    with_id = format_memory_context([m], include_uuid=True)
    assert str(m.uuid) in with_id and "I prefer pasta." in with_id  # assistant: targetable


def test_record_memory_use_posts_debug_memory_row(app_ctx, fresh_subject):
    """Verify the audit row is posted as kind='debug-memory' with a JSON
    payload listing the memory uuids and provenance labels."""
    try:
        # Set up a chatroom + chat user (agent) to post the row.
        human = db.get_human_user()
        assert human is not None
        room = db.create_chatroom(
            f"memreturn-{uuid4().hex[:6]}", human.uuid, [],
        )
        try:
            agent_uuid = uuid4()
            # Pre-seed a chat_user for the agent so post_chat_message's sender
            # is recognized.
            agent_user = db.ChatUser(
                uuid=agent_uuid, name=f"test-agent-{uuid4().hex[:6]}",
                user_type="agent",
            )
            db.db.session.add(agent_user)
            db.db.session.commit()

            memories = [
                _retrieved(
                    text="hi", evidence=["confirmed_by_user"],
                    confidence=1.0,
                ),
            ]
            posted = record_memory_use(
                journal_id=None,
                room_uuid=room.uuid,
                agent_uuid=agent_uuid,
                query="hello",
                memories=memories,
            )
            assert posted is not None
            assert posted.kind == "debug-memory"
            payload = json.loads(posted.text)
            assert payload["query"] == "hello"
            assert len(payload["memories"]) == 1
            entry = payload["memories"][0]
            assert entry["memory_uuid"] == str(memories[0].uuid)
            assert entry["confidence"] == 1.0
            assert entry["provenance"] == ["confirmed_by_user"]
            assert entry["reason"] == "token_overlap"
        finally:
            db.db.session.query(db.Chatroom).filter(
                db.Chatroom.uuid == room.uuid
            ).delete()
            db.db.session.query(db.ChatUser).filter(
                db.ChatUser.uuid == agent_uuid
            ).delete()
            db.db.session.commit()
    finally:
        _cleanup(fresh_subject)


def test_record_memory_use_returns_none_when_empty():
    """No memories — no audit row to post. Returns None and does not
    insert a row (verified by the caller not having a row to post)."""
    # Pass dummy uuids; the function should short-circuit before any DB write.
    assert record_memory_use(
        journal_id=None,
        room_uuid=uuid4(),
        agent_uuid=uuid4(),
        query="anything",
        memories=[],
    ) is None


from datetime import UTC, datetime, timedelta  # noqa: E402 — used by Task 3 tests


def test_retrieval_excludes_active_claim_with_past_expires_at(app_ctx, fresh_subject):
    try:
        past = datetime.now(UTC) - timedelta(hours=1)
        expired = db.create_memory_claim(
            scope="global", kind="fact",
            text="this memory expired an hour ago",
            confidence=1.0, status="active", sensitivity="public",
            subject=fresh_subject, expires_at=past,
        )
        out = retrieve_memories("memory expired", agent_uuid=None, room_uuid=None)
        assert expired.uuid not in {m.uuid for m in out}
    finally:
        _cleanup(fresh_subject)


def test_retrieval_includes_active_claim_with_future_expires_at(app_ctx, fresh_subject):
    try:
        future = datetime.now(UTC) + timedelta(hours=1)
        live = db.create_memory_claim(
            scope="global", kind="fact",
            text="this memory expires in the future",
            confidence=1.0, status="active", sensitivity="public",
            subject=fresh_subject, expires_at=future,
        )
        out = retrieve_memories(
            "memory expires future", agent_uuid=None, room_uuid=None,
        )
        assert live.uuid in {m.uuid for m in out}
    finally:
        _cleanup(fresh_subject)


def test_retrieval_includes_active_claim_with_null_expires_at(app_ctx, fresh_subject):
    try:
        ever = db.create_memory_claim(
            scope="global", kind="fact",
            text="this memory has no expiry at all",
            confidence=1.0, status="active", sensitivity="public",
            subject=fresh_subject,
            # expires_at omitted = NULL
        )
        out = retrieve_memories(
            "memory expiry", agent_uuid=None, room_uuid=None,
        )
        assert ever.uuid in {m.uuid for m in out}
    finally:
        _cleanup(fresh_subject)
