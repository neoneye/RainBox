"""Tests for the assistant's read-only actions (PR 4) and dispatch.

Each action reuses an existing rainbox surface (memory retrieval, the QueryAgent
Q&A pipeline, the workspace command policy, kanban reads) and returns an
AssistantObservation; the dispatcher owns validation, the output cap, and the
running->observed/failed trace boundary. No writes, no MCP, no generated code.
"""

from uuid import UUID, uuid4

import pytest

import db
from db import AssistantStep, MemoryClaim
from agents.assistant import (
    AssistantActionContext,
    AssistantActionName,
    AssistantAgent,
    AssistantObservation,
    AssistantStepDecision,
    AssistantTurnStep,
    _action_kanban_read,
    _action_query_memory,
    _action_workspace_read_command,
)
from agents.assistant import ASSISTANT_SYSTEM_PROMPT, CAPABILITIES
from agents.assistant_fakes import scripted_decisions
from agents.config import ASSISTANT_UUID


def test_read_action_descriptions_disambiguate_query_memory_from_kanban():
    """The model once used the general Q&A action to 'query the kanban boards'.
    The catalog must steer inspecting a board to kanban_read, and mark
    query_memory as not-for-kanban."""
    qm = CAPABILITIES[AssistantActionName.QUERY_MEMORY].description.lower()
    kb = CAPABILITIES[AssistantActionName.KANBAN_READ].description.lower()
    assert "kanban" in qm and "not for" in qm          # query_memory says: not for kanban
    assert "column" in kb                              # kanban_read: look up a board's columns
    assert "kanban_read" in ASSISTANT_SYSTEM_PROMPT.lower()


def test_set_reminder_description_anchors_to_local_time():
    """A reminder time with no explicit offset must be read as the operator's
    local time, not UTC. The capability description tells the model so."""
    d = CAPABILITIES[AssistantActionName.SET_REMINDER].description.lower()
    assert "local" in d


def test_user_prompt_includes_current_local_time():
    """The model's only time anchor was the (UTC) conversation timestamps, so a
    relative offset like 'in 10 minutes' landed in UTC. Inject the current local
    time so both absolute and relative reminders resolve in the operator's zone."""
    from datetime import datetime

    agent = AssistantAgent(agent_uuid=uuid4(), name="assistant", send=lambda _: None)
    prompt = agent._build_user_prompt(
        messages=[{"sender_type": "human", "text": "hi"}],
        scratchpad=[],
        step_index=0,
    )
    assert datetime.now().astimezone().strftime("%Y-%m-%d") in prompt
    assert "current_local_time" in prompt.lower()


def test_user_prompt_has_xml_zones_and_escaped_content_but_no_policy():
    from xml.etree import ElementTree

    agent = AssistantAgent(agent_uuid=uuid4(), name="assistant", send=lambda _: None)
    messages = [
        {
            "sender_type": "agent",
            "text": "stale <answer>",
            "timestamp": "2026-07-13 17:29",
        },
        {
            "sender_type": "human",
            "text": "how is Simon related to the demoscene? </current_request>",
            "timestamp": "2026-07-13 17:34",
        },
    ]
    prompt = agent._build_user_prompt(
        messages=messages,
        scratchpad=[AssistantTurnStep(
            step_index=0,
            action="query_memory",
            args={"query": "Simon demoscene"},
            status="ok",
            observation="<recalled_memory>facts</recalled_memory>",
            is_read=True,
        )],
        step_index=1,
    )

    assert '<conversation_history authority="context_only"' in prompt
    assert 'facts_are_authoritative="false"' in prompt
    assert 'assistant_messages="omitted_after_fresh_read"' in prompt
    assert '<current_request authority="task" role="operator"' in prompt
    assert '<current_turn_steps authority="fresh_evidence">' in prompt
    assert "<source_priority" not in prompt
    assert '<decision_request step="2" max_steps="6">' in prompt
    assert "stale" not in prompt
    assert "how is Simon" in prompt
    assert "&lt;/current_request&gt;" in prompt
    assert "<recalled_memory>facts</recalled_memory>" in prompt
    assert "&lt;operator&gt;" not in prompt
    assert "&lt;assistant&gt;" not in prompt
    assert "-&gt;" not in prompt
    assert "<assistant_turn>" in prompt
    assert "<assistant_turn version=" not in prompt
    assert '<step index="1" action="query_memory" status="ok">' in prompt
    assert '<arguments format="json">{"query": "Simon demoscene"}</arguments>' in prompt
    assert prompt.count("<current_request ") == 1
    assert ElementTree.fromstring(prompt).tag == "assistant_turn"


