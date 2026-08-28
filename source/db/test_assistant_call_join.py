"""How an `llm_call` row finds the event it belongs to.

The row holds what the step row never has — the prefill/decode split and the
cache reuse — so a call that took sixteen seconds is only explicable once the
two are joined. There are two ways to join them, and the difference is the
point of this module: a row that names its step is matched by RECORD, and one
that does not is matched by how close its start time is.

The record was not available for a long time. A step row is written once the
response is in hand, so while a call was in flight there was no step to tag it
with — and time was all there was. That fails exactly where it matters most: a
step that retried made two calls seconds apart, and no tolerance wide enough to
match a call at all is narrow enough to tell those two apart.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

import db
from db import AssistantRun

T0 = datetime(2026, 8, 23, 23, 4, 12, tzinfo=UTC)


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


def _at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _call_row(*, run_uuid, step_uuid, at, prefill_ms, model="m"):
    db.record_llm_call({
        "started_at": at, "finished_at": at + timedelta(seconds=1),
        "caller": "assistant.decide", "model": model, "provider": "test",
        "run_uuid": run_uuid, "step_uuid": step_uuid,
        "ok": True, "prefill_ms": prefill_ms, "decode_ms": 1,
        "cached_tokens_reported": prefill_ms,
    })


def _cleanup(run_uuid) -> None:
    db.session.query(AssistantRun).filter(
        AssistantRun.uuid == run_uuid).delete()
    db.session.commit()


def _events(run):
    return db.run_events(run, db.assistant_trace_steps(run.uuid))


def test_a_tagged_call_lands_on_the_step_that_made_it(app_ctx):
    """The straightforward case, and the one every run gets from now on."""
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=uuid4(), agent_uuid=uuid4())
    try:
        step = db.open_assistant_step(
            run_uuid=run.uuid, step_index=0, action="memory_query",
            requested_at=_at(0), duration_ms=1000)
        db.settle_assistant_step(step, phase="observed")
        _call_row(run_uuid=run.uuid, step_uuid=step.uuid, at=_at(0),
                  prefill_ms=700)
        calls = [e for e in _events(run) if e["kind"] == "llm"]

        assert len(calls) == 1
        assert calls[0]["kpis"]["prefill_ms"] == 700
        assert calls[0]["kpis"]["cached_tokens"] == 700
    finally:
        _cleanup(run.uuid)


def test_a_retried_step_gives_each_attempt_its_own_row(app_ctx):
    """The case time could not decide.

    A rejected attempt and the answer that replaced it are one step's two
    calls, seconds apart — well inside any tolerance wide enough to match a
    call at all. Both rows name the step, so the pairing is positional within
    it: the attempt that ran first is the one that was thrown away.
    """
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=uuid4(), agent_uuid=uuid4())
    try:
        step = db.open_assistant_step(
            run_uuid=run.uuid, step_index=0, action="reply",
            requested_at=_at(0), duration_ms=3000,
            rejected_attempts=[{"requested_at": _at(0).isoformat(), "ms": 2000,
                                "response": "not json"}])
        db.settle_assistant_step(step, phase="observed")
        # In the order they went out: the refused attempt, then the retry.
        _call_row(run_uuid=run.uuid, step_uuid=step.uuid, at=_at(0),
                  prefill_ms=111)
        _call_row(run_uuid=run.uuid, step_uuid=step.uuid, at=_at(2),
                  prefill_ms=222)
        calls = [e for e in _events(run) if e["kind"] == "llm"]

        assert [c["variant"] for c in calls] == ["rejected", "decide"]
        assert [c["kpis"]["prefill_ms"] for c in calls] == [111, 222]
    finally:
        _cleanup(run.uuid)


def test_an_untagged_row_still_reaches_its_event(app_ctx):
    """Every call recorded before the linkage existed. Nothing was rewritten
    for them, so they keep the time match — a run from before this reads back
    exactly as it did."""
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=uuid4(), agent_uuid=uuid4())
    try:
        step = db.open_assistant_step(
            run_uuid=run.uuid, step_index=0, action="memory_query",
            requested_at=_at(0), duration_ms=1000)
        db.settle_assistant_step(step, phase="observed")
        _call_row(run_uuid=run.uuid, step_uuid=None, at=_at(0),
                  prefill_ms=700)
        calls = [e for e in _events(run) if e["kind"] == "llm"]

        assert calls[0]["kpis"]["prefill_ms"] == 700
    finally:
        _cleanup(run.uuid)


def test_a_row_naming_a_step_this_run_has_no_event_for_falls_back(app_ctx):
    """A tag that matches nothing is not a reason to drop the numbers. The row
    is still this run's, so it goes back into the time match rather than being
    quietly discarded."""
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=uuid4(), agent_uuid=uuid4())
    try:
        step = db.open_assistant_step(
            run_uuid=run.uuid, step_index=0, action="memory_query",
            requested_at=_at(0), duration_ms=1000)
        db.settle_assistant_step(step, phase="observed")
        _call_row(run_uuid=run.uuid, step_uuid=uuid4(), at=_at(0),
                  prefill_ms=700)
        calls = [e for e in _events(run) if e["kind"] == "llm"]

        assert calls[0]["kpis"]["prefill_ms"] == 700
    finally:
        _cleanup(run.uuid)
