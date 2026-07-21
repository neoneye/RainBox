"""Tests for the executable release gate (evals/profile_gate): the recorded
contract — hard-zero exact-source, 2-of-3 with the 90% override rate,
no-regression, family margins — plus the evidence validation each reported
bypass targeted: repetition-count enforcement, run provenance and variant
slots, bound-group and member-snapshot proof, per-case manifest equality,
mandatory-family presence, the mandatory combined run, and invalid state
withholding every decision."""

from uuid import UUID, uuid4

import pytest

import db
import evals.profile_gate as gate

GROUP = str(uuid4())
MEMBERS = sorted(str(uuid4()) for _ in range(2))


class _FakeBinding:
    model_group_uuid = UUID(GROUP)


@pytest.fixture
def app_ctx(monkeypatch):
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    # The gate proves runs used the assistant's CURRENT binding; pin it.
    monkeypatch.setattr(gate.db, "get_agent_model_binding",
                        lambda _uuid: _FakeBinding())
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


def _make_run(tracking_create, cases, *, variant, repetitions=3,
              group=GROUP, members=None, live=True, finish=True,
              agent_role="assistant"):
    """cases: {case_uuid: (family, [rep dicts or scores], manifest_extra)}."""
    run = tracking_create(
        name=f"pg-test {variant}", agent_role=agent_role,
        config={"live": live, "variant": variant, "repetitions": repetitions,
                "model_group_uuid": group,
                "model_member_uuids": members if members is not None else MEMBERS,
                "case_uuids": [str(cu) for cu in cases]})
    for cu, spec in cases.items():
        family, reps = spec[0], spec[1]
        manifest = spec[2] if len(spec) > 2 else {}
        reps = [r if isinstance(r, dict) else
                {"score": r, "model_uuid": MEMBERS[0]} for r in reps]
        scores = [float(r.get("score") or 0.0) for r in reps]
        db.create_eval_result(
            eval_run_uuid=run.uuid, eval_case_uuid=cu,
            score=sum(scores) / len(scores) if scores else 0.0,
            passed=all(s >= 0.7 for s in scores),
            details={"threshold": manifest.get("threshold", 0.7),
                     "family": family, "variant": variant,
                     "case_fingerprint": manifest.get("fingerprint",
                                                      f"fp-{family}"),
                     "seed_id": manifest.get("seed_id", f"sid-{family}"),
                     "repetitions": reps})
    if finish:
        db.finish_eval_run(run.uuid, summary={"variant": variant})
    return run


def _case_set():
    out = {}
    for key in ("locale", "exact", "override", "calibration", "injection"):
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
        ids["injection"]: ("injection", [1.0, 1.0, 1.0]),
    }


def _passing_formatting_cases(ids):
    cases = _baseline_cases(ids)
    cases[ids["locale"]] = ("locale", [0.9, 0.9, 0.9])
    return cases


def test_passing_formatting_gate(app_ctx):
    _, create, _ = app_ctx
    ids = _case_set()
    base = _make_run(create, _baseline_cases(ids), variant="baseline")
    cand = _make_run(create, _passing_formatting_cases(ids),
                     variant="formatting_only")
    report = gate.evaluate_gate(baseline_uuid=base.uuid,
                                formatting_uuid=cand.uuid)
    assert report["valid"] is True, report["problems"]
    verdict = report["decisions"]["formatting"]
    assert verdict["passed"], verdict["reasons"]
    assert verdict["margins"]["locale"] == pytest.approx(0.5)
    assert report["allowed_enablement"] == "formatting"
    gate_run = db.get_eval_run(UUID(report["gate_run_uuid"]))
    assert gate_run is not None and gate_run.summary["valid"] is True


def test_margin_hard_zero_override_and_regression_rules(app_ctx):
    _, create, _ = app_ctx
    ids = _case_set()
    base = _make_run(create, _baseline_cases(ids), variant="baseline")
    cand_cases = _passing_formatting_cases(ids)
    cand_cases[ids["locale"]] = ("locale", [0.5, 0.5, 0.5])   # +0.10 only
    cand_cases[ids["exact"]] = ("exact_source", [1.0, 1.0, 0.0])
    cand_cases[ids["override"]] = ("override", [0.0, 0.0, 0.0])
    cand = _make_run(create, cand_cases, variant="formatting_only")
    verdict = gate.evaluate_gate(
        baseline_uuid=base.uuid,
        formatting_uuid=cand.uuid)["decisions"]["formatting"]
    assert not verdict["passed"]
    joined = " | ".join(verdict["reasons"])
    assert "below required +0.15" in joined
    assert "hard-zero" in joined
    assert "regression" in joined


