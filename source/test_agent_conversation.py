"""Tests for the bounded conversation manager (Phase 0 walking skeleton).

Pure scheduler logic (next_speaker / evaluate_stop) needs no DB or model. The
integration tests drive ConversationManagerAgent.handle directly against the
live test DB (conftest pins rainbox_claude), simulating speaker turns by posting
chat messages + replaying routed-completion payloads — no LM Studio required.
Every DB test deletes the rows it created.
"""

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

import db
from agent_config import (
    CONVERSATION_MANAGER_UUID,
    PERSONA_BENNY_UUID,
    PERSONA_EGON_UUID,
)
from agent_conversation import (
    ConversationManagerAgent,
    StopDecision,
    build_conversation_prompt,
    evaluate_stop,
    next_speaker,
)
from db.models import ChatMessage, ConversationRun, Inbox

MANAGER = CONVERSATION_MANAGER_UUID


# --- pure logic (no DB, no model) --------------------------------------------


def _participants():
    return [
        {"slug": "egon", "agent_uuid": str(PERSONA_EGON_UUID), "turn_order": 1, "persona_id": "p-egon"},
        {"slug": "benny", "agent_uuid": str(PERSONA_BENNY_UUID), "turn_order": 2, "persona_id": "p-benny"},
    ]


def test_next_speaker_round_robin():
    ps = _participants()
    assert next_speaker(ps, 0)["slug"] == "egon"
    assert next_speaker(ps, 1)["slug"] == "benny"
    assert next_speaker(ps, 2)["slug"] == "egon"  # wraps
    assert next_speaker(ps, 3)["slug"] == "benny"


def test_next_speaker_orders_by_turn_order_not_list_order():
    ps = [
        {"slug": "b", "agent_uuid": str(uuid4()), "turn_order": 2},
        {"slug": "a", "agent_uuid": str(uuid4()), "turn_order": 1},
    ]
    assert next_speaker(ps, 0)["slug"] == "a"


def _run(turn=0, max_turns=6, stop_requested=False, stop_phrases=None):
    policy = {"max_turns": max_turns}
    if stop_phrases is not None:
        policy["stop_phrases"] = stop_phrases
    return SimpleNamespace(
        turn=turn, turn_policy=policy, stop_requested=stop_requested, created_at=None
    )


def _msg(text, sender_uuid, kind="message", sender_type="agent"):
    return {"text": text, "sender_uuid": str(sender_uuid), "kind": kind, "sender_type": sender_type}


def test_evaluate_stop_max_turns():
    d = evaluate_stop(_run(turn=6, max_turns=6), [], MANAGER)
    assert d.should_stop and d.reason == "max_turns" and d.status == "finished"


def test_evaluate_stop_operator_stop_wins():
    d = evaluate_stop(_run(turn=1, stop_requested=True), [], MANAGER)
    assert d.should_stop and d.reason == "operator_stop" and d.status == "stopped"


def test_evaluate_stop_stop_phrase():
    msgs = [_msg("here is the plan DONE", PERSONA_EGON_UUID)]
    d = evaluate_stop(_run(turn=2), msgs, MANAGER)
    assert d.should_stop and d.reason == "stop_phrase"


def test_evaluate_stop_ignores_manager_and_human_for_phrase():
    # A DONE authored by the manager or a human must NOT terminate the run.
    msgs = [
        _msg("DONE", MANAGER),
        _msg("DONE", uuid4(), sender_type="human"),
    ]
    d = evaluate_stop(_run(turn=1), msgs, MANAGER)
    assert not d.should_stop


def test_evaluate_stop_continue():
    msgs = [_msg("let us keep going", PERSONA_EGON_UUID)]
    d = evaluate_stop(_run(turn=1, max_turns=6), msgs, MANAGER)
    assert not d.should_stop and d.budget_left == 5


def test_evaluate_stop_min_turns_defers_stop_phrase():
    # A premature DONE is ignored until min_turns is reached.
    msgs = [_msg("here is the plan DONE", PERSONA_EGON_UUID)]
    early = _run(turn=1, max_turns=8)
    early.turn_policy["min_turns"] = 4
    assert not evaluate_stop(early, msgs, MANAGER).should_stop
    late = _run(turn=4, max_turns=8)
    late.turn_policy["min_turns"] = 4
    d = evaluate_stop(late, msgs, MANAGER)
    assert d.should_stop and d.reason == "stop_phrase"


