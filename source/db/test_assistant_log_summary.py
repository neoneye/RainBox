"""The run summarizer's call on the run's own stream.

The digest at the top of /assistant is produced by a model call like any
other — one with a system prompt worth reading when the digest is wrong. It
has no step row, runs after the run is finished, and so had nowhere to be
shown. Here it is derived from its `llm_call` row.

Rows are tagged with a caller no other test or real call uses and cleaned up
after, so the suite stays safe against a database holding real calls.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

import db
from db import LlmCall

T0 = datetime(2026, 8, 24, 20, 30, 0, tzinfo=UTC)


def _at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


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
def caller(app_ctx) -> str:
    """The summarizer's caller name, restored after the test.

    The lookup keys on the real name, so the rows a test writes have to carry
    it. They are removed again by uuid.
    """
    written: list = []
    yield written
    if written:
        db.db.session.query(LlmCall).filter(LlmCall.uuid.in_(written)).delete(
            synchronize_session=False)
        db.db.session.commit()


def _summarizer_row(written, *, at, ms=1770, run_uuid=None,
                    system="You summarize a run.", user="Run status: finished",
                    response='{"outcome": "resolved"}') -> LlmCall:
    row = LlmCall(
        uuid=uuid4(), started_at=_at(at),
        finished_at=_at(at + ms / 1000), provider="ollama",
        model="gemma4:e4b", caller=db.SUMMARIZER_CALLER, ok=True,
        prompt_tokens=863, completion_tokens=30, prefill_ms=1500,
        decode_ms=270, total_ms=ms, run_uuid=run_uuid,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        response_text=response)
    db.db.session.add(row)
    db.db.session.commit()
    written.append(row.uuid)
    return row


def _step(action, *, at, ms):
    return SimpleNamespace(
        uuid=uuid4(), action=action, phase="observed", code_driven=True,
        requested_at=_at(at), created_at=_at(at + ms / 1000),
        duration_ms=int(ms), system_prompt="sys", user_prompt="usr",
        model_response="{}", reasoning=None, log=None, error=None,
        args={}, reason="", observation={"data": {}}, observation_preview="",
        model_uuid=None, model_group_uuid=None, input_tokens=10,
        output_tokens=5, rejected_attempts=[], step_index=0, settled_at=None)


def _run(*, summarized_at=None, finished=70.0):
    summary = {"outcome": "resolved", "trigger": "t", "obstacles": []}
    if summarized_at is not None:
        summary["summarized_at"] = _at(summarized_at).isoformat()
    return SimpleNamespace(uuid=uuid4(), started_at=_at(0),
                           finished_at=_at(finished), summary=summary,
                           room_uuid=None)


def _one(events, **match):
    found = [e for e in events
             if all(e.get(k) == v for k, v in match.items())]
    assert len(found) == 1, [(e["kind"], e["label"]) for e in events]
    return found[0]


def test_the_summarizer_s_call_is_on_the_stream(caller):
    """Its output is the first thing the page shows; the call that produced it
    was the one thing the page did not."""
    run = _run()
    _summarizer_row(caller, at=72, run_uuid=run.uuid)

    events = db.run_events(run, [_step("reply", at=0, ms=2000)])

    event = _one(events, kind="llm", label=db.SUMMARY_LABEL)
    assert event["variant"] == "summary"
    assert event["duration_ms"] == 1770


def test_the_summary_call_carries_the_prompts_to_inspect(caller):
    """The reason it is here: a digest that reads wrong is a prompt to read."""
    run = _run()
    _summarizer_row(caller, at=72, run_uuid=run.uuid,
                    system="You summarize a run.", user="Run status: finished")

    event = _one(db.run_events(run, [_step("reply", at=0, ms=2000)]),
                 kind="llm", label=db.SUMMARY_LABEL)

    assert event["payload"]["system_prompt"] == "You summarize a run."
    assert event["payload"]["user_prompt"] == "Run status: finished"
    assert event["payload"]["model_response"] == '{"outcome": "resolved"}'


def test_the_summary_call_reports_what_it_cost(caller):
    run = _run()
    _summarizer_row(caller, at=72, run_uuid=run.uuid)

    event = _one(db.run_events(run, [_step("reply", at=0, ms=2000)]),
                 kind="llm", label=db.SUMMARY_LABEL)

    assert event["kpis"]["input_tokens"] == 863
    assert event["kpis"]["output_tokens"] == 30
    assert event["kpis"]["prefill_ms"] == 1500


def test_a_run_summarized_before_the_linkage_still_finds_its_call(caller):
    """Rows written before the summarizer tagged its run carry no run_uuid.
    The summary records when it was stored, and the call that produced it
    ended a moment earlier — which is enough to find it without rewriting a
    single row that has already been written."""
    run = _run(summarized_at=73.8)
    _summarizer_row(caller, at=72, run_uuid=None)

    event = _one(db.run_events(run, [_step("reply", at=0, ms=2000)]),
                 kind="llm", label=db.SUMMARY_LABEL)

    assert event["payload"]["system_prompt"] == "You summarize a run."


def test_an_unsummarized_run_gets_no_summary_row(caller):
    """A row for a call that was never made would claim work that never ran."""
    run = SimpleNamespace(uuid=uuid4(), started_at=_at(0),
                          finished_at=_at(70), summary=None, room_uuid=None)

    events = db.run_events(run, [_step("reply", at=0, ms=2000)])

    assert not [e for e in events if e["label"] == db.SUMMARY_LABEL]


def test_the_wait_before_the_summarizer_is_named_not_unaccounted(caller):
    """The summarizer is enqueued, so seconds pass between the run finishing
    and its call going out. Left as a hole the gantt would report the queue as
    missing instrumentation, which is the one thing an unaccounted bar is for.
    """
    run = _run(finished=70.0)
    _summarizer_row(caller, at=72, run_uuid=run.uuid)

    events = db.run_events(run, [_step("reply", at=0, ms=2000)])

    wait = _one(events, kind="activity", label=db.SUMMARY_WAIT_LABEL)
    assert wait["start"] == _at(70.0)
    assert wait["duration_ms"] == 2000
    assert not [e for e in events if e["kind"] == "unaccounted"
                and e["start"] >= _at(70.0)]


def test_the_summary_call_is_not_part_of_what_the_turn_cost(caller):
    """It runs after the reply was delivered. Counted in, a short run's model
    seconds could exceed the run's own wall clock — the totals answer what the
    operator waited for."""
    run = _run()
    _summarizer_row(caller, at=72, run_uuid=run.uuid)
    steps = [_step("reply", at=0, ms=2000)]

    stats = db.assistant_run_stats(steps, run=run)

    assert stats["calls"] == 1
    assert stats["input_tokens"] == 10
    assert not [c for c in db.assistant_llm_calls(steps, run=run)
                if c["label"] == db.SUMMARY_LABEL]
