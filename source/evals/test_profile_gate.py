"""Tests for the executable release gate (evals/profile_gate): the recorded
contract — hard-zero exact-source, 2-of-3 with the 90% override rate,
no-regression, family margins, run compatibility, and invalid-run refusal —
applied over synthetic recorded runs, plus the durable verdict row."""

from uuid import uuid4

import pytest

import db
import evals.profile_gate as gate


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    created_runs = []
    orig_create = db.create_eval_run

    def tracking_create(**kwargs):
        run = orig_create(**kwargs)
        created_runs.append(run.uuid)
        return run

    try:
        yield app, tracking_create, created_runs
    finally:
        db.db.session.rollback()
        for run_uuid in created_runs:
            run = db.get_eval_run(run_uuid)
            if run is not None:
                db.db.session.delete(run)
        for run in db.db.session.query(db.EvalRun).filter(
                db.EvalRun.name == "profile-gate").all():
            db.db.session.delete(run)
        for case in db.db.session.query(db.EvalCase).filter(
                db.EvalCase.name.like("pg-gate-test-%")).all():
            db.db.session.delete(case)
        db.db.session.commit()
        ctx.pop()


GROUP = str(uuid4())


def _make_run(tracking_create, cases, *, variant, repetitions=3):
    """cases: {case_uuid: (family, [rep dicts or scores])}."""
    run = tracking_create(
        name=f"pg-test {variant}", agent_role="assistant",
        config={"live": True, "variant": variant, "repetitions": repetitions,
                "model_group_uuid": GROUP,
                "case_uuids": [str(cu) for cu in cases]})
    for cu, (family, reps) in cases.items():
        reps = [r if isinstance(r, dict) else {"score": r} for r in reps]
        scores = [float(r.get("score") or 0.0) for r in reps]
        db.create_eval_result(
            eval_run_uuid=run.uuid, eval_case_uuid=cu,
            score=sum(scores) / len(scores) if scores else 0.0,
            passed=all(s >= 0.7 for s in scores),
            details={"threshold": 0.7, "family": family, "variant": variant,
                     "repetitions": reps})
    return run


def _case_set():
    """Real throwaway EvalCase rows (EvalResult carries a FK to them),
    removed by the fixture teardown's name sweep."""
    out = {}
    for key in ("locale", "exact", "override", "calibration"):
        case = db.create_eval_case(
            name=f"pg-gate-test-{key}-{uuid4().hex[:8]}",
            case_type="chat_reply", status="active")
        out[key] = case.uuid
    return out


def _baseline_cases(ids):
    return {
        ids["locale"]: ("locale", [0.4, 0.4, 0.4]),
        ids["exact"]: ("exact_source", [1.0, 1.0, 1.0]),
        ids["override"]: ("override", [1.0, 1.0, 1.0]),
        ids["calibration"]: ("calibration", [0.5, 0.5, 0.5]),
    }


def test_passing_formatting_gate(app_ctx):
    _, create, _ = app_ctx
    ids = _case_set()
    base = _make_run(create, _baseline_cases(ids), variant="baseline")
    cand = _make_run(create, {
        ids["locale"]: ("locale", [0.9, 0.9, 0.9]),      # +0.50 ≥ +0.15
        ids["exact"]: ("exact_source", [1.0, 1.0, 1.0]),
        ids["override"]: ("override", [1.0, 1.0, 0.9]),
        ids["calibration"]: ("calibration", [0.5, 0.5, 0.5]),
    }, variant="formatting_only")
    report = gate.evaluate_gate(baseline_uuid=base.uuid,
                                formatting_uuid=cand.uuid)
    assert report["valid"] is True
    verdict = report["decisions"]["formatting"]
    assert verdict["passed"], verdict["reasons"]
    assert verdict["margins"]["locale"] == pytest.approx(0.5)
    # The verdict is durable: a profile-gate run row carries the report.
    gate_run = db.get_eval_run(__import__("uuid").UUID(report["gate_run_uuid"]))
    assert gate_run is not None and gate_run.summary["valid"] is True


def test_margin_miss_fails(app_ctx):
    _, create, _ = app_ctx
    ids = _case_set()
    base = _make_run(create, _baseline_cases(ids), variant="baseline")
    cand_cases = _baseline_cases(ids)
    cand_cases[ids["locale"]] = ("locale", [0.5, 0.5, 0.5])   # only +0.10
    cand = _make_run(create, cand_cases, variant="formatting_only")
    report = gate.evaluate_gate(baseline_uuid=base.uuid,
                                formatting_uuid=cand.uuid)
    verdict = report["decisions"]["formatting"]
    assert not verdict["passed"]
    assert any("below required +0.15" in r for r in verdict["reasons"])


