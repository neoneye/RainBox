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
from db.models import AgentModelBinding, ModelConfig

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
        db.session.query(LlmCall).filter(LlmCall.uuid.in_(written)).delete(
            synchronize_session=False)
        db.session.commit()


def _summarizer_row(written, *, at, ms=1770, run_uuid=None, group_uuid=None,
                    system="You summarize a run.", user="Run status: finished",
                    response='{"outcome": "resolved"}') -> LlmCall:
    row = LlmCall(
        uuid=uuid4(), started_at=_at(at),
        finished_at=_at(at + ms / 1000), provider="ollama",
        model="gemma4:e4b", caller=db.SUMMARIZER_CALLER, ok=True,
        prompt_tokens=863, completion_tokens=30, prefill_ms=1500,
        decode_ms=270, total_ms=ms, run_uuid=run_uuid,
        model_group_uuid=group_uuid,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        response_text=response)
    db.session.add(row)
    db.session.commit()
    written.append(row.uuid)
    return row


_BINDING_BACKUP: dict = {}
_GROUPS: list = []


def _a_group():
    """A model group of this test's own, removed with the binding.

    With a member: an empty group does not resolve (`resolve_model_group`
    skips one, so the chain falls through to `assistant.default`), and a group
    nothing can answer from is not the case under test.
    """
    config = db.create_model_config(
        model_name=f"test-model-{uuid4().hex[:8]}", arguments={})
    group = db.create_model_group(name=f"test-group-{uuid4().hex[:8]}")
    db.set_model_group_members(group.uuid, [config.uuid])
    _GROUPS.append((group.uuid, config.uuid))
    return group.uuid


def _bind_summarizer(group_uuid, *, at):
    """Point the summarizer's slot at a group, stamped as last changed at
    `at`. The original binding is restored by `_restore_summarizer_binding`."""
    from agents.config import ASSISTANT_RUN_SUMMARIZER_UUID as SLOT

    row = db.session.query(AgentModelBinding).filter(
        AgentModelBinding.agent_uuid == SLOT).one_or_none()
    if row is None:
        row = AgentModelBinding(agent_uuid=SLOT)
        db.session.add(row)
        _BINDING_BACKUP["absent"] = True
    else:
        _BINDING_BACKUP["group"] = row.model_group_uuid
        _BINDING_BACKUP["updated_at"] = row.updated_at
    row.model_group_uuid = group_uuid
    db.session.commit()
    # After the commit: `updated_at` carries an onupdate that would overwrite
    # a value set in the same flush.
    db.session.query(AgentModelBinding).filter(
        AgentModelBinding.agent_uuid == SLOT).update(
            {"updated_at": at}, synchronize_session=False)
    db.session.commit()
    return group_uuid


def _restore_summarizer_binding() -> None:
    from agents.config import ASSISTANT_RUN_SUMMARIZER_UUID as SLOT

    q = db.session.query(AgentModelBinding).filter(
        AgentModelBinding.agent_uuid == SLOT)
    if _BINDING_BACKUP.pop("absent", None):
        q.delete(synchronize_session=False)
    elif _BINDING_BACKUP:
        q.update({"model_group_uuid": _BINDING_BACKUP.pop("group"),
                  "updated_at": _BINDING_BACKUP.pop("updated_at")},
                 synchronize_session=False)
    _BINDING_BACKUP.clear()
    db.session.commit()
    while _GROUPS:
        group_uuid, config_uuid = _GROUPS.pop()
        db.delete_model_group(group_uuid)
        db.session.query(ModelConfig).filter(
            ModelConfig.uuid == config_uuid).delete(synchronize_session=False)
        db.session.commit()


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


def test_the_summary_call_links_to_the_group_that_chose_its_model(caller):
    """Which model summarizes a run is settled by the agent's model GROUP, so
    that is what a reader following the link wants: the binding they would
    change. The config the call landed on is a consequence of it."""
    run = _run()
    group = uuid4()
    _summarizer_row(caller, at=72, run_uuid=run.uuid, group_uuid=group)

    event = _one(db.run_events(run, [_step("reply", at=0, ms=2000)]),
                 kind="llm", label=db.SUMMARY_LABEL)

    assert event["kpis"]["model_group_uuid"] == str(group)


def test_a_call_recorded_before_the_group_was_reads_it_off_the_binding(caller):
    """Rows written before the group was recorded can still be resolved: the
    summarizer's slot names one, and a binding untouched since the call ran is
    the binding the call ran under."""
    run = _run()
    _summarizer_row(caller, at=72, run_uuid=run.uuid, group_uuid=None)
    bound = _bind_summarizer(_a_group(), at=_at(0))
    try:
        event = _one(db.run_events(run, [_step("reply", at=0, ms=2000)]),
                     kind="llm", label=db.SUMMARY_LABEL)

        assert event["kpis"]["model_group_uuid"] == str(bound)
    finally:
        _restore_summarizer_binding()


def test_a_binding_changed_since_the_call_leaves_the_row_unlinked(caller):
    """Today's binding is not evidence about a call that ran before it was
    made. Sending the reader to a group this call never used would be worse
    than the plain model name the row already shows."""
    run = _run()
    _summarizer_row(caller, at=72, run_uuid=run.uuid, group_uuid=None)
    _bind_summarizer(_a_group(), at=_at(600))
    try:
        event = _one(db.run_events(run, [_step("reply", at=0, ms=2000)]),
                     kind="llm", label=db.SUMMARY_LABEL)

        assert event["kpis"]["model_group_uuid"] is None
        assert event["kpis"]["model"] == "gemma4:e4b"
    finally:
        _restore_summarizer_binding()


def test_the_summary_call_does_not_guess_a_model_config(caller):
    """The name a provider answered on identifies no config this app can be
    asked about: the assistant runs on a tuned override, and the bare config of
    the same name is a page it never used."""
    run = _run()
    _summarizer_row(caller, at=72, run_uuid=run.uuid, group_uuid=None)
    _bind_summarizer(None, at=_at(600))
    try:
        event = _one(db.run_events(run, [_step("reply", at=0, ms=2000)]),
                     kind="llm", label=db.SUMMARY_LABEL)

        assert event["kpis"]["model_uuid"] is None
    finally:
        _restore_summarizer_binding()


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