def test_source_priority_policy_is_in_system_prompt_only():
    assert '<source_priority highest_first="true">' in ASSISTANT_SYSTEM_PROMPT
    assert '<source rank="1">successful current_turn_steps observations</source>' in (
        ASSISTANT_SYSTEM_PROMPT
    )
    assert '<source rank="4">conversation_history (context only)</source>' in (
        ASSISTANT_SYSTEM_PROMPT
    )


def test_turn_event_budget_drops_whole_old_events():
    agent = AssistantAgent(agent_uuid=uuid4(), name="assistant", send=lambda _: None)
    old = AssistantTurnStep(
        step_index=0, action="query_memory", args={}, status="ok",
        observation="x" * (agent.MAX_SCRATCHPAD_CHARS + 100), is_read=True,
    )
    newest = AssistantTurnStep(
        step_index=1, action="kanban_read", args={}, status="ok",
        observation="3 tasks", is_read=True,
    )
    kept, omitted = agent._bounded_turn_events([old, newest])
    assert kept == [newest]
    assert omitted == 1


def test_turn_event_budget_keeps_oversized_newest_event_whole():
    agent = AssistantAgent(agent_uuid=uuid4(), name="assistant", send=lambda _: None)
    newest = AssistantTurnStep(
        step_index=0, action="query_memory", args={}, status="ok",
        observation="x" * (agent.MAX_SCRATCHPAD_CHARS + 100), is_read=True,
    )
    kept, omitted = agent._bounded_turn_events([newest])
    assert kept == [newest]
    assert omitted == 0


def test_turn_event_budget_preserves_events_within_budget():
    agent = AssistantAgent(agent_uuid=uuid4(), name="assistant", send=lambda _: None)
    events = [
        AssistantTurnStep(
            step_index=0, action="kanban_read", args={}, status="ok",
            observation="3 tasks", is_read=True,
        ),
        AssistantTurnStep(
            step_index=1, action="query_memory", args={"query": "x"},
            status="failed", observation="unavailable", is_read=True,
        ),
    ]
    kept, omitted = agent._bounded_turn_events(events)
    assert kept == events
    assert omitted == 0


def test_successful_read_removes_old_assistant_answers_but_keeps_operator_context():
    agent = AssistantAgent(agent_uuid=uuid4(), name="assistant", send=lambda _: None)
    messages = [
        {"sender_type": "human", "text": "earlier operator context"},
        {"sender_type": "agent", "text": "stale factual answer"},
        {"sender_type": "human", "text": "current question"},
    ]
    prompt = agent._build_user_prompt(
        messages=messages,
        scratchpad=[AssistantTurnStep(
            step_index=0, action="query_memory", args={"query": "current question"},
            status="ok", observation="fresh facts", is_read=True,
        )],
        step_index=1,
    )

    assert "earlier operator context" in prompt
    assert "stale factual answer" not in prompt
    assert 'assistant_messages="omitted_after_fresh_read"' in prompt


def test_system_prompt_forbids_claiming_unperformed_writes():
    """Run 19: the model read a task then replied 'successfully moved' with no
    kanban_move_task step. The prompt must forbid claiming a write it didn't perform."""
    p = ASSISTANT_SYSTEM_PROMPT.lower()
    assert "never tell the operator you did something" in p
    assert "reading a task is not moving it" in p


def test_system_prompt_requires_fresh_read_not_chat_history():
    """The model replied from an earlier answer in the transcript instead of
    re-querying: it reused a stored fact that had since become restricted, and
    it repeated a stale live value. The prompt must forbid answering factual or
    live-value questions from earlier messages and require a fresh read action."""
    p = ASSISTANT_SYSTEM_PROMPT.lower()
    assert "do not reuse an answer from an earlier message" in p
    assert "call the matching read action" in p
    assert "after a read action succeeds" in p
    assert "do not repeat the same read" in p
    assert "same\nargs" in p
    assert "source" in p and "current_turn_steps" in p
    assert "conversation_history (context only)" in p


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
def fresh_subject() -> str:
    return f"test-{uuid4()}"


def _cleanup_subject(subject: str) -> None:
    db.db.session.query(MemoryClaim).filter(MemoryClaim.subject == subject).delete()
    db.db.session.commit()


