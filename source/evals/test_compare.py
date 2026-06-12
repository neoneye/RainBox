"""Tests for evals.compare: compare_eval_runs + gate_candidate_run.

The gate tests use live Postgres (EvalRun + EvalResult rows are real).
Tests clean up via a per-test `name` prefix on EvalRun and EvalCase."""

import subprocess
from uuid import UUID, uuid4

import pytest

import db
from db import EvalCase, EvalResult, EvalRun

from evals.compare import (
    EvalComparison,
    GateDecision,
    compare_eval_runs,
    gate_candidate_run,
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
def fresh_tag() -> str:
    return f"test-{uuid4().hex[:8]}"


def _cleanup(prefix: str) -> None:
    run_uuids = [
        r.uuid for r in db.db.session.query(EvalRun)
        .filter(EvalRun.name.like(f"{prefix}%")).all()
    ]
    if run_uuids:
        db.db.session.query(EvalResult).filter(
            EvalResult.eval_run_uuid.in_(run_uuids)
        ).delete(synchronize_session=False)
        db.db.session.query(EvalRun).filter(
            EvalRun.uuid.in_(run_uuids)
        ).delete(synchronize_session=False)
    db.db.session.query(EvalCase).filter(
        EvalCase.name.like(f"{prefix}%")
    ).delete(synchronize_session=False)
    db.db.session.commit()


def _make_case(prefix: str, label: str, split: str = "train") -> EvalCase:
    return db.create_eval_case(
        name=f"{prefix}: {label}", case_type="chat_reply",
        split=split, status="active",
    )


def _make_run(prefix: str, label: str) -> EvalRun:
    return db.create_eval_run(name=f"{prefix}: {label}", agent_role="chat")


def _stamp(run: EvalRun, *, mean: float, passed: int, total: int) -> None:
    db.finish_eval_run(
        run.uuid, summary={
            "cases": total, "passed": passed,
            "failed": total - passed, "mean_score": mean, "failures": [],
        },
    )


def _result(run, case, *, score, passed):
    return db.create_eval_result(
        eval_run_uuid=run.uuid, eval_case_uuid=case.uuid,
        score=score, passed=passed,
        details={},
    )


def test_candidate_with_same_scores_passes(app_ctx, fresh_tag):
    try:
        case = _make_case(fresh_tag, "stable")
        baseline = _make_run(fresh_tag, "baseline")
        _result(baseline, case, score=0.9, passed=True)
        _stamp(baseline, mean=0.9, passed=1, total=1)

        candidate = _make_run(fresh_tag, "candidate")
        _result(candidate, case, score=0.9, passed=True)
        _stamp(candidate, mean=0.9, passed=1, total=1)

        gate = gate_candidate_run(baseline.uuid, candidate.uuid)
        assert gate.passed is True
        assert gate.reasons == []
    finally:
        _cleanup(fresh_tag)


def test_candidate_with_mean_drop_over_threshold_fails(app_ctx, fresh_tag):
    try:
        case = _make_case(fresh_tag, "drops")
        baseline = _make_run(fresh_tag, "baseline")
        _result(baseline, case, score=1.0, passed=True)
        _stamp(baseline, mean=1.0, passed=1, total=1)

        candidate = _make_run(fresh_tag, "candidate")
        _result(candidate, case, score=0.7, passed=True)
        _stamp(candidate, mean=0.7, passed=1, total=1)

        gate = gate_candidate_run(
            baseline.uuid, candidate.uuid, max_mean_drop=0.02,
        )
        assert gate.passed is False
        assert any("mean" in r.lower() for r in gate.reasons), gate.reasons
    finally:
        _cleanup(fresh_tag)


def test_candidate_with_regression_case_pass_to_fail_fails(app_ctx, fresh_tag):
    try:
        case = _make_case(fresh_tag, "regression-pin", split="regression")
        baseline = _make_run(fresh_tag, "baseline")
        _result(baseline, case, score=1.0, passed=True)
        _stamp(baseline, mean=1.0, passed=1, total=1)

        candidate = _make_run(fresh_tag, "candidate")
        _result(candidate, case, score=0.95, passed=False)
        _stamp(candidate, mean=0.95, passed=0, total=1)

        gate = gate_candidate_run(baseline.uuid, candidate.uuid)
        assert gate.passed is False
        assert any("regression" in r.lower() for r in gate.reasons), gate.reasons
    finally:
        _cleanup(fresh_tag)


def test_candidate_with_train_up_but_holdout_down_warns(app_ctx, fresh_tag):
    try:
        train_case = _make_case(fresh_tag, "train", split="train")
        holdout_case = _make_case(fresh_tag, "holdout", split="holdout")

        baseline = _make_run(fresh_tag, "baseline")
        _result(baseline, train_case, score=0.5, passed=False)
        _result(baseline, holdout_case, score=1.0, passed=True)
        _stamp(baseline, mean=0.75, passed=1, total=2)

        candidate = _make_run(fresh_tag, "candidate")
        _result(candidate, train_case, score=1.0, passed=True)
        _result(candidate, holdout_case, score=0.8, passed=True)
        _stamp(candidate, mean=0.9, passed=2, total=2)

        gate = gate_candidate_run(baseline.uuid, candidate.uuid)
        # Mean went UP (no fail), no regression-split pin → gate passes,
        # but holdout dropped from 1.0 to 0.8 → should warn.
        assert gate.passed is True
        assert any(
            "holdout" in w.lower() for w in gate.warnings
        ), f"expected holdout warning; got {gate.warnings!r}"
    finally:
        _cleanup(fresh_tag)


def test_train_score_up_no_flip_with_holdout_regression_still_warns(
    app_ctx, fresh_tag,
):
    """Regression test for the WP04 final-review bug: when train cases
    improve in score without flipping pass status, the prior implementation
    omitted them from `all_common_entries` and the train-up/holdout-down
    warning silently no-fired. The fix is to use every common case for the
    per-split mean."""
    try:
        train_a = _make_case(fresh_tag, "train-a", split="train")
        train_b = _make_case(fresh_tag, "train-b", split="train")
        holdout_steady = _make_case(fresh_tag, "holdout-steady", split="holdout")
        holdout_drops = _make_case(fresh_tag, "holdout-drops", split="holdout")

        baseline = _make_run(fresh_tag, "baseline")
        # train cases: 0.5/pass each
        _result(baseline, train_a, score=0.5, passed=True)
        _result(baseline, train_b, score=0.5, passed=True)
        # holdout cases: both 1.0/pass
        _result(baseline, holdout_steady, score=1.0, passed=True)
        _result(baseline, holdout_drops, score=1.0, passed=True)
        _stamp(baseline, mean=0.75, passed=4, total=4)

        candidate = _make_run(fresh_tag, "candidate")
        # train cases: improve to 0.9/pass — same pass status, score went up
        _result(candidate, train_a, score=0.9, passed=True)
        _result(candidate, train_b, score=0.9, passed=True)
        # holdout: one steady, one drops to 0.5/pass — still passing but lower
        _result(candidate, holdout_steady, score=1.0, passed=True)
        _result(candidate, holdout_drops, score=0.5, passed=True)
        _stamp(candidate, mean=0.825, passed=4, total=4)

        gate = gate_candidate_run(baseline.uuid, candidate.uuid)
        assert gate.passed is True, gate.reasons
        assert any(
            "holdout" in w.lower() for w in gate.warnings
        ), f"expected holdout warning; got {gate.warnings!r}"
    finally:
        _cleanup(fresh_tag)


def test_compare_lists_new_failures_and_improvements(app_ctx, fresh_tag):
    try:
        improved_case = _make_case(fresh_tag, "improved")
        regressed_case = _make_case(fresh_tag, "regressed")

        baseline = _make_run(fresh_tag, "baseline")
        _result(baseline, improved_case, score=0.0, passed=False)
        _result(baseline, regressed_case, score=1.0, passed=True)
        _stamp(baseline, mean=0.5, passed=1, total=2)

        candidate = _make_run(fresh_tag, "candidate")
        _result(candidate, improved_case, score=1.0, passed=True)
        _result(candidate, regressed_case, score=0.0, passed=False)
        _stamp(candidate, mean=0.5, passed=1, total=2)

        comp = compare_eval_runs(baseline.uuid, candidate.uuid)
        assert isinstance(comp, EvalComparison)
        improved_uuids = {entry["eval_case_uuid"] for entry in comp.improved}
        new_failure_uuids = {entry["eval_case_uuid"] for entry in comp.new_failures}
        assert str(improved_case.uuid) in improved_uuids
        assert str(regressed_case.uuid) in new_failure_uuids
    finally:
        _cleanup(fresh_tag)


# --- CLI ---


def test_cli_exits_0_on_pass(app_ctx, fresh_tag):
    try:
        case = _make_case(fresh_tag, "stable")
        baseline = _make_run(fresh_tag, "baseline")
        _result(baseline, case, score=0.9, passed=True)
        _stamp(baseline, mean=0.9, passed=1, total=1)

        candidate = _make_run(fresh_tag, "candidate")
        _result(candidate, case, score=0.9, passed=True)
        _stamp(candidate, mean=0.9, passed=1, total=1)

        db.db.session.commit()
        result = subprocess.run(
            [
                "venv/bin/python", "-m", "evals.compare",
                "--baseline", str(baseline.uuid),
                "--candidate", str(candidate.uuid),
            ],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert "Gate: PASS" in result.stdout
    finally:
        _cleanup(fresh_tag)


def test_cli_exits_1_on_fail(app_ctx, fresh_tag):
    try:
        case = _make_case(fresh_tag, "drops")
        baseline = _make_run(fresh_tag, "baseline")
        _result(baseline, case, score=1.0, passed=True)
        _stamp(baseline, mean=1.0, passed=1, total=1)

        candidate = _make_run(fresh_tag, "candidate")
        _result(candidate, case, score=0.5, passed=False)
        _stamp(candidate, mean=0.5, passed=0, total=1)

        db.db.session.commit()
        result = subprocess.run(
            [
                "venv/bin/python", "-m", "evals.compare",
                "--baseline", str(baseline.uuid),
                "--candidate", str(candidate.uuid),
            ],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 1, (
            f"expected exit 1, got {result.returncode}; "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert "Gate: FAIL" in result.stdout
    finally:
        _cleanup(fresh_tag)


def test_gate_fails_when_candidate_misses_baseline_cases(
    app_ctx, fresh_tag,
):
    """A candidate that skips a baseline case must NOT pass the gate
    even if it scores high on the cases it did run. Otherwise a
    candidate could silently omit hard / regression / forbidden-memory
    pins and pass on the surviving subset."""
    try:
        case_a = _make_case(fresh_tag, "case-a", split="train")
        case_b = _make_case(fresh_tag, "case-b", split="train")

        baseline = _make_run(fresh_tag, "baseline")
        _result(baseline, case_a, score=0.5, passed=True)
        _result(baseline, case_b, score=0.5, passed=True)
        _stamp(baseline, mean=0.5, passed=2, total=2)

        # Candidate only ran case_a — case_b is missing.
        candidate = _make_run(fresh_tag, "cand-misses-b")
        _result(candidate, case_a, score=1.0, passed=True)
        _stamp(candidate, mean=1.0, passed=1, total=1)

        gate = gate_candidate_run(baseline.uuid, candidate.uuid)
        assert gate.passed is False, gate.reasons
        # Reason must mention the missing case so the operator can
        # identify it.
        joined = " ".join(gate.reasons).lower()
        assert "missing" in joined or "baseline" in joined, gate.reasons
        assert str(case_b.uuid) in " ".join(gate.reasons), gate.reasons
    finally:
        _cleanup(fresh_tag)


def test_gate_fails_on_candidate_only_cases(app_ctx, fresh_tag):
    """A candidate that adds easy extra cases not present in baseline
    must fail the gate — its inflated mean over those cases is not
    proof that the shared case set improved. Mirror of the
    missing-baseline check from WP06/WP07."""
    try:
        shared = _make_case(fresh_tag, "shared", split="train")
        extra = _make_case(fresh_tag, "candidate-only-extra", split="train")

        baseline = _make_run(fresh_tag, "baseline")
        _result(baseline, shared, score=0.5, passed=True)
        _stamp(baseline, mean=0.5, passed=1, total=1)

        candidate = _make_run(fresh_tag, "cand-with-extra")
        _result(candidate, shared, score=0.5, passed=True)
        _result(candidate, extra, score=1.0, passed=True)   # easy extra
        _stamp(candidate, mean=0.75, passed=2, total=2)     # inflated

        gate = gate_candidate_run(baseline.uuid, candidate.uuid)
        assert gate.passed is False, gate.reasons
        joined = " ".join(gate.reasons).lower()
        assert (
            "candidate-only" in joined
            or "unmatched" in joined
            or "extra" in joined
        ), gate.reasons
        assert str(extra.uuid) in " ".join(gate.reasons), gate.reasons
    finally:
        _cleanup(fresh_tag)
