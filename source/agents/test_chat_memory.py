"""Stubbed-LLM tests for StructuredChatAgent's memory integration.

`_structured_call` is monkeypatched so no LM Studio is required. We
verify (1) the constructed user prompt includes a relevant remembered
fact, (2) irrelevant memories are not included, and (3) a debug-memory
row is posted when memories are injected.
"""

import json
from uuid import UUID, uuid4

import pytest

import db
from agents.chat_structured import StructuredChatAgent, ChatAgentResponse
from db import ChatMessage, MemoryClaim


def _build_retrieved_memory(*, memory_uuid: UUID, score: float = 0.0):
    """Construct a minimal RetrievedMemory for telemetry-helper tests.

    The actual `RetrievedMemory` dataclass uses `uuid` (not
    `memory_uuid`) and has no `score` field; the telemetry helper
    reads the uuid attribute and pulls score via
    `getattr(..., "score", None)`. Tests pass `memory_uuid=` here for
    readability and this builder routes it to the dataclass `uuid`
    field. `score` is currently unused by the dataclass; we keep the
    kwarg so future RetrievedMemory schemas can adopt it without
    rewriting every test."""
    from memory.retrieval import RetrievedMemory
    import dataclasses
    fields = {f.name: f for f in dataclasses.fields(RetrievedMemory)}
    kwargs: dict = {}
    if "uuid" in fields:
        kwargs["uuid"] = memory_uuid
    if "memory_uuid" in fields:
        kwargs["memory_uuid"] = memory_uuid
    if "score" in fields:
        kwargs["score"] = score
    for name, f in fields.items():
        if name in kwargs:
            continue
        if f.default is not dataclasses.MISSING:
            continue
        if f.default_factory is not dataclasses.MISSING:
            continue
        type_ = f.type
        if "str" in str(type_):
            kwargs[name] = "placeholder"
        elif "int" in str(type_):
            kwargs[name] = 0
        elif "float" in str(type_):
            kwargs[name] = 0.0
        elif "list" in str(type_):
            kwargs[name] = []
        elif "dict" in str(type_):
            kwargs[name] = {}
        else:
            kwargs[name] = None
    return RetrievedMemory(**kwargs)


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


def _room_with_chat_agent(human_uuid):
    """Create a chatroom, register a synthetic chat-agent user, return
    (room_uuid, agent_uuid). Caller is responsible for cleanup of the
    chatroom (cascades to messages) and the ChatUser row."""
    agent_uuid = uuid4()
    agent_user = db.ChatUser(
        uuid=agent_uuid, name=f"chat-{uuid4().hex[:6]}", user_type="agent",
    )
    db.db.session.add(agent_user)
    db.db.session.flush()
    room = db.create_chatroom(
        f"chatmem-{uuid4().hex[:6]}", human_uuid, [agent_uuid],
    )
    return room.uuid, agent_uuid


def _cleanup_room(room_uuid, agent_uuid):
    # StructuredChatAgent.user_prompt() now writes RetrievalEvent rows (WP06
    # Finding 2). Delete them first so they don't accumulate across
    # test runs — same-room rows have no FK on chatroom so they
    # would otherwise leak.
    db.db.session.query(db.RetrievalEvent).filter(
        db.RetrievalEvent.room_uuid == room_uuid
    ).delete(synchronize_session=False)
    db.db.session.query(db.Chatroom).filter(
        db.Chatroom.uuid == room_uuid
    ).delete()
    db.db.session.query(db.ChatUser).filter(
        db.ChatUser.uuid == agent_uuid
    ).delete()
    db.db.session.commit()


def test_user_prompt_includes_relevant_memory(app_ctx, fresh_subject):
    human = db.get_human_user()
    assert human is not None
    room_uuid, agent_uuid = _room_with_chat_agent(human.uuid)
    try:
        db.create_memory_claim(
            scope="global", kind="preference",
            text="Username prefers concise technical answers.",
            confidence=1.0, status="active", sensitivity="private",
            subject=fresh_subject,
        )
        db.post_chat_message(
            room_uuid, human.uuid,
            "What's the username style for technical answers?",
        )
        agent = StructuredChatAgent(
            agent_uuid=agent_uuid, name="chat_structured", send=lambda _: None,
        )
        prompt = agent.user_prompt({"room_uuid": str(room_uuid)})
        assert "Relevant remembered facts:" in prompt
        assert "Username prefers concise technical answers." in prompt
        # The current-message marker is still present and clearly separate.
        assert "Current message:" in prompt
        # And the memory block precedes the transcript.
        assert prompt.index("Relevant remembered facts:") < prompt.index("Current message:")
    finally:
        _cleanup_room(room_uuid, agent_uuid)
        _cleanup(fresh_subject)