def _ctx() -> AssistantActionContext:
    return AssistantActionContext(
        journal_id=uuid4(), room_uuid=uuid4(), agent_uuid=uuid4(), step_index=0
    )


# --- query_memory -------------------------------------------------------------


def test_query_memory_returns_relevant_fact_and_never_secret(app_ctx, fresh_subject):
    try:
        db.create_memory_claim(
            scope="global", kind="fact",
            text="the deploy host is prod-web-01",
            confidence=0.9, status="active", sensitivity="public",
            subject=fresh_subject,
        )
        db.create_memory_claim(
            scope="global", kind="fact",
            text="the deploy ssh key passphrase is swordfish",
            confidence=1.0, status="active", sensitivity="secret",
            subject=fresh_subject,
        )
        obs = _action_query_memory(_ctx(), {"query": "deploy host"})
        assert obs.ok
        assert "prod-web-01" in obs.text
        assert "swordfish" not in obs.text  # secrets are filtered before ranking
    finally:
        _cleanup_subject(fresh_subject)


def test_query_memory_with_no_matches_is_ok_and_empty(app_ctx):
    obs = _action_query_memory(_ctx(), {"query": "no such topic zzzqqq"})
    assert obs.ok
    assert obs.text  # a human-readable "nothing found" message, not a crash


def test_query_memory_includes_seed_memories_tiered(app_ctx):
    from agents.assistant import _action_query_memory
    from memory.seed_memory import SeedMemory
    def fake_seed(query, **_):
        return [SeedMemory(uuid="up-1", path="p.up", source="upstream", answer="upstream fact", score=0.7),
                SeedMemory(uuid="ov-1", path="p.ov", source="user-overlay", answer="overlay fact", score=0.65)]
    ctx = AssistantActionContext(journal_id=None, room_uuid=uuid4(), agent_uuid=ASSISTANT_UUID, step_index=0)
    obs = _action_query_memory(ctx, {"query": "anything unrelated zzz"}, _seed_retriever=fake_seed)
    assert obs.ok is True
    # user-overlay seed appears before upstream seed
    assert obs.text.index("overlay fact") < obs.text.index("upstream fact")
    # the seed uuids are present (greppable)
    assert "ov-1" in obs.text and "up-1" in obs.text
    # source tag is shown
    assert "user-overlay" in obs.text


def test_query_memory_surfaces_dynamic_handler_answer(app_ctx):
    """query_memory resolves dynamic seed handlers (project status, git status):
    a git-status handler answer must appear in the fenced block."""
    from memory.seed_memory import SeedMemory
    def fake_seed(query, *, qctx, **_):
        return [SeedMemory(uuid="dyn-git", path="dev.git", source="upstream",
                           answer="Working tree clean.", score=0.82, kind="dynamic")]
    obs = _action_query_memory(_ctx(), {"query": "what is the git status"},
                               _seed_retriever=fake_seed)
    assert obs.ok
    assert "Working tree clean." in obs.text
    assert "<recalled_memory" in obs.text


def test_query_memory_loads_seed_kb_before_retrieval(app_ctx, monkeypatch):
    """Regression: the assistant loop never loads the seed KB the way the chat
    route does, so `_entries_by_id` stayed empty and seed Q&A (e.g. family facts)
    silently returned nothing. The action must trigger `_load_kb()` +
    `_ensure_populated()` before seed retrieval, as the old query_qa action did."""
    from memory import seed_memory as qkb
    calls = []
    monkeypatch.setattr(qkb, "_load_kb", lambda: calls.append("load_kb"))
    monkeypatch.setattr(qkb, "_vector_store", lambda: "VS")
    monkeypatch.setattr(qkb, "_ensure_populated", lambda vs: calls.append(("ensure", vs)))
    monkeypatch.setattr(qkb, "retrieve_seed_answers", lambda q, *, qctx: [])
    _action_query_memory(_ctx(), {"query": "who is Gitte"})
    assert "load_kb" in calls              # KB registry loaded
    assert ("ensure", "VS") in calls       # pgvector table ensured populated


