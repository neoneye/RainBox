"""Tests for the EvalRun.is_baseline column + set_baseline_eval_run helper."""

from uuid import uuid4

import pytest

import db
from db import EvalRun


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


def _cleanup(prefix: str) -> None:
    db.db.session.query(EvalRun).filter(
        EvalRun.name.like(f"{prefix}%")
    ).delete(synchronize_session=False)
    db.db.session.commit()


def test_new_eval_run_is_not_baseline_by_default(app_ctx, fresh_tag):
    try:
        run = db.create_eval_run(
            name=f"{fresh_tag}: default", agent_role="chat",
        )
        db.db.session.expire_all()
        reloaded = db.get_eval_run(run.uuid)
        assert reloaded.is_baseline is False
    finally:
        _cleanup(fresh_tag)


def test_set_baseline_eval_run_flips_the_flag(app_ctx, fresh_tag):
    try:
        run = db.create_eval_run(
            name=f"{fresh_tag}: to-be-baseline", agent_role="chat",
        )
        db.set_baseline_eval_run(run.uuid, is_baseline=True)
        db.db.session.expire_all()
        reloaded = db.get_eval_run(run.uuid)
        assert reloaded.is_baseline is True
    finally:
        _cleanup(fresh_tag)


def test_set_baseline_eval_run_can_unset(app_ctx, fresh_tag):
    try:
        run = db.create_eval_run(
            name=f"{fresh_tag}: toggle", agent_role="chat",
        )
        db.set_baseline_eval_run(run.uuid, is_baseline=True)
        db.set_baseline_eval_run(run.uuid, is_baseline=False)
        db.db.session.expire_all()
        reloaded = db.get_eval_run(run.uuid)
        assert reloaded.is_baseline is False
    finally:
        _cleanup(fresh_tag)


def test_set_baseline_eval_run_missing_uuid_raises(app_ctx):
    nonexistent = uuid4()
    with pytest.raises(ValueError) as excinfo:
        db.set_baseline_eval_run(nonexistent, is_baseline=True)
    assert str(nonexistent) in str(excinfo.value)
