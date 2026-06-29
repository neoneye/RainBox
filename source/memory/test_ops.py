"""Tests for memory_ops: parser, normalizer, finder, dispatcher.

Pure-unit tests run first (no DB); DB-touching tests use the live local
Postgres and clean up by tagging their `subject` field.
"""

from uuid import uuid4

import pytest

import db
from db import MemoryClaim, MemoryEvidence

from memory.ops import (
    MemoryCommand,
    find_memory_matches,  # noqa: E402 — added in Task 2
    normalize_memory_text,
    parse_memory_command,
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
        ctx.pop()


@pytest.fixture
def fresh_subject() -> str:
    return f"test-{uuid4()}"


def _cleanup_by_subject(subject: str) -> None:
    db.db.session.query(MemoryClaim).filter(
        MemoryClaim.subject == subject
    ).delete()
    db.db.session.commit()


# --- parser ------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("remember that the sky is blue", MemoryCommand(kind="remember", text="the sky is blue")),
        ("remember the sky is blue", MemoryCommand(kind="remember", text="the sky is blue")),
        ("REMEMBER that X", MemoryCommand(kind="remember", text="X")),
        ("forget that the sky is blue", MemoryCommand(kind="forget", text="the sky is blue")),
        ("forget the sky is blue", MemoryCommand(kind="forget", text="the sky is blue")),
        ("confirm that the sky is blue", MemoryCommand(kind="confirm", text="the sky is blue")),
        (
            "correct that the sky is red -> the sky is blue",
            MemoryCommand(kind="correct", text="the sky is red", new_text="the sky is blue"),
        ),
        ("what do you remember?", MemoryCommand(kind="recall", text="")),
        ("What do you remember", MemoryCommand(kind="recall", text="")),
        (
            "what do you remember about colors?",
            MemoryCommand(kind="recall", text="colors"),
        ),
        (
            "why do you remember the sky is blue?",
            MemoryCommand(kind="explain", text="the sky is blue"),
        ),
        (
            "which memories did you use?",
            MemoryCommand(kind="used", text=""),
        ),
        (
            "which memory did you use?",
            MemoryCommand(kind="used", text=""),
        ),
        (
            "why did you remember that?",
            MemoryCommand(kind="used", text=""),
        ),
        (
            "why did you say that?",
            MemoryCommand(kind="used", text=""),
        ),
    ],
)
def test_parse_memory_command_recognizes_each_pattern(text, expected):
    assert parse_memory_command(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "what is 2 + 2?",
        "hello",
        "remembering things is hard",
        "remember",            # missing object
        "forget",              # missing object
        "confirm",             # missing object
        "correct that x",      # missing -> NEW
        "remember that",       # bare "that" — would have been remembered as "that"
        "forget that",         # same footgun on forget
        "",
        "   ",
    ],
)
def test_parse_memory_command_returns_none_for_non_commands(text):
    assert parse_memory_command(text) is None


# --- normalizer ---------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("  The   sky    is BLUE  ", "the sky is blue"),
        ("Hello", "hello"),
        ("\nfoo\tbar\n", "foo bar"),
    ],
)
def test_normalize_memory_text(raw, expected):
    assert normalize_memory_text(raw) == expected


# --- finder (live-DB) ---------------------------------------------------------


def test_find_memory_matches_finds_active_by_normalized_text(app_ctx, fresh_subject):
    try:
        db.create_memory_claim(
            scope="global", kind="fact", text="  THE SKY  is blue  ",
            confidence=1.0, status="active", sensitivity="private",
            subject=fresh_subject,
        )
        # Whitespace and case differ; normalization should still match.
        hits = find_memory_matches("the sky is blue")
        matched = [h for h in hits if h.subject == fresh_subject]
        assert len(matched) == 1
    finally:
        _cleanup_by_subject(fresh_subject)


def test_find_memory_matches_filters_by_status(app_ctx, fresh_subject):
    try:
        db.create_memory_claim(
            scope="global", kind="fact", text="x is true",
            confidence=1.0, status="candidate", sensitivity="private",
            subject=fresh_subject,
        )
        # Default status is "active" — candidate shouldn't match.
        assert [h for h in find_memory_matches("x is true") if h.subject == fresh_subject] == []
        # Asking for the candidate status explicitly finds it.
        hits = find_memory_matches("x is true", status="candidate")
        assert [h.subject for h in hits if h.subject == fresh_subject] == [fresh_subject]
    finally:
        _cleanup_by_subject(fresh_subject)