def test_hard_zero_family_tolerates_no_failed_repetition(app_ctx):
    _, create, _ = app_ctx
    ids = _case_set()
    base = _make_run(create, _baseline_cases(ids), variant="baseline")
    cand_cases = _baseline_cases(ids)
    cand_cases[ids["locale"]] = ("locale", [0.9, 0.9, 0.9])
    cand_cases[ids["exact"]] = ("exact_source", [1.0, 1.0, 0.0])  # 2-of-3 NOT enough
    cand = _make_run(create, cand_cases, variant="formatting_only")
    report = gate.evaluate_gate(baseline_uuid=base.uuid,
                                formatting_uuid=cand.uuid)
    verdict = report["decisions"]["formatting"]
    assert not verdict["passed"]
    assert any("hard-zero" in r for r in verdict["reasons"])


def test_override_repetition_rate_enforced(app_ctx):
    _, create, _ = app_ctx
    ids = _case_set()
    ids["override2"] = db.create_eval_case(
        name=f"pg-gate-test-override2-{uuid4().hex[:8]}",
        case_type="chat_reply", status="active").uuid
    base_cases = _baseline_cases(ids)
    base_cases[ids["override2"]] = ("override", [0.0, 0.0, 0.0])
    base = _make_run(create, base_cases, variant="baseline")
    cand_cases = dict(base_cases)
    cand_cases[ids["locale"]] = ("locale", [0.9, 0.9, 0.9])
    # Both cases pass 2-of-3 individually, but 4 of 6 override repetitions
    # (67%) is below the 90% overall rate.
    cand_cases[ids["override"]] = ("override", [1.0, 1.0, 0.0])
    cand_cases[ids["override2"]] = ("override", [1.0, 1.0, 0.0])
    cand = _make_run(create, cand_cases, variant="formatting_only")
    report = gate.evaluate_gate(baseline_uuid=base.uuid,
                                formatting_uuid=cand.uuid)
    verdict = report["decisions"]["formatting"]
    assert not verdict["passed"]
    assert any("repetition pass rate" in r for r in verdict["reasons"])


def test_regression_blocks(app_ctx):
    _, create, _ = app_ctx
    ids = _case_set()
    base = _make_run(create, _baseline_cases(ids), variant="baseline")
    cand_cases = _baseline_cases(ids)
    cand_cases[ids["locale"]] = ("locale", [0.9, 0.9, 0.9])
    cand_cases[ids["override"]] = ("override", [0.0, 0.0, 0.0])  # was passing
    cand = _make_run(create, cand_cases, variant="formatting_only")
    report = gate.evaluate_gate(baseline_uuid=base.uuid,
                                formatting_uuid=cand.uuid)
    verdict = report["decisions"]["formatting"]
    assert not verdict["passed"]
    assert any("regression" in r for r in verdict["reasons"])


def test_combined_requires_both_margins(app_ctx):
    _, create, _ = app_ctx
    ids = _case_set()
    base = _make_run(create, _baseline_cases(ids), variant="baseline")
    cand_cases = _baseline_cases(ids)
    cand_cases[ids["locale"]] = ("locale", [0.9, 0.9, 0.9])   # locale margin ok
    # calibration unchanged → +0.00 < +0.10
    cand = _make_run(create, cand_cases, variant="combined")
    report = gate.evaluate_gate(baseline_uuid=base.uuid,
                                combined_uuid=cand.uuid)
    verdict = report["decisions"]["combined"]
    assert not verdict["passed"]
    assert any("calibration improvement" in r for r in verdict["reasons"])


def test_invalid_and_incompatible_runs_refuse_decision(app_ctx):
    _, create, _ = app_ctx
    ids = _case_set()
    base = _make_run(create, _baseline_cases(ids), variant="baseline")

    # An invalid repetition (violated pair invariant) poisons the run.
    cand_cases = _baseline_cases(ids)
    cand_cases[ids["calibration"]] = (
        "calibration", [{"score": 0.0, "invalid": True}])
    cand = _make_run(create, cand_cases, variant="calibration_only")
    report = gate.evaluate_gate(baseline_uuid=base.uuid,
                                calibration_uuid=cand.uuid)
    assert report["valid"] is False
    assert report["decisions"] == {}                # never a fail — INVALID
    assert any("invalid repetitions" in p for p in report["problems"])

    # A differing case set is incompatible.
    other_ids = _case_set()
    cand2 = _make_run(create, _baseline_cases(other_ids),
                      variant="formatting_only")
    report = gate.evaluate_gate(baseline_uuid=base.uuid,
                                formatting_uuid=cand2.uuid)
    assert report["valid"] is False
    assert any("case set differs" in p for p in report["problems"])