# --- DB-backed tests ----------------------------------------------------------


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
def room(app_ctx):
    human = db.get_human_user()
    room = db.create_chatroom("test-conv", human.uuid, [PERSONA_EGON_UUID, PERSONA_BENNY_UUID])
    created_runs: list[UUID] = []
    yield SimpleNamespace(uuid=room.uuid, register_run=created_runs.append)
    # teardown: remove everything this test created
    db.db.session.query(ChatMessage).filter(ChatMessage.room_uuid == room.uuid).delete()
    for ruuid in created_runs:
        db.db.session.query(ConversationRun).filter(ConversationRun.id == ruuid).delete()
    db.db.session.query(Inbox).filter(
        Inbox.agent_uuid.in_([PERSONA_EGON_UUID, PERSONA_BENNY_UUID, MANAGER])
    ).delete(synchronize_session=False)
    db.db.session.commit()
    db.delete_chatroom(room.uuid)
    db.db.session.commit()


def _make_run(room, max_turns=4, stop_phrases=("DONE",)):
    run = db.create_conversation_run(
        room.uuid,
        [
            {"slug": "egon", "agent_uuid": str(PERSONA_EGON_UUID), "agent_kind": "chat_unstructured", "turn_order": 1, "persona_id": "p-egon"},
            {"slug": "benny", "agent_uuid": str(PERSONA_BENNY_UUID), "agent_kind": "chat_unstructured", "turn_order": 2, "persona_id": "p-benny"},
        ],
        {"max_turns": max_turns, "stop_phrases": list(stop_phrases)},
        last_human_message_id=0,
    )
    room.register_run(run.id)
    return run


def test_claim_tick_cas(room):
    run = _make_run(room)
    assert db.claim_conversation_tick(run.id, 0) is True   # owns it
    assert db.claim_conversation_tick(run.id, 0) is False  # stale duplicate
    assert db.claim_conversation_tick(run.id, 1) is True   # next monotonic value


def test_advance_is_idempotent(room):
    run = _make_run(room)
    db.mark_conversation_turn_in_flight(run.id, 0, PERSONA_EGON_UUID)
    assert db.advance_conversation_if_new(run.id, 10, 0) is True
    # replay same completion → no double advance
    assert db.advance_conversation_if_new(run.id, 10, 0) is False
    fresh = db.get_conversation_run(run.id)
    assert fresh.turn == 1 and fresh.active_turn is None


def _mgr():
    return ConversationManagerAgent(MANAGER, "conversation", lambda m: None)


def test_manager_full_two_turn_run(room):
    run = _make_run(room, max_turns=6)
    mgr = _mgr()

    # start tick → schedules turn 0 (egon)
    out = mgr.handle(1, {"run_uuid": str(run.id), "kind": "tick", "expected_tick_count": 0})
    assert out["enqueued"] == "egon" and out["turn"] == 0
    assert db.get_conversation_run(run.id).active_turn == 0

    # egon speaks, then its completion routes back
    db.post_chat_message(room.uuid, PERSONA_EGON_UUID, "here is the plan", kind="message")
    out = mgr.handle(2, {"from": "persona_egon", "from_journal_id": 100,
                         "input": {"run_uuid": str(run.id), "turn": 0}, "result": {}})
    assert out["enqueued"] == "benny" and out["turn"] == 1

    # benny replies with the stop phrase
    db.post_chat_message(room.uuid, PERSONA_BENNY_UUID, "agreed, first step is X. DONE", kind="message")
    out = mgr.handle(3, {"from": "persona_benny", "from_journal_id": 101,
                         "input": {"run_uuid": str(run.id), "turn": 1}, "result": {}})
    assert out["stopped"] == "stop_phrase"
    assert db.get_conversation_run(run.id).status == "finished"

    # replaying benny's completion does not resurrect or advance the run
    out = mgr.handle(3, {"from": "persona_benny", "from_journal_id": 101,
                         "input": {"run_uuid": str(run.id), "turn": 1}, "result": {}})
    assert "skipped" in out


def test_manager_bounded_by_max_turns(room):
    # personas never say DONE → must stop at max_turns, not loop forever.
    run = _make_run(room, max_turns=2, stop_phrases=("DONE",))
    mgr = _mgr()
    out = mgr.handle(1, {"run_uuid": str(run.id), "kind": "tick", "expected_tick_count": 0})
    assert out["turn"] == 0
    db.post_chat_message(room.uuid, PERSONA_EGON_UUID, "step one", kind="message")
    out = mgr.handle(2, {"from": "persona_egon", "from_journal_id": 200,
                         "input": {"run_uuid": str(run.id), "turn": 0}, "result": {}})
    assert out["turn"] == 1
    db.post_chat_message(room.uuid, PERSONA_BENNY_UUID, "step two", kind="message")
    out = mgr.handle(3, {"from": "persona_benny", "from_journal_id": 201,
                         "input": {"run_uuid": str(run.id), "turn": 1}, "result": {}})
    assert out["stopped"] == "max_turns"