def test_find_memory_matches_returns_empty_when_nothing_matches(app_ctx):
    assert find_memory_matches(f"nonexistent-{uuid4()}") == []


from memory.ops import handle_memory_command  # noqa: E402 — added in Task 3


def _ctx(query: str, message_uuid: str | None = None, room_uuid=None, agent_uuid=None) -> QueryContext:
    """Build a minimal QueryContext for the dispatcher tests."""
    from agents.query_handlers import QueryContext as QC
    return QC(
        room_uuid=room_uuid or uuid4(),
        query=query,
        payload={"message_uuid": message_uuid} if message_uuid else {},
        agent_uuid=agent_uuid or uuid4(),
    )


def _claims_for_subject(subject: str) -> list[MemoryClaim]:
    return (
        db.db.session.query(MemoryClaim)
        .filter(MemoryClaim.subject == subject)
        .order_by(MemoryClaim.id.asc())
        .all()
    )


def _evidence_for(memory_uuid) -> list[MemoryEvidence]:
    return (
        db.db.session.query(MemoryEvidence)
        .filter(MemoryEvidence.memory_uuid == memory_uuid)
        .order_by(MemoryEvidence.id.asc())
        .all()
    )


def test_remember_creates_active_private_claim_and_confirmed_evidence(app_ctx, fresh_subject):
    # The dispatcher doesn't know about `subject` — patch the create call
    # via a thin direct test instead.
    msg_uuid = str(uuid4())
    cmd = parse_memory_command("remember that the sky is blue")
    assert cmd is not None
    ctx = _ctx(query="remember that the sky is blue", message_uuid=msg_uuid)
    try:
        reply = handle_memory_command(ctx, cmd)
        # Tag the claim we just created so cleanup can find it.
        claims = (
            db.db.session.query(MemoryClaim)
            .filter(MemoryClaim.text == "the sky is blue")
            .order_by(MemoryClaim.id.desc())
            .limit(1)
            .all()
        )
        assert claims, f"reply was {reply!r}; no claim created"
        claim = claims[0]
        # Tag for teardown.
        claim.subject = fresh_subject
        db.db.session.commit()

        assert claim.status == "active"
        assert claim.sensitivity == "private"
        assert claim.scope == "global"
        assert claim.kind == "fact"
        assert abs(claim.confidence - 1.0) < 1e-9

        ev = _evidence_for(claim.uuid)
        assert len(ev) == 1
        assert ev[0].provenance == "confirmed_by_user"
        assert ev[0].source_type == "manual"  # ops.py has no operator UUID; manual is correct
        assert ev[0].source_id == msg_uuid
        assert ev[0].excerpt == "remember that the sky is blue"
        assert "the sky is blue" in reply.lower()
    finally:
        _cleanup_by_subject(fresh_subject)


def test_forget_marks_matching_claim_rejected(app_ctx, fresh_subject):
    try:
        claim = db.create_memory_claim(
            scope="global", kind="fact", text="x is true",
            confidence=1.0, status="active", sensitivity="private",
            subject=fresh_subject,
        )
        cmd = parse_memory_command("forget that x is true")
        ctx = _ctx(query="forget that x is true", message_uuid=str(uuid4()))
        reply = handle_memory_command(ctx, cmd)
        db.db.session.expire_all()
        reloaded = db.get_memory_claim(claim.uuid)
        assert reloaded is not None and reloaded.status == "rejected"
        # Evidence row added; the original (none) didn't have any to preserve.
        ev = _evidence_for(claim.uuid)
        assert any(e.provenance == "confirmed_by_user" for e in ev)
        assert "forg" in reply.lower() or "ok" in reply.lower()
    finally:
        _cleanup_by_subject(fresh_subject)


def test_forget_asks_for_clarification_on_multiple_matches(app_ctx, fresh_subject):
    try:
        db.create_memory_claim(
            scope="global", kind="fact", text="x is true",
            confidence=1.0, status="active", sensitivity="private",
            subject=fresh_subject,
        )
        db.create_memory_claim(
            scope="agent", kind="fact", text="x is true",
            confidence=1.0, status="active", sensitivity="private",
            subject=fresh_subject,
        )
        cmd = parse_memory_command("forget x is true")
        ctx = _ctx(query="forget x is true", message_uuid=str(uuid4()))
        reply = handle_memory_command(ctx, cmd)
        # Neither was rejected — both stayed active.
        claims = _claims_for_subject(fresh_subject)
        assert all(c.status == "active" for c in claims)
        assert ("more specific" in reply.lower()) or ("clarif" in reply.lower()) or ("multiple" in reply.lower())
    finally:
        _cleanup_by_subject(fresh_subject)


