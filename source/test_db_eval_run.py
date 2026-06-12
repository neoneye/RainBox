"""Tests for the eval_run / eval_result tables and helpers.

Live Postgres. Cleanup uses a per-test `name` tag on EvalRun (or by
walking the run uuid for its results)."""

from uuid import uuid4

import pytest

import db
from db import EvalCase, EvalResult, EvalRun


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


def _cleanup(run_name_prefix: str) -> None:
    run_uuids = [
        r.uuid for r in db.db.session.query(EvalRun)
        .filter(EvalRun.name.like(f"{run_name_prefix}%"))
        .all()
    ]
    if run_uuids:
        db.db.session.query(EvalResult).filter(
            EvalResult.eval_run_uuid.in_(run_uuids)
        ).delete(synchronize_session=False)
        db.db.session.query(EvalRun).filter(
            EvalRun.uuid.in_(run_uuids)
        ).delete(synchronize_session=False)
    # Also tear down any EvalCase rows tagged with this prefix.
    db.db.session.query(EvalCase).filter(
        EvalCase.name.like(f"{run_name_prefix}%")
    ).delete(synchronize_session=False)
    db.db.session.commit()


def test_create_eval_run_persists_required_fields(app_ctx, fresh_tag):
    try:
        run = db.create_eval_run(
            name=f"{fresh_tag}: persistence",
            agent_role="chat",
            config={"split": "regression"},
        )
        db.db.session.expire_all()
        reloaded = db.get_eval_run(run.uuid)
        assert reloaded is not None
        assert reloaded.name.startswith(fresh_tag)
        assert reloaded.agent_role == "chat"
        assert reloaded.config == {"split": "regression"}
        assert reloaded.started_at is not None
        assert reloaded.finished_at is None
        assert reloaded.summary == {}
    finally:
        _cleanup(fresh_tag)


def test_finish_eval_run_stamps_finished_and_summary(app_ctx, fresh_tag):
    try:
        run = db.create_eval_run(
            name=f"{fresh_tag}: finish",
            agent_role="chat",
        )
        db.finish_eval_run(
            run.uuid,
            summary={"cases": 3, "passed": 2, "mean_score": 0.78},
        )
        db.db.session.expire_all()
        reloaded = db.get_eval_run(run.uuid)
        assert reloaded.finished_at is not None
        assert reloaded.summary == {"cases": 3, "passed": 2, "mean_score": 0.78}
    finally:
        _cleanup(fresh_tag)


def test_create_eval_result_persists_score_and_details(app_ctx, fresh_tag):
    try:
        run = db.create_eval_run(name=f"{fresh_tag}: results", agent_role="chat")
        case = db.create_eval_case(
            name=f"{fresh_tag}: case", case_type="chat_reply",
            split="train", status="active",
        )
        result = db.create_eval_result(
            eval_run_uuid=run.uuid,
            eval_case_uuid=case.uuid,
            score=0.42,
            passed=False,
            details={"must_include": {"matched": 1, "total": 2}},
        )
        db.db.session.expire_all()
        rows = db.list_eval_results_for_run(run.uuid)
        assert len(rows) == 1
        r = rows[0]
        assert r.uuid == result.uuid
        assert r.eval_case_uuid == case.uuid
        assert abs(r.score - 0.42) < 1e-9
        assert r.passed is False
        assert r.details == {"must_include": {"matched": 1, "total": 2}}
    finally:
        _cleanup(fresh_tag)


def test_list_eval_runs_ordered_newest_first(app_ctx, fresh_tag):
    try:
        a = db.create_eval_run(name=f"{fresh_tag}: A", agent_role="chat")
        b = db.create_eval_run(name=f"{fresh_tag}: B", agent_role="chat")
        c = db.create_eval_run(name=f"{fresh_tag}: C", agent_role="chat")
        ours = [
            r for r in db.list_eval_runs()
            if r.name.startswith(fresh_tag)
        ]
        # Newest first => C, B, A.
        assert [r.uuid for r in ours] == [c.uuid, b.uuid, a.uuid]
    finally:
        _cleanup(fresh_tag)


def test_eval_result_cascades_when_run_is_deleted(app_ctx, fresh_tag):
    try:
        run = db.create_eval_run(name=f"{fresh_tag}: cascade", agent_role="chat")
        case = db.create_eval_case(
            name=f"{fresh_tag}: cascade case", case_type="chat_reply",
            split="train", status="active",
        )
        db.create_eval_result(
            eval_run_uuid=run.uuid, eval_case_uuid=case.uuid,
            score=1.0, passed=True, details={},
        )
        # Deleting the run cascades to its results.
        db.db.session.query(EvalRun).filter(EvalRun.uuid == run.uuid).delete()
        db.db.session.commit()
        assert db.list_eval_results_for_run(run.uuid) == []
    finally:
        _cleanup(fresh_tag)