def test_manager_operator_stop(room):
    run = _make_run(room, max_turns=6)
    mgr = _mgr()
    mgr.handle(1, {"run_uuid": str(run.id), "kind": "tick", "expected_tick_count": 0})
    db.request_conversation_stop(run.id)
    tick = db.current_tick_count(run.id)
    out = mgr.handle(2, {"run_uuid": str(run.id), "kind": "tick", "expected_tick_count": tick})
    assert out["stopped"] == "operator_stop"
    assert db.get_conversation_run(run.id).status == "stopped"


def test_manager_human_interruption_pauses(room):
    run = _make_run(room, max_turns=6)
    mgr = _mgr()
    mgr.handle(1, {"run_uuid": str(run.id), "kind": "tick", "expected_tick_count": 0})
    # a human barges in
    human = db.get_human_user()
    db.post_chat_message(room.uuid, human.uuid, "wait, stop", kind="message")
    db.post_chat_message(room.uuid, PERSONA_EGON_UUID, "ok", kind="message")
    out = mgr.handle(2, {"from": "persona_egon", "from_journal_id": 300,
                         "input": {"run_uuid": str(run.id), "turn": 0}, "result": {}})
    assert out["paused"] == "human_interruption"
    assert db.get_conversation_run(run.id).status == "paused"


# --- failed-turn handling (P1) ------------------------------------------------


def test_failed_turn_retries_then_fails(room):
    run = _make_run(room, max_turns=6)
    mgr = _mgr()
    mgr.handle(1, {"run_uuid": str(run.id), "kind": "tick", "expected_tick_count": 0})  # egon, turn 0

    # egon's turn errors → retried (same speaker, same turn, not advanced)
    out = mgr.handle(2, {"from": "persona_egon", "from_journal_id": 100, "state": "failed",
                         "input": {"run_uuid": str(run.id), "turn": 0}, "result": {"error": "boom"}})
    assert out["retried"] == "egon" and out["turn"] == 0 and out["attempt"] == 1
    r = db.get_conversation_run(run.id)
    assert r.turn == 0 and r.active_turn == 0 and r.retry_count == 1 and r.status == "running"

    # second failure exceeds MAX_TURN_RETRIES → whole run fails
    out = mgr.handle(3, {"from": "persona_egon", "from_journal_id": 101, "state": "failed",
                         "input": {"run_uuid": str(run.id), "turn": 0}, "result": {"error": "boom"}})
    assert out["failed"] == "turn_failed"
    assert db.get_conversation_run(run.id).status == "failed"

    # a duplicate failed delivery is ignored (run already terminal)
    out = mgr.handle(3, {"from": "persona_egon", "from_journal_id": 101, "state": "failed",
                         "input": {"run_uuid": str(run.id), "turn": 0}, "result": {}})
    assert "skipped" in out


def test_failed_then_success_advances_normally(room):
    run = _make_run(room, max_turns=6)
    mgr = _mgr()
    mgr.handle(1, {"run_uuid": str(run.id), "kind": "tick", "expected_tick_count": 0})
    # fail once → retry
    mgr.handle(2, {"from": "persona_egon", "from_journal_id": 100, "state": "failed",
                   "input": {"run_uuid": str(run.id), "turn": 0}, "result": {"error": "x"}})
    # now the retry succeeds → advance to turn 1, retry_count resets
    db.post_chat_message(room.uuid, PERSONA_EGON_UUID, "the plan", kind="message")
    out = mgr.handle(3, {"from": "persona_egon", "from_journal_id": 102, "state": "completed",
                         "input": {"run_uuid": str(run.id), "turn": 0}, "result": {}})
    assert out["enqueued"] == "benny" and out["turn"] == 1
    assert db.get_conversation_run(run.id).retry_count == 0


# --- stop / resume / reconcile (P1/P2) ----------------------------------------


def test_stop_paused_run_transitions_to_stopped(room):
    run = _make_run(room)
    db.pause_conversation(run.id, reason="human_interruption")
    assert db.stop_conversation(run.id) == "stopped"
    assert db.get_conversation_run(run.id).status == "stopped"


def test_stop_running_run_sets_flag(room):
    run = _make_run(room)
    assert db.stop_conversation(run.id) == "stopping"
    assert db.get_conversation_run(run.id).stop_requested is True


def test_resume_paused_run(room):
    run = _make_run(room)
    db.mark_conversation_turn_in_flight(run.id, 0, PERSONA_EGON_UUID)
    db.pause_conversation(run.id, reason="human_interruption")
    res = db.resume_conversation(run.id)
    assert res["status"] == "running"
    r = db.get_conversation_run(run.id)
    assert r.status == "running" and r.active_turn is None and r.stop_requested is False


def test_resume_failed_run(room):
    run = _make_run(room)
    db.finish_conversation(run.id, status="failed", reason="turn_failed")
    res = db.resume_conversation(run.id)
    assert res["status"] == "running"
    assert db.get_conversation_run(run.id).status == "running"