def test_confirm_activates_existing_candidate(app_ctx, fresh_subject):
    try:
        claim = db.create_memory_claim(
            scope="global", kind="fact", text="x is true",
            confidence=0.5, status="candidate", sensitivity="private",
            subject=fresh_subject,
        )
        db.add_memory_evidence(
            memory_uuid=claim.uuid, provenance="inferred_by_model",
            source_type="chat_message", source_id="m1",
        )
        cmd = parse_memory_command("confirm that x is true")
        ctx = _ctx(query="confirm that x is true", message_uuid=str(uuid4()))
        handle_memory_command(ctx, cmd)
        db.db.session.expire_all()
        reloaded = db.get_memory_claim(claim.uuid)
        assert reloaded.status == "active"
        assert abs(reloaded.confidence - 1.0) < 1e-9
        ev = _evidence_for(claim.uuid)
        # Both the original inferred and the new confirmed-by-user are present.
        provenances = [e.provenance for e in ev]
        assert "inferred_by_model" in provenances
        assert "confirmed_by_user" in provenances
    finally:
        _cleanup_by_subject(fresh_subject)


def test_confirm_creates_memory_when_no_candidate_exists(app_ctx, fresh_subject):
    cmd = parse_memory_command("confirm that brand new fact")
    msg_uuid = str(uuid4())
    ctx = _ctx(query="confirm that brand new fact", message_uuid=msg_uuid)
    try:
        handle_memory_command(ctx, cmd)
        rows = (
            db.db.session.query(MemoryClaim)
            .filter(MemoryClaim.text == "brand new fact")
            .order_by(MemoryClaim.id.desc())
            .limit(1)
            .all()
        )
        assert rows, "confirm should have fallen through to create"
        claim = rows[0]
        claim.subject = fresh_subject
        db.db.session.commit()
        assert claim.status == "active"
        ev = _evidence_for(claim.uuid)
        assert ev and ev[0].provenance == "confirmed_by_user"
    finally:
        _cleanup_by_subject(fresh_subject)


def test_correct_supersedes_old_and_creates_new(app_ctx, fresh_subject):
    try:
        old = db.create_memory_claim(
            scope="global", kind="fact", text="the sky is red",
            confidence=0.8, status="active", sensitivity="private",
            subject=fresh_subject,
        )
        cmd = parse_memory_command("correct that the sky is red -> the sky is blue")
        ctx = _ctx(
            query="correct that the sky is red -> the sky is blue",
            message_uuid=str(uuid4()),
        )
        handle_memory_command(ctx, cmd)
        db.db.session.expire_all()
        old_reloaded = db.get_memory_claim(old.uuid)
        assert old_reloaded.status == "superseded"
        new_rows = (
            db.db.session.query(MemoryClaim)
            .filter(MemoryClaim.text == "the sky is blue")
            .order_by(MemoryClaim.id.desc())
            .limit(1)
            .all()
        )
        assert new_rows
        new = new_rows[0]
        new.subject = fresh_subject  # tag for cleanup
        db.db.session.commit()
        assert new.status == "active"
        assert new.supersedes_uuid == old.uuid
    finally:
        _cleanup_by_subject(fresh_subject)


def test_recall_all_lists_only_active_memories(app_ctx, fresh_subject):
    try:
        db.create_memory_claim(
            scope="global", kind="fact", text="active fact",
            confidence=1.0, status="active", sensitivity="private",
            subject=fresh_subject,
        )
        db.create_memory_claim(
            scope="global", kind="fact", text="candidate fact",
            confidence=0.5, status="candidate", sensitivity="private",
            subject=fresh_subject,
        )
        db.create_memory_claim(
            scope="global", kind="fact", text="rejected fact",
            confidence=0.5, status="rejected", sensitivity="private",
            subject=fresh_subject,
        )
        cmd = parse_memory_command("what do you remember?")
        ctx = _ctx(query="what do you remember?", message_uuid=str(uuid4()))
        reply = handle_memory_command(ctx, cmd)
        assert "active fact" in reply
        assert "candidate fact" not in reply
        assert "rejected fact" not in reply
    finally:
        _cleanup_by_subject(fresh_subject)