def test_user_prompt_omits_irrelevant_memory(app_ctx, fresh_subject):
    human = db.get_human_user()
    assert human is not None
    room_uuid, agent_uuid = _room_with_chat_agent(human.uuid)
    try:
        db.create_memory_claim(
            scope="global", kind="fact",
            text="cats are mammals",
            confidence=1.0, status="active", sensitivity="public",
            subject=fresh_subject,
        )
        db.post_chat_message(
            room_uuid, human.uuid, "what's the weather in Paris today?",
        )
        agent = StructuredChatAgent(
            agent_uuid=agent_uuid, name="chat_structured", send=lambda _: None,
        )
        prompt = agent.user_prompt({"room_uuid": str(room_uuid)})
        # No token overlap with "weather Paris today" — memory block omitted.
        assert "Relevant remembered facts:" not in prompt
        assert "cats are mammals" not in prompt
    finally:
        _cleanup_room(room_uuid, agent_uuid)
        _cleanup(fresh_subject)


def test_handle_posts_debug_memory_row_when_memories_injected(
    app_ctx, fresh_subject, monkeypatch,
):
    human = db.get_human_user()
    assert human is not None
    room_uuid, agent_uuid = _room_with_chat_agent(human.uuid)
    try:
        db.create_memory_claim(
            scope="global", kind="preference",
            text="Username prefers concise technical answers.",
            confidence=1.0, status="active", sensitivity="private",
            subject=fresh_subject,
        )
        db.post_chat_message(
            room_uuid, human.uuid, "What is the username style?",
        )

        # Stub the LLM so no LM Studio call is made.
        def fake_structured_call(self, user_prompt):
            return ChatAgentResponse(
                reply_format="markdown",
                reply_content="(stub reply)",
            )

        monkeypatch.setattr(StructuredChatAgent, "_structured_call", fake_structured_call)
        agent = StructuredChatAgent(
            agent_uuid=agent_uuid, name="chat_structured", send=lambda _: None,
        )
        agent.handle(journal_id=0, payload={"room_uuid": str(room_uuid)})

        debug_rows = (
            db.db.session.query(ChatMessage)
            .filter(
                ChatMessage.room_uuid == room_uuid,
                ChatMessage.kind == "debug-memory",
            )
            .all()
        )
        assert len(debug_rows) == 1
        payload = json.loads(debug_rows[0].text)
        assert any(
            m["memory_uuid"] for m in payload["memories"]
        ), "debug-memory payload should list at least one memory"
    finally:
        _cleanup_room(room_uuid, agent_uuid)
        _cleanup(fresh_subject)


def test_handle_does_not_post_debug_memory_when_no_memories(
    app_ctx, monkeypatch,
):
    human = db.get_human_user()
    assert human is not None
    room_uuid, agent_uuid = _room_with_chat_agent(human.uuid)
    try:
        db.post_chat_message(
            room_uuid, human.uuid, "completely unrelated question",
        )

        def fake_structured_call(self, user_prompt):
            return ChatAgentResponse(
                reply_format="markdown", reply_content="(stub reply)",
            )

        monkeypatch.setattr(StructuredChatAgent, "_structured_call", fake_structured_call)
        agent = StructuredChatAgent(
            agent_uuid=agent_uuid, name="chat_structured", send=lambda _: None,
        )
        agent.handle(journal_id=0, payload={"room_uuid": str(room_uuid)})

        debug_rows = (
            db.db.session.query(ChatMessage)
            .filter(
                ChatMessage.room_uuid == room_uuid,
                ChatMessage.kind == "debug-memory",
            )
            .all()
        )
        assert debug_rows == [], "no memories matched -> no debug-memory row"
    finally:
        _cleanup_room(room_uuid, agent_uuid)