def test_single_repetition_cannot_satisfy_two_of_three(app_ctx):
    """One recorded passing repetition must not read as 100% — the gate
    requires exactly three repetitions per case."""
    _, create, _ = app_ctx
    ids = _case_set()
    base = _make_run(create, _baseline_cases(ids), variant="baseline")
    cand_cases = _passing_formatting_cases(ids)
    cand_cases[ids["override"]] = ("override", [1.0])          # one rep only
    cand = _make_run(create, cand_cases, variant="formatting_only")
    report = gate.evaluate_gate(baseline_uuid=base.uuid,
                                formatting_uuid=cand.uuid)
    assert report["valid"] is False
    assert any("requires exactly 3" in p for p in report["problems"])
    assert report["decisions"] == {}


def test_wrong_variant_and_provenance_rejected(app_ctx):
    _, create, _ = app_ctx
    ids = _case_set()
    base = _make_run(create, _baseline_cases(ids), variant="baseline")

    combined = _make_run(create, _passing_formatting_cases(ids),
                         variant="combined")
    report = gate.evaluate_gate(baseline_uuid=base.uuid,
                                formatting_uuid=combined.uuid)
    assert report["valid"] is False
    assert any("requires 'formatting_only'" in p for p in report["problems"])

    not_live = _make_run(create, _passing_formatting_cases(ids),
                         variant="formatting_only", live=False)
    report = gate.evaluate_gate(baseline_uuid=base.uuid,
                                formatting_uuid=not_live.uuid)
    assert any("not a live" in p for p in report["problems"])

    unfinished = _make_run(create, _passing_formatting_cases(ids),
                           variant="formatting_only", finish=False)
    report = gate.evaluate_gate(baseline_uuid=base.uuid,
                                formatting_uuid=unfinished.uuid)
    assert any("not finished" in p for p in report["problems"])


def test_family_relabel_and_threshold_change_rejected(app_ctx):
    """A case cannot escape hard-zero by relabeling itself, nor soften its
    threshold, between baseline and candidate — the per-case manifest must
    be identical."""
    _, create, _ = app_ctx
    ids = _case_set()
    base = _make_run(create, _baseline_cases(ids), variant="baseline")
    cand_cases = _passing_formatting_cases(ids)
    # The exact-source case corrupts one repetition AND relabels itself.
    cand_cases[ids["exact"]] = ("regression", [1.0, 1.0, 0.0],
                                {"fingerprint": "fp-exact_source",
                                 "seed_id": "sid-exact_source"})
    cand = _make_run(create, cand_cases, variant="formatting_only")
    report = gate.evaluate_gate(baseline_uuid=base.uuid,
                                formatting_uuid=cand.uuid)
    assert report["valid"] is False
    assert any("family changed" in p for p in report["problems"])
    assert report["decisions"] == {}

    cand_cases = _passing_formatting_cases(ids)
    cand_cases[ids["exact"]] = ("exact_source", [0.95, 0.95, 0.95],
                                {"threshold": 0.5})
    cand2 = _make_run(create, cand_cases, variant="formatting_only")
    report = gate.evaluate_gate(baseline_uuid=base.uuid,
                                formatting_uuid=cand2.uuid)
    assert any("threshold changed" in p for p in report["problems"])


def test_missing_mandatory_family_rejected(app_ctx):
    """A run whose case set lacks a mandatory family (no exact-source, no
    override, …) is broken evidence, not a clean pass."""
    _, create, _ = app_ctx
    ids = _case_set()
    slim = {ids["locale"]: ("locale", [0.4, 0.4, 0.4])}
    base = _make_run(create, slim, variant="baseline")
    cand = _make_run(create,
                     {ids["locale"]: ("locale", [0.9, 0.9, 0.9])},
                     variant="formatting_only")
    report = gate.evaluate_gate(baseline_uuid=base.uuid,
                                formatting_uuid=cand.uuid)
    assert report["valid"] is False
    assert any("mandatory families absent" in p for p in report["problems"])