def test_query_memory_merges_seed_and_dynamic_without_duplicate_legend(app_ctx, fresh_subject):
    """Seed + dynamic together: seed lines first, then dynamic facts, and the
    '{memory_uuid}, ...' legend appears exactly once (the dynamic block's own
    header/legend must not be re-appended)."""
    from memory.seed_memory import SeedMemory
    def fake_seed(query, **_):
        return [SeedMemory(uuid="ov-1", path="p", source="user-overlay",
                           answer="overlay fact", score=0.7)]
    try:
        db.create_memory_claim(
            scope="global", kind="fact", text="the deploy host is prod-web-01",
            confidence=0.9, status="active", sensitivity="public", subject=fresh_subject)
        obs = _action_query_memory(_ctx(), {"query": "deploy host prod"}, _seed_retriever=fake_seed)
        assert obs.ok
        assert "overlay fact" in obs.text and "prod-web-01" in obs.text   # both present
        assert obs.text.index("overlay fact") < obs.text.index("prod-web-01")  # seed before dynamic
        assert obs.text.count("{memory_uuid}") == 1   # the legend is not duplicated
    finally:
        _cleanup_subject(fresh_subject)


def test_query_memory_observation_is_fenced_when_memories_present(app_ctx, fresh_subject):
    """The query_memory observation must wrap retrieved facts in a recalled_memory
    fence so they enter the model context as untrusted reference data."""
    try:
        db.create_memory_claim(
            scope="global", kind="fact", text="the deploy host is prod-web-01 fenced",
            confidence=0.9, status="active", sensitivity="public", subject=fresh_subject)
        obs = _action_query_memory(_ctx(), {"query": "deploy host fenced"})
        assert obs.ok
        assert "<recalled_memory" in obs.text
        assert obs.text.rstrip().endswith("</recalled_memory>")
    finally:
        _cleanup_subject(fresh_subject)


# --- workspace_read_command ---------------------------------------------------


def test_workspace_read_command_allows_safe_command(app_ctx):
    obs = _action_workspace_read_command(_ctx(), {"command": "pwd"})
    assert obs.ok
    assert obs.data.get("exit_code") == 0


def test_workspace_read_command_blocks_forbidden_command(app_ctx):
    obs = _action_workspace_read_command(_ctx(), {"command": "python -c 'print(1)'"})
    assert obs.ok is False
    assert "blocked" in obs.text.lower()


def test_workspace_read_command_blocks_mutation_command(app_ctx):
    obs = _action_workspace_read_command(_ctx(), {"command": "rm -rf foo"})
    assert obs.ok is False
    assert "blocked" in obs.text.lower()


# --- kanban_read --------------------------------------------------------------


def test_kanban_read_returns_board_state_without_appending_events(app_ctx):
    board = db.kanban_create_board("Assistant read board", "desc")
    bu = UUID(board["uuid"])
    todo = board["columns"][0]["uuid"]
    task = {
        "uuid": str(uuid4()), "columnUuid": todo,
        "title": "ship the thing", "description": "", "agentUuid": None,
    }
    db.kanban_save_board(bu, {**board, "tasks": [task]})
    task_uuid = UUID(task["uuid"])
    events_before = db.kanban_task_events(task_uuid) or []
    try:
        obs = _action_kanban_read(_ctx(), {"board_uuid": str(bu)})
        assert obs.ok
        assert "ship the thing" in obs.text
        events_after = db.kanban_task_events(task_uuid) or []
        assert len(events_after) == len(events_before), "read must not write events"
    finally:
        db.kanban_delete_board(bu)


def test_kanban_read_without_args_lists_boards(app_ctx):
    board = db.kanban_create_board("Listable board", "desc")
    bu = UUID(board["uuid"])
    try:
        obs = _action_kanban_read(_ctx(), {})
        assert obs.ok
        assert "Listable board" in obs.text
    finally:
        db.kanban_delete_board(bu)


def test_kanban_read_board_list_shows_folder_tree(app_ctx):
    """The no-arg listing preserves the folder tree: folders appear with
    their uuids, nesting is indentation, and a board sits under its folder."""
    folder = db.kanban_create_folder("Projects read test")
    sub = db.kanban_create_folder("Active", UUID(folder["uuid"]))
    board = db.kanban_create_board("Tree board", folder_uuid=UUID(sub["uuid"]))
    try:
        obs = _action_kanban_read(_ctx(), {})
        assert obs.ok
        assert f"- [folder] Projects read test ({folder['uuid']})" in obs.text
        assert f"  - [folder] Active ({sub['uuid']})" in obs.text
        assert f"    - Tree board ({board['uuid']}) — 0 task(s)" in obs.text
    finally:
        db.kanban_delete_board(UUID(board["uuid"]))
        db.kanban_delete_folder(UUID(sub["uuid"]))
        db.kanban_delete_folder(UUID(folder["uuid"]))