def test_user_prompt_excludes_diagnostic_rows(app_ctx, fresh_subject):
    """Diagnostic rows (kind in {debug-memory, debug-query, debug-router,
    progress, thinking}) must never appear in the chat agent's user
    prompt. They are operator-only audit content."""
    human = db.get_human_user()
    assert human is not None
    room_uuid, agent_uuid = _room_with_chat_agent(human.uuid)
    try:
        db.post_chat_message(
            room_uuid, human.uuid, "hello there",
        )
        # Diagnostic rows of every existing kind.
        db.post_chat_message(
            room_uuid, agent_uuid,
            '{"memories":[{"memory_uuid":"deadbeef-aaaa","reason":"x"}]}',
            "json", kind="debug-memory",
        )
        db.post_chat_message(
            room_uuid, agent_uuid,
            '{"query":"x","match":null}', "json", kind="debug-query",
        )
        db.post_chat_message(
            room_uuid, agent_uuid, "thinking…", kind="thinking",
        )
        db.post_chat_message(
            room_uuid, agent_uuid, "step 1 of 3", kind="progress",
        )
        # Another real human message after the diagnostics.
        db.post_chat_message(
            room_uuid, human.uuid, "what was that?",
        )

        agent = StructuredChatAgent(
            agent_uuid=agent_uuid, name="chat_structured", send=lambda _: None,
        )
        prompt = agent.user_prompt({"room_uuid": str(room_uuid)})

        # None of the diagnostic content reaches the prompt.
        assert "debug-memory" not in prompt
        assert "memories" not in prompt or "Relevant remembered facts:" in prompt
        assert "match" not in prompt or "match" in "what was that?"
        assert "thinking" not in prompt
        assert "step 1 of 3" not in prompt

        # Current message is the latest real human message.
        assert "Current message:" in prompt
        current_idx = prompt.index("Current message:")
        assert "what was that?" in prompt[current_idx:]
    finally:
        _cleanup_room(room_uuid, agent_uuid)


def test_user_prompt_current_message_skips_trailing_diagnostic(app_ctx, fresh_subject):
    """If the most recent DB row is a diagnostic row (e.g., a debug-memory
    posted just before the agent re-runs handle), the prompt's Current
    message must still point at the latest real chat message."""
    human = db.get_human_user()
    assert human is not None
    room_uuid, agent_uuid = _room_with_chat_agent(human.uuid)
    try:
        db.post_chat_message(
            room_uuid, human.uuid, "the actual question",
        )
        db.post_chat_message(
            room_uuid, agent_uuid,
            '{"memories":[]}', "json", kind="debug-memory",
        )

        agent = StructuredChatAgent(
            agent_uuid=agent_uuid, name="chat_structured", send=lambda _: None,
        )
        prompt = agent.user_prompt({"room_uuid": str(room_uuid)})

        assert "Current message:" in prompt
        current_idx = prompt.index("Current message:")
        assert "the actual question" in prompt[current_idx:]
        assert "debug-memory" not in prompt
        assert "memories" not in prompt or "Relevant remembered facts:" in prompt
    finally:
        _cleanup_room(room_uuid, agent_uuid)


def test_chat_memory_retrieval_writes_retrieved_and_used_events(
    app_ctx, fresh_subject,
):
    """Every memory `retrieve_memories` returns must produce a paired
    (retrieved, used) RetrievalEvent in Phase 1, since all retrieved
    memories are currently injected into the prompt context."""
    from memory.retrieval import _record_memory_telemetry

    room_uuid = uuid4()
    agent_uuid = uuid4()
    mem_a_uuid = uuid4()
    mem_b_uuid = uuid4()
    mems = [
        _build_retrieved_memory(memory_uuid=mem_a_uuid, score=0.9),
        _build_retrieved_memory(memory_uuid=mem_b_uuid, score=0.7),
    ]
    try:
        _record_memory_telemetry(
            query=f"{fresh_subject} hello",
            room_uuid=room_uuid,
            agent_uuid=agent_uuid,
            journal_id=42,
            retrieval_limit=6,
            include_secret=False,
            memories=mems,
        )

        rows = db.db.session.query(db.RetrievalEvent).filter(
            db.RetrievalEvent.room_uuid == room_uuid
        ).all()
        assert len(rows) == 4  # 2 retrieved + 2 used
        by_stage: dict = {}
        for r in rows:
            by_stage.setdefault(r.stage, []).append(r)
        assert set(by_stage.keys()) == {"retrieved", "used"}
        for r in rows:
            assert r.target_type == "memory_claim"
            assert r.source == "chat_memory_retrieval"
            assert r.target_id in {str(mem_a_uuid), str(mem_b_uuid)}
        # Retrieved-stage rows carry rank + score from the inputs.
        retr_rows = sorted(
            by_stage["retrieved"], key=lambda r: r.retrieval_rank
        )
        assert retr_rows[0].retrieval_rank == 0
        # Used-stage rows carry filter_label='relevant' so consumers can
        # distinguish them from query-filter accepted-approximation rows.
        for r in by_stage["used"]:
            assert r.filter_label == "relevant"
        # Metadata carries retrieval_limit + include_secret context.
        for r in rows:
            assert (r.metadata_ or {}).get("retrieval_limit") == 6
            assert (r.metadata_ or {}).get("include_secret") is False
    finally:
        db.db.session.query(db.RetrievalEvent).filter(
            db.RetrievalEvent.room_uuid == room_uuid
        ).delete(synchronize_session=False)
        db.db.session.commit()