def test_both_blocks_require_combined_run(app_ctx):
    _, create, _ = app_ctx
    ids = _case_set()
    base = _make_run(create, _baseline_cases(ids), variant="baseline")
    fmt = _make_run(create, _passing_formatting_cases(ids),
                    variant="formatting_only")
    cal_cases = _baseline_cases(ids)
    cal_cases[ids["calibration"]] = ("calibration", [0.9, 0.9, 0.9])
    cal = _make_run(create, cal_cases, variant="calibration_only")
    report = gate.evaluate_gate(baseline_uuid=base.uuid,
                                formatting_uuid=fmt.uuid,
                                calibration_uuid=cal.uuid)
    assert report["valid"] is False
    assert any("requires the combined" in p for p in report["problems"])
    assert report["decisions"] == {}

    # With the combined run supplied and passing, both may enable.
    combined_cases = _passing_formatting_cases(ids)
    combined_cases[ids["calibration"]] = ("calibration", [0.9, 0.9, 0.9])
    com = _make_run(create, combined_cases, variant="combined")
    report = gate.evaluate_gate(baseline_uuid=base.uuid,
                                formatting_uuid=fmt.uuid,
                                calibration_uuid=cal.uuid,
                                combined_uuid=com.uuid)
    assert report["valid"] is True, report["problems"]
    assert report["allowed_enablement"] == "both"


def test_invalid_later_run_withholds_earlier_decisions(app_ctx):
    """A verdict computed before a later run turned out broken must not
    survive: global invalidity clears every decision."""
    _, create, _ = app_ctx
    ids = _case_set()
    base = _make_run(create, _baseline_cases(ids), variant="baseline")
    fmt = _make_run(create, _passing_formatting_cases(ids),
                    variant="formatting_only")
    bad_cal = _make_run(create, _baseline_cases(ids),
                        variant="calibration_only", live=False)
    com = _make_run(create, _passing_formatting_cases(ids),
                    variant="combined")
    report = gate.evaluate_gate(baseline_uuid=base.uuid,
                                formatting_uuid=fmt.uuid,
                                calibration_uuid=bad_cal.uuid,
                                combined_uuid=com.uuid)
    assert report["valid"] is False
    assert report["decisions"] == {}               # formatting NOT retained
    assert report["allowed_enablement"] == "none"


def test_wrong_model_group_or_snapshot_rejected(app_ctx):
    _, create, _ = app_ctx
    ids = _case_set()
    base = _make_run(create, _baseline_cases(ids), variant="baseline")

    other_group = _make_run(create, _passing_formatting_cases(ids),
                            variant="formatting_only", group=str(uuid4()))
    report = gate.evaluate_gate(baseline_uuid=base.uuid,
                                formatting_uuid=other_group.uuid)
    assert any("currently bound group" in p for p in report["problems"])

    other_members = _make_run(create, _passing_formatting_cases(ids),
                              variant="formatting_only",
                              members=sorted([str(uuid4())]))
    report = gate.evaluate_gate(baseline_uuid=base.uuid,
                                formatting_uuid=other_members.uuid)
    assert report["valid"] is False
    # A stray recorded model (outside its own snapshot) or a snapshot that
    # differs from baseline both invalidate.
    joined = " | ".join(report["problems"])
    assert "snapshot" in joined


def test_invalid_repetition_poisons_the_run(app_ctx):
    _, create, _ = app_ctx
    ids = _case_set()
    base = _make_run(create, _baseline_cases(ids), variant="baseline")
    cand_cases = _passing_formatting_cases(ids)
    cand_cases[ids["calibration"]] = (
        "calibration",
        [{"score": 0.0, "invalid": True, "model_uuid": MEMBERS[0]},
         {"score": 0.0, "invalid": True, "model_uuid": MEMBERS[0]},
         {"score": 0.0, "invalid": True, "model_uuid": MEMBERS[0]}])
    cand = _make_run(create, cand_cases, variant="formatting_only")
    report = gate.evaluate_gate(baseline_uuid=base.uuid,
                                formatting_uuid=cand.uuid)
    assert report["valid"] is False
    assert any("invalid repetitions" in p for p in report["problems"])
    assert report["decisions"] == {}