def test_explain_reports_claim_text_and_evidence_provenance(app_ctx, fresh_subject):
    try:
        claim = db.create_memory_claim(
            scope="global", kind="fact", text="the sky is blue",
            confidence=1.0, status="active", sensitivity="private",
            subject=fresh_subject,
        )
        db.add_memory_evidence(
            memory_uuid=claim.uuid, provenance="inferred_by_model",
            source_type="chat_message", source_id="m1",
            excerpt="the user mentioned the sky",
        )
        db.add_memory_evidence(
            memory_uuid=claim.uuid, provenance="confirmed_by_user",
            source_type="chat_message", source_id="m2",
            excerpt="confirm that the sky is blue",
        )
        cmd = parse_memory_command("why do you remember the sky is blue?")
        ctx = _ctx(query="why do you remember the sky is blue?", message_uuid=str(uuid4()))
        reply = handle_memory_command(ctx, cmd)
        # The explanation should surface the claim text + both provenance kinds.
        assert "the sky is blue" in reply
        assert "inferred_by_model" in reply
        assert "confirmed_by_user" in reply
    finally:
        _cleanup_by_subject(fresh_subject)


def test_handle_used_reports_memories_from_most_recent_debug_memory(
    app_ctx, fresh_subject,
):
    import json as _json
    human = db.get_human_user()
    assert human is not None
    agent_uuid = uuid4()
    agent_user = db.ChatUser(
        uuid=agent_uuid, name=f"chat-used-{uuid4().hex[:6]}", user_type="agent",
    )
    db.db.session.add(agent_user)
    db.db.session.flush()
    room = db.create_chatroom(
        f"used-{uuid4().hex[:6]}", human.uuid, [agent_uuid],
    )
    try:
        claim = db.create_memory_claim(
            scope="global", kind="preference",
            text="Username prefers concise technical answers.",
            confidence=1.0, status="active", sensitivity="private",
            subject=fresh_subject,
        )
        db.add_memory_evidence(
            memory_uuid=claim.uuid, provenance="confirmed_by_user",
            source_type="manual",
        )
        payload = {
            "query": "what is the username style?",
            "memories": [
                {
                    "memory_uuid": str(claim.uuid),
                    "reason": "token_overlap",
                    "confidence": 1.0,
                    "provenance": ["confirmed_by_user"],
                },
            ],
        }
        db.post_chat_message(
            room.uuid, agent_uuid,
            _json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            "json", kind="debug-memory",
        )
        from agents.query_handlers import QueryContext as QC
        ctx = QC(
            room_uuid=room.uuid, query="which memories did you use?",
            payload={"message_uuid": str(uuid4())},
            agent_uuid=uuid4(),
        )
        reply = handle_memory_command(
            ctx, MemoryCommand(kind="used", text=""),
        )
        assert str(claim.uuid) in reply
        assert "Username prefers concise technical answers." in reply
        assert "confirmed_by_user" in reply
    finally:
        db.db.session.query(db.Chatroom).filter(
            db.Chatroom.uuid == room.uuid
        ).delete()
        db.db.session.query(db.ChatUser).filter(
            db.ChatUser.uuid == agent_uuid
        ).delete()
        db.db.session.commit()
        _cleanup_by_subject(fresh_subject)


def test_handle_used_reports_no_history_when_no_debug_row(app_ctx):
    human = db.get_human_user()
    assert human is not None
    room = db.create_chatroom(
        f"empty-{uuid4().hex[:6]}", human.uuid, [],
    )
    try:
        from agents.query_handlers import QueryContext as QC
        ctx = QC(
            room_uuid=room.uuid, query="which memories did you use?",
            payload={"message_uuid": str(uuid4())},
            agent_uuid=uuid4(),
        )
        reply = handle_memory_command(
            ctx, MemoryCommand(kind="used", text=""),
        )
        assert "no" in reply.lower() or "haven't" in reply.lower() or "none" in reply.lower()
    finally:
        db.db.session.query(db.Chatroom).filter(
            db.Chatroom.uuid == room.uuid
        ).delete()
        db.db.session.commit()