def test_chat_memory_telemetry_empty_memories_is_noop(app_ctx):
    from memory.retrieval import _record_memory_telemetry
    _record_memory_telemetry(
        query="x",
        room_uuid=uuid4(),
        agent_uuid=uuid4(),
        journal_id=None,
        retrieval_limit=6,
        include_secret=False,
        memories=[],
    )
    # No assertion — just verifies the helper returns cleanly with
    # no commits/rolls.


def test_chat_memory_telemetry_call_site_is_wrapped_for_isolation():
    """If `_record_memory_telemetry` raises, the agent must NOT crash.
    Verify by source inspection that the call site in user_prompt() is
    wrapped in try/except — same pattern as WP05's QFR telemetry."""
    import memory.retrieval as ac
    src = open(ac.__file__).read()
    lines = src.split("\n")
    call_lines = [
        i for i, line in enumerate(lines)
        if "_record_memory_telemetry(" in line
        and not line.lstrip().startswith("def ")
    ]
    assert len(call_lines) >= 1, "expected at least one call site"
    for i in call_lines:
        prev = "\n".join(lines[max(0, i - 5):i])
        nxt = "\n".join(lines[i:i + 40])
        assert "try:" in prev, (
            f"call site at line {i+1} not wrapped: {prev}"
        )
        assert "except" in nxt, (
            f"no except clause near call site at line {i+1}"
        )


def test_user_prompt_signature_accepts_journal_id():
    """Regression for WP06 fix: user_prompt() must accept journal_id."""
    import inspect
    from agents.chat_structured import StructuredChatAgent
    sig = inspect.signature(StructuredChatAgent.user_prompt)
    assert "journal_id" in sig.parameters, sig.parameters


def test_chat_memory_telemetry_call_site_uses_local_journal_id():
    """Regression: the call site of _record_memory_telemetry must
    pass `journal_id=journal_id` (the local), not the dead
    `getattr(self, '_journal_id', None)` pattern."""
    import memory.retrieval as ac
    src = open(ac.__file__).read()
    # The dead-getattr pattern must be gone.
    assert "_journal_id" not in src, (
        "found '_journal_id' lingering; the dead-getattr pattern "
        "should be replaced with the journal_id parameter."
    )
    # And the helper must receive the local journal_id.
    import re
    m = re.search(
        r"_record_memory_telemetry\([^)]*journal_id\s*=\s*journal_id",
        src, re.DOTALL,
    )
    assert m, "expected _record_memory_telemetry(journal_id=journal_id, ...)"


def test_chat_memory_telemetry_uses_actual_retrieval_limit():
    """Regression: retrieval_limit and include_secret passed to the
    telemetry helper must be the same locals that were passed to
    retrieve_memories — otherwise drift bugs silently lie about what
    was retrieved."""
    import memory.retrieval as ac
    src = open(ac.__file__).read()
    import re
    # Look for `retrieve_memories(...limit=<name>...)` and verify the
    # same name appears as `retrieval_limit=<name>` in the immediately
    # following _record_memory_telemetry call.
    m = re.search(
        r"retrieve_memories\([^)]*limit=(\w+)",
        src, re.DOTALL,
    )
    assert m, "retrieve_memories no longer passes an explicit limit"
    limit_local = m.group(1)
    assert re.search(
        rf"_record_memory_telemetry\([^)]*retrieval_limit\s*=\s*{limit_local}",
        src, re.DOTALL,
    ), (
        f"telemetry helper should use the same `{limit_local}` local "
        f"as retrieve_memories; otherwise drift is possible."
    )