def test_resume_running_run_is_noop(room):
    run = _make_run(room)
    res = db.resume_conversation(run.id)
    assert res["status"] == "not_resumable"


def test_resume_stopped_run(room):
    # Stop is pause/play, not a hard terminal: a stopped run must be resumable.
    run = _make_run(room)
    assert db.stop_conversation(run.id) == "stopping"
    db.finish_conversation(run.id, status="stopped", reason="operator_stop")
    res = db.resume_conversation(run.id)
    assert res["status"] == "running"
    r = db.get_conversation_run(run.id)
    assert r.status == "running" and r.stop_requested is False and r.active_turn is None


def test_resume_finished_run_is_noop(room):
    run = _make_run(room)
    db.finish_conversation(run.id, status="finished", reason="max_turns")
    assert db.resume_conversation(run.id)["status"] == "not_resumable"


def test_resume_resets_wall_clock_anchor(room):
    # A run created long ago and resumed must NOT instantly trip the wall-clock
    # budget: idle (paused/stopped) time should not count against it.
    import time as _time

    import sqlalchemy as sa

    run = _make_run(room, max_turns=8)
    db.db.session.execute(
        sa.update(ConversationRun).where(ConversationRun.id == run.id).values(
            turn_policy={"max_turns": 8, "max_wall_clock_seconds": 600, "stop_phrases": ["DONE"]},
            budget={"wall_clock_started_at": _time.time() - 9999},
        )
    )
    db.db.session.commit()
    stale = db.get_conversation_run(run.id)
    assert evaluate_stop(stale, [], MANAGER).reason == "wall_clock"  # would finish before the fix

    db.finish_conversation(run.id, status="stopped", reason="operator_stop")
    db.resume_conversation(run.id)
    fresh = db.get_conversation_run(run.id)
    assert evaluate_stop(fresh, [], MANAGER).should_stop is False  # anchor reset → in budget


def _age_active_turn(run_uuid, seconds):
    import sqlalchemy as sa
    from datetime import UTC, datetime, timedelta
    db.db.session.execute(
        sa.update(ConversationRun)
        .where(ConversationRun.id == run_uuid)
        .values(active_turn_enqueued_at=datetime.now(UTC) - timedelta(seconds=seconds))
    )
    db.db.session.commit()


def test_reconcile_recent_turn_is_too_recent(room):
    run = _make_run(room)
    db.mark_conversation_turn_in_flight(run.id, 0, PERSONA_EGON_UUID)
    assert db.reconcile_conversation(run.id, timeout_seconds=120)["status"] == "too_recent"


def test_reconcile_stale_turn_retries_then_fails(room):
    run = _make_run(room)
    db.mark_conversation_turn_in_flight(run.id, 0, PERSONA_EGON_UUID)
    _age_active_turn(run.id, 9999)
    res = db.reconcile_conversation(run.id, timeout_seconds=120)
    assert res["status"] == "retry"
    r = db.get_conversation_run(run.id)
    assert r.active_turn is None and r.retry_count == 1 and r.status == "running"

    # a second stale turn exceeds the retry budget → failed
    db.mark_conversation_turn_in_flight(run.id, 0, PERSONA_EGON_UUID)
    _age_active_turn(run.id, 9999)
    res = db.reconcile_conversation(run.id, timeout_seconds=120)
    assert res["status"] == "failed"
    assert db.get_conversation_run(run.id).status == "failed"


def test_reconcile_no_active_turn_is_noop(room):
    run = _make_run(room)
    assert db.reconcile_conversation(run.id)["status"] == "noop"


# --- conversation context builder (P2) ----------------------------------------


def test_build_conversation_prompt_pure():
    p = build_conversation_prompt("Egon", ["Benny"], 2, 8, "Benny: hi")
    assert "You are Egon" in p
    assert "with Benny" in p
    assert "turn 2 of at most 8" in p
    assert "Benny: hi" in p
    assert "DONE" in p


def test_agent_uses_conversation_context(room):
    # The persona chat agent must build conversation context (preamble + turn +
    # other participant + recent transcript) when the payload carries run_uuid.
    from agent_chat_unstructured import UnstructuredChatAgent

    run = _make_run(room)
    db.post_chat_message(room.uuid, PERSONA_BENNY_UUID, "here is my idea", kind="message")
    # a manager debug row must NOT leak into the persona's context
    db.post_chat_message(room.uuid, MANAGER, '{"next":"egon"}', content_type="json",
                         kind="debug-conversation")
    ag = UnstructuredChatAgent(PERSONA_EGON_UUID, "persona_egon", lambda m: None)
    ag.setup()
    prompt = ag.user_prompt({"run_uuid": str(run.id), "turn": 0, "room_uuid": str(room.uuid)})
    assert "You are Egon" in prompt
    assert "Benny" in prompt
    assert "here is my idea" in prompt
    assert '{"next":"egon"}' not in prompt