def test_kanban_read_unknown_board_is_blocked(app_ctx):
    obs = _action_kanban_read(_ctx(), {"board_uuid": str(uuid4())})
    assert obs.ok is False


def test_kanban_read_by_task_uuid_returns_task_and_events(app_ctx):
    board = db.kanban_create_board("Task read board", "desc")
    bu = UUID(board["uuid"])
    todo = board["columns"][0]["uuid"]
    task = {"uuid": str(uuid4()), "columnUuid": todo,
            "title": "fix the bug", "description": "acceptance: tests pass"}
    db.kanban_save_board(bu, {**board, "tasks": [task]})
    tu = UUID(task["uuid"])
    db.kanban_append_event(tu, "comment", actor="human", detail="please prioritize")
    try:
        obs = _action_kanban_read(_ctx(), {"task_uuid": str(tu)})
        assert obs.ok is True
        assert "fix the bug" in obs.text and "please prioritize" in obs.text
        assert obs.data["task_uuid"] == str(tu)
    finally:
        db.kanban_delete_board(bu)


def test_kanban_read_unknown_task_is_blocked(app_ctx):
    obs = _action_kanban_read(_ctx(), {"task_uuid": str(uuid4())})
    assert obs.ok is False


# --- argument validation ------------------------------------------------------


def test_validate_rejects_unsupported_kanban_args(app_ctx):
    """kanban_read takes optional board_uuid / task_uuid; an unknown arg must be a
    traceable validation failure, not a silent fall-through to 'list all boards'."""
    agent = AssistantAgent(agent_uuid=ASSISTANT_UUID, name="assistant", send=lambda _: None)
    bad = AssistantStepDecision(
        reason="x", action=AssistantActionName.KANBAN_READ,
        args={"column_uuid": str(uuid4())},  # not a kanban_read arg
    )
    err = agent._validate_decision(bad)
    assert err and "column_uuid" in err
    # board_uuid, task_uuid, and empty args are all accepted.
    for ok_args in ({"board_uuid": str(uuid4())}, {"task_uuid": str(uuid4())}, {}):
        ok = AssistantStepDecision(
            reason="x", action=AssistantActionName.KANBAN_READ, args=ok_args)
        assert agent._validate_decision(ok) is None


# --- dispatch through the loop ------------------------------------------------


@pytest.fixture
def room(app_ctx):
    human = db.get_human_user()
    assert human is not None
    chatroom = db.create_chatroom(f"act-test-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])
    msg = db.post_chat_message(chatroom.uuid, human.uuid, "do a read")
    try:
        yield chatroom.uuid, msg.uuid
    finally:
        from db import AssistantRun
        db.db.session.query(AssistantRun).filter(
            AssistantRun.room_uuid == chatroom.uuid
        ).delete()
        db.db.session.query(db.Chatroom).filter(
            db.Chatroom.uuid == chatroom.uuid
        ).delete()
        db.db.session.commit()


def _agent() -> AssistantAgent:
    return AssistantAgent(agent_uuid=ASSISTANT_UUID, name="assistant", send=lambda _: None)


def _decision(action: AssistantActionName, **args) -> AssistantStepDecision:
    return AssistantStepDecision(reason="step", action=action, args=args)


def _steps_for(run_id):
    return (
        db.db.session.query(AssistantStep)
        .filter(AssistantStep.run_uuid == run_id)
        .order_by(AssistantStep.id)
        .all()
    )


def test_loop_dispatches_read_action_then_replies(room):
    room_uuid, message_uuid = room
    agent = _agent()
    agent._decide_next_step = scripted_decisions(
        _decision(AssistantActionName.QUERY_MEMORY, query="anything"),
        _decision(AssistantActionName.REPLY, message="All set."),
    )
    result = agent.handle(uuid4(), {"room_uuid": str(room_uuid), "message_uuid": str(message_uuid)})

    assert result["status"] == "finished"
    steps = _steps_for(result["assistant_run_uuid"])
    # One row per step: the read step settles running->observed in place, the
    # reply is a single terminal row.
    assert [s.phase for s in steps] == ["observed", "final"]
    observed = steps[0]
    assert observed.action == "query_memory"
    assert observed.observation_preview is not None


def test_loop_does_not_dispatch_identical_successful_read_twice(room):
    """If the model repeats the exact same read, the loop should not replay the
    action. The earlier observation is already in the scratchpad; the repeat gets
    a steering observation that tells the model to reply."""
    room_uuid, message_uuid = room
    agent = _agent()
    calls = []

    def fake_dispatch(ctx, decision):
        calls.append((ctx.step_index, decision.action.value, dict(decision.args)))
        return AssistantObservation(ok=True, text="remembered fact: Simon used demos")

    agent._dispatch_action = fake_dispatch
    agent._decide_next_step = scripted_decisions(
        _decision(AssistantActionName.QUERY_MEMORY, query="Simon relation to demoscene"),
        _decision(AssistantActionName.QUERY_MEMORY, query="Simon relation to demoscene"),
        _decision(AssistantActionName.REPLY, message="Simon used demos."),
    )

    result = agent.handle(uuid4(), {"room_uuid": str(room_uuid), "message_uuid": str(message_uuid)})

    assert result["status"] == "finished"
    assert calls == [
        (0, "query_memory", {"query": "Simon relation to demoscene"})
    ]
    steps = _steps_for(result["assistant_run_uuid"])
    assert [s.phase for s in steps] == ["observed", "observed", "final"]
    assert "remembered fact: Simon used demos" in (steps[1].observation_preview or "")
    assert "already completed this exact read" in (steps[1].observation_preview or "")


def test_loop_does_not_dispatch_identical_failed_action_twice(room):
    """A failed action with unchanged args cannot produce new evidence, so the
    host should reject its replay before dispatch and steer the next decision."""
    room_uuid, message_uuid = room
    agent = _agent()
    calls = []

    def fake_dispatch(ctx, decision):
        calls.append((ctx.step_index, decision.action.value, dict(decision.args)))
        return AssistantObservation(ok=False, text="backend unavailable")

    agent._dispatch_action = fake_dispatch
    agent._decide_next_step = scripted_decisions(
        _decision(AssistantActionName.QUERY_MEMORY, query="Simon demoscene"),
        _decision(AssistantActionName.QUERY_MEMORY, query="Simon demoscene"),
        _decision(AssistantActionName.REPLY, message="I could not retrieve that fact."),
    )

    result = agent.handle(
        uuid4(), {"room_uuid": str(room_uuid), "message_uuid": str(message_uuid)}
    )

    assert result["status"] == "finished"
    assert calls == [(0, "query_memory", {"query": "Simon demoscene"})]
    steps = _steps_for(result["assistant_run_uuid"])
    assert [s.phase for s in steps] == ["failed", "failed", "final"]
    assert "already failed earlier" in (steps[1].observation_preview or "")


def test_loop_records_failed_action_and_continues(room):
    """A blocked/forbidden read becomes a failed step (trace-before-action: the
    running row is committed first), and the loop continues to a terminal reply."""
    room_uuid, message_uuid = room
    agent = _agent()
    agent._decide_next_step = scripted_decisions(
        _decision(AssistantActionName.WORKSPACE_READ_COMMAND, command="rm -rf /"),
        _decision(AssistantActionName.REPLY, message="Could not do that."),
    )
    result = agent.handle(uuid4(), {"room_uuid": str(room_uuid), "message_uuid": str(message_uuid)})

    assert result["status"] == "finished"
    steps = _steps_for(result["assistant_run_uuid"])
    # The blocked read opens a running row (committed before the action) that
    # settles in place to failed; then a terminal reply row.
    assert [s.phase for s in steps] == ["failed", "final"]
    failed = steps[0]
    assert failed.action == "workspace_read_command"
    assert failed.error and "blocked" in failed.error.lower()


# --- query_memory truncation (per-fact cap + overall budget + uuid full-fetch) ---


def test_query_memory_truncates_large_facts_and_tags_them(app_ctx):
    """A fact longer than the per-fact cap is shortened and tagged truncate1200
    so the model knows it is partial and can fetch the full text by uuid."""
    from memory.seed_memory import SeedMemory
    big = "x" * 3000
    def fake_seed(query, *, qctx, **_):
        return [SeedMemory(uuid="u-big", path="p", source="user-overlay",
                           answer=big, score=0.9, kind="static")]
    obs = _action_query_memory(_ctx(), {"query": "q"}, _seed_retriever=fake_seed)
    assert "u-big, seed/user-overlay, p, truncate1200:" in obs.text
    assert ("x" * 1200) in obs.text and ("x" * 1201) not in obs.text  # capped at 1200
    assert obs.data["truncated"] == 1  # count of shortened facts


def test_query_memory_data_splits_counts_and_tags_dynamic(app_ctx):
    """The trace data separates QA static / QA dynamic / memory counts; a
    dynamic seed entry (a live handler) carries a `dynamic` tag in its line,
    and every seed line carries the entry's `path` so answers whose text alone
    is ambiguous (two uptime strings, a load average) stay tellable apart."""
    from memory.seed_memory import SeedMemory
    def fake_seed(query, *, qctx, **_):
        return [SeedMemory(uuid="u-stat", path="project.overview", source="upstream",
                           answer="a static fact", score=0.9, kind="static"),
                SeedMemory(uuid="u-dyn", path="system.uptime_host", source="upstream",
                           answer="14 cron jobs", score=0.9, kind="dynamic")]
    obs = _action_query_memory(_ctx(), {"query": "q"}, _seed_retriever=fake_seed)
    assert obs.data["qa_static"] == 1
    assert obs.data["qa_dynamic"] == 1
    assert "memory" in obs.data and "memory_uuids" not in obs.data
    # the dynamic seed carries the `dynamic` tag; the static one does not
    assert "u-dyn, seed/upstream, dynamic, system.uptime_host: 14 cron jobs" in obs.text
    assert "u-stat, seed/upstream, project.overview: a static fact" in obs.text


def test_query_memory_seed_without_path_omits_the_path_tag(app_ctx):
    """A seed entry with no `path` renders without a trailing path tag (no
    dangling comma)."""
    from memory.seed_memory import SeedMemory
    def fake_seed(query, *, qctx, **_):
        return [SeedMemory(uuid="u-nopath", path="", source="upstream",
                           answer="a fact", score=0.9, kind="static")]
    obs = _action_query_memory(_ctx(), {"query": "q"}, _seed_retriever=fake_seed)
    assert "u-nopath, seed/upstream: a fact" in obs.text


def test_query_memory_omits_tail_and_notes_it(app_ctx):
    """When more capped facts than fit the budget are retrieved, the tail is
    dropped at a fact boundary (never mid-word) and the omission is noted."""
    from memory.seed_memory import SeedMemory
    def fake_seed(query, *, qctx, **_):
        return [SeedMemory(uuid=f"u{i}", path="p", source="user-overlay",
                           answer="y" * 1200, score=0.9, kind="static") for i in range(15)]
    obs = _action_query_memory(_ctx(), {"query": "q"}, _seed_retriever=fake_seed)
    assert obs.data["omitted"] > 0
    assert "omitted" in obs.text.lower()


def test_query_memory_uuid_returns_full_seed_entry(app_ctx, monkeypatch):
    """query_memory with a uuid returns that one entry in full, untruncated —
    the escape hatch for a fact query_memory shortened."""
    from memory import seed_memory as qkb
    big = "z" * 3000
    monkeypatch.setattr(qkb, "_load_kb", lambda: None)
    monkeypatch.setattr(qkb, "_entries_by_id",
                        {"u1": {"kind": "static", "answer": big, "_source": "user-overlay"}})
    monkeypatch.setattr(qkb, "_unlocked_shields", lambda: set())
    obs = _action_query_memory(_ctx(), {"uuid": "u1"})
    assert obs.data.get("matched") is True
    assert big in obs.text  # full text, not shortened


def test_query_memory_uuid_respects_shield(app_ctx, monkeypatch):
    from memory import seed_memory as qkb
    monkeypatch.setattr(qkb, "_load_kb", lambda: None)
    monkeypatch.setattr(qkb, "_entries_by_id",
                        {"u1": {"kind": "static", "answer": "secret", "shield": "locked", "_source": "user-overlay"}})
    monkeypatch.setattr(qkb, "_unlocked_shields", lambda: set())
    obs = _action_query_memory(_ctx(), {"uuid": "u1"})
    assert obs.data.get("matched") is False
    assert "secret" not in obs.text


def test_system_prompt_explains_truncate_and_uuid_fetch():
    p = ASSISTANT_SYSTEM_PROMPT.lower()
    assert "truncate" in p
    assert '"uuid"' in p or "uuid" in p
