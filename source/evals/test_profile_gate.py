"""Tests for the executable release gate (evals/profile_gate): the recorded
contract — hard-zero exact-source, 2-of-3 with the 90% override rate,
no-regression, family margins — plus the evidence validation each reported
bypass targeted: the complete current seed-manifest inventory (seed_rev and
fingerprint enforced), defensive threshold/shape validation, repetition
counts, run provenance and variant slots, current-membership model proof,
per-repetition provenance, manifest equality, the mandatory combined run,
and invalid state withholding every decision. Synthetic runs are built FROM
the real code-owned manifest so these tests track the shipped inventory."""

from uuid import UUID, uuid4

import pytest

import db
import evals.profile_gate as gate
from evals.profile_guidance import SEED_REV, current_seed_manifest

GROUP = str(uuid4())
MEMBERS = sorted(str(uuid4()) for _ in range(2))

# Family score defaults chosen so every case passes at baseline levels and
# the formatting candidate clears its margins.
BASELINE_SCORES = {"locale": 0.4, "calibration": 0.5, "exact_source": 1.0,
                   "override": 1.0, "injection": 1.0, "counterfactual": 0.8,
                   "regression": 0.8}


class _FakeBinding:
    model_group_uuid = UUID(GROUP)


@pytest.fixture
def app_ctx(monkeypatch):
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    # The gate proves runs used the assistant's CURRENT binding and the
    # group's CURRENT membership; pin both.
    monkeypatch.setattr(gate.db, "get_agent_model_binding",
                        lambda _uuid: _FakeBinding())
    monkeypatch.setattr(gate.db, "get_model_group_member_uuids",
                        lambda _uuid: [UUID(m) for m in MEMBERS])
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


@pytest.fixture
def inventory(app_ctx):
    """Real EvalCase rows for the COMPLETE current code-owned manifest,
    keyed by seed id (EvalResult carries a FK to eval_case)."""
    manifest = current_seed_manifest()
    ids = {}
    for sid in manifest:
        case = db.create_eval_case(
            name=f"pg-gate-test-{sid}-{uuid4().hex[:6]}",
            case_type="chat_reply", status="active")
        ids[sid] = case.uuid
    return manifest, ids


def _rows(manifest, ids, *, scores_by_family=None, overrides=None):
    """{case_uuid: row spec} for every manifest entry, family-default scores
    with per-seed-id overrides ({sid: reps} or {sid: (reps, extra)})."""
    scores_by_family = {**BASELINE_SCORES, **(scores_by_family or {})}
    out = {}
    for sid, entry in manifest.items():
        reps = [scores_by_family[entry["family"]]] * 3
        extra = {}
        if overrides and sid in overrides:
            spec = overrides[sid]
            reps, extra = spec if isinstance(spec, tuple) else (spec, {})
        out[ids[sid]] = {
            "family": extra.get("family", entry["family"]),
            "threshold": extra.get("threshold", entry["threshold"]),
            "fingerprint": extra.get("fingerprint", entry["fingerprint"]),
            "seed_id": extra.get("seed_id", sid),
            "seed_rev": extra.get("seed_rev", SEED_REV),
            "reps": reps,
        }
    return out


def _make_run(tracking_create, rows, *, variant, repetitions=3,
              group=GROUP, members=None, live=True, finish=True,
              agent_role="assistant"):
    run = tracking_create(
        name=f"pg-test {variant}", agent_role=agent_role,
        config={"live": live, "variant": variant, "repetitions": repetitions,
                "model_group_uuid": group,
                "model_member_uuids": members if members is not None else MEMBERS,
                "case_uuids": [str(cu) for cu in rows]})
    for cu, row in rows.items():
        reps = [r if isinstance(r, (dict, str)) else
                {"score": r, "model_uuid": MEMBERS[0],
                 "model_group_uuid": group} for r in row["reps"]]
        # The stored EvalResult.score column has a [0,1] constraint; clamp a
        # sane aggregate even when the test plants malformed rep entries —
        # the gate reads the per-repetition details, not this column.
        numeric = [float(r["score"]) for r in reps
                   if isinstance(r, dict)
                   and isinstance(r.get("score"), (int, float))
                   and 0.0 <= r["score"] <= 1.0]
        db.create_eval_result(
            eval_run_uuid=run.uuid, eval_case_uuid=cu,
            score=sum(numeric) / len(numeric) if numeric else 0.0,
            passed=all(s >= 0.7 for s in numeric),
            details={"threshold": row["threshold"], "family": row["family"],
                     "variant": variant,
                     "case_fingerprint": row["fingerprint"],
                     "seed_id": row["seed_id"], "seed_rev": row["seed_rev"],
                     "repetitions": reps})
    if finish:
        db.finish_eval_run(run.uuid, summary={"variant": variant})
    return run


def _formatting_scores():
    return {"locale": 0.9}


def test_passing_formatting_gate(app_ctx, inventory):
    _, create, _ = app_ctx
    manifest, ids = inventory
    base = _make_run(create, _rows(manifest, ids), variant="baseline")
    cand = _make_run(create,
                     _rows(manifest, ids, scores_by_family=_formatting_scores()),
                     variant="formatting_only")
    report = gate.evaluate_gate(baseline_uuid=base.uuid,
                                formatting_uuid=cand.uuid)
    assert report["valid"] is True, report["problems"]
    verdict = report["decisions"]["formatting"]
    assert verdict["passed"], verdict["reasons"]
    assert verdict["margins"]["locale"] == pytest.approx(0.5)
    assert report["allowed_enablement"] == {
        "formatting_alone": True, "calibration_alone": False, "both": False}
    gate_run = db.get_eval_run(UUID(report["gate_run_uuid"]))
    assert gate_run is not None and gate_run.summary["valid"] is True


def test_margin_hard_zero_override_and_regression_rules(app_ctx, inventory):
    _, create, _ = app_ctx
    manifest, ids = inventory
    base = _make_run(create, _rows(manifest, ids), variant="baseline")
    cand = _make_run(create, _rows(
        manifest, ids,
        scores_by_family={"locale": 0.5},              # +0.10 only
        overrides={"exact_source.code": [1.0, 1.0, 0.0],
                   "override.fahrenheit": [0.0, 0.0, 0.0]},
    ), variant="formatting_only")
    verdict = gate.evaluate_gate(
        baseline_uuid=base.uuid,
        formatting_uuid=cand.uuid)["decisions"]["formatting"]
    assert not verdict["passed"]
    joined = " | ".join(verdict["reasons"])
    assert "below required +0.15" in joined
    assert "hard-zero" in joined
    assert "regression" in joined


def test_stale_seed_rev_or_fingerprint_rejected(app_ctx, inventory):
    """Obsolete weak definitions are not evidence about the code that would
    ship: an older seed_rev, or a fingerprint differing from the current
    code-owned definition, invalidates the run."""
    _, create, _ = app_ctx
    manifest, ids = inventory
    stale_rev = _make_run(create, _rows(
        manifest, ids,
        overrides={"injection.hostile_note":
                   ([1.0, 1.0, 1.0], {"seed_rev": SEED_REV - 1})},
    ), variant="baseline")
    report = gate.evaluate_gate(baseline_uuid=stale_rev.uuid)
    assert report["valid"] is False
    assert any("ran at rev" in p for p in report["problems"])

    stale_fp = _make_run(create, _rows(
        manifest, ids,
        overrides={"injection.hostile_note":
                   ([1.0, 1.0, 1.0], {"fingerprint": "0" * 16})},
    ), variant="baseline")
    report = gate.evaluate_gate(baseline_uuid=stale_fp.uuid)
    assert report["valid"] is False
    assert any("differs from the current code-owned definition" in p
               for p in report["problems"])


def test_missing_required_seed_case_rejected(app_ctx, inventory):
    """One case per family is not coverage: dropping a single required seed
    case (four of five locale cases present, say) invalidates the run."""
    _, create, _ = app_ctx
    manifest, ids = inventory
    rows = _rows(manifest, ids)
    del rows[ids["locale.time_format.de"]]
    base = _make_run(create, rows, variant="baseline")
    report = gate.evaluate_gate(baseline_uuid=base.uuid)
    assert report["valid"] is False
    assert any("required seed case locale.time_format.de is missing" in p
               for p in report["problems"])


def test_invalid_threshold_is_evidence_defect_not_pass(app_ctx, inventory):
    """A negative threshold must not let zero-score exact-source repetitions
    pass — it is INVALID evidence, and malformed shapes never crash."""
    _, create, _ = app_ctx
    manifest, ids = inventory
    base = _make_run(create, _rows(manifest, ids), variant="baseline")
    cand = _make_run(create, _rows(
        manifest, ids,
        scores_by_family=_formatting_scores(),
        overrides={"exact_source.code":
                   ([0.0, 0.0, 0.0], {"threshold": -1})},
    ), variant="formatting_only")
    report = gate.evaluate_gate(baseline_uuid=base.uuid,
                                formatting_uuid=cand.uuid)
    assert report["valid"] is False
    assert any("not a finite number in [0,1]" in p for p in report["problems"])
    assert report["decisions"] == {}


def test_malformed_repetition_shapes_become_problems(app_ctx, inventory):
    _, create, _ = app_ctx
    manifest, ids = inventory
    rows = _rows(manifest, ids)
    rows[ids["locale.date_order.de"]]["reps"] = [
        {"score": "NaN", "model_uuid": MEMBERS[0]},   # string, not a number
        "not-an-object",
        {"score": 2.0, "model_uuid": MEMBERS[0]}]
    base = _make_run(create, rows, variant="baseline")
    report = gate.evaluate_gate(baseline_uuid=base.uuid)   # must not raise
    assert report["valid"] is False
    joined = " | ".join(report["problems"])
    assert "non-object repetition" in joined
    assert "out-of-range repetition score" in joined


def test_wrong_variant_and_provenance_rejected(app_ctx, inventory):
    _, create, _ = app_ctx
    manifest, ids = inventory
    base = _make_run(create, _rows(manifest, ids), variant="baseline")
    cand_rows = _rows(manifest, ids, scores_by_family=_formatting_scores())

    combined = _make_run(create, cand_rows, variant="combined")
    report = gate.evaluate_gate(baseline_uuid=base.uuid,
                                formatting_uuid=combined.uuid)
    assert report["valid"] is False
    assert any("requires 'formatting_only'" in p for p in report["problems"])

    not_live = _make_run(create, cand_rows, variant="formatting_only",
                         live=False)
    report = gate.evaluate_gate(baseline_uuid=base.uuid,
                                formatting_uuid=not_live.uuid)
    assert any("not a live" in p for p in report["problems"])

    unfinished = _make_run(create, cand_rows, variant="formatting_only",
                           finish=False)
    report = gate.evaluate_gate(baseline_uuid=base.uuid,
                                formatting_uuid=unfinished.uuid)
    assert any("not finished" in p for p in report["problems"])


def test_family_relabel_between_runs_rejected(app_ctx, inventory):
    _, create, _ = app_ctx
    manifest, ids = inventory
    base = _make_run(create, _rows(manifest, ids), variant="baseline")
    cand = _make_run(create, _rows(
        manifest, ids,
        scores_by_family=_formatting_scores(),
        overrides={"exact_source.code":
                   ([1.0, 1.0, 0.0], {"family": "regression"})},
    ), variant="formatting_only")
    report = gate.evaluate_gate(baseline_uuid=base.uuid,
                                formatting_uuid=cand.uuid)
    assert report["valid"] is False
    assert any("family changed" in p for p in report["problems"])
    assert report["decisions"] == {}


def test_combined_failure_still_allows_single_blocks(app_ctx, inventory):
    """Per the proposal: both blocks proven safe alone, combined missing a
    margin → ship one alone. The capabilities report says exactly that."""
    _, create, _ = app_ctx
    manifest, ids = inventory
    base = _make_run(create, _rows(manifest, ids), variant="baseline")
    fmt = _make_run(create,
                    _rows(manifest, ids, scores_by_family=_formatting_scores()),
                    variant="formatting_only")
    cal = _make_run(create,
                    _rows(manifest, ids, scores_by_family={"calibration": 0.9}),
                    variant="calibration_only")
    # Combined keeps locale margin but misses calibration.
    com = _make_run(create,
                    _rows(manifest, ids, scores_by_family=_formatting_scores()),
                    variant="combined")
    report = gate.evaluate_gate(baseline_uuid=base.uuid,
                                formatting_uuid=fmt.uuid,
                                calibration_uuid=cal.uuid,
                                combined_uuid=com.uuid)
    assert report["valid"] is True, report["problems"]
    assert report["decisions"]["formatting"]["passed"]
    assert report["decisions"]["calibration"]["passed"]
    assert not report["decisions"]["combined"]["passed"]
    assert report["allowed_enablement"] == {
        "formatting_alone": True, "calibration_alone": True, "both": False}


def test_both_blocks_require_combined_run(app_ctx, inventory):
    _, create, _ = app_ctx
    manifest, ids = inventory
    base = _make_run(create, _rows(manifest, ids), variant="baseline")
    fmt = _make_run(create,
                    _rows(manifest, ids, scores_by_family=_formatting_scores()),
                    variant="formatting_only")
    cal = _make_run(create,
                    _rows(manifest, ids, scores_by_family={"calibration": 0.9}),
                    variant="calibration_only")
    report = gate.evaluate_gate(baseline_uuid=base.uuid,
                                formatting_uuid=fmt.uuid,
                                calibration_uuid=cal.uuid)
    assert report["valid"] is False
    assert any("requires the combined" in p for p in report["problems"])
    assert report["decisions"] == {}

    good = _make_run(create, _rows(
        manifest, ids,
        scores_by_family={**_formatting_scores(), "calibration": 0.9}),
        variant="combined")
    report = gate.evaluate_gate(baseline_uuid=base.uuid,
                                formatting_uuid=fmt.uuid,
                                calibration_uuid=cal.uuid,
                                combined_uuid=good.uuid)
    assert report["valid"] is True, report["problems"]
    assert report["allowed_enablement"]["both"] is True


def test_membership_drift_and_missing_provenance_rejected(app_ctx, inventory):
    _, create, _ = app_ctx
    manifest, ids = inventory
    base = _make_run(create, _rows(manifest, ids), variant="baseline")
    cand_rows = _rows(manifest, ids, scores_by_family=_formatting_scores())

    # A snapshot that no longer matches the group's CURRENT membership.
    drifted = _make_run(create, cand_rows, variant="formatting_only",
                        members=sorted([str(uuid4())]))
    report = gate.evaluate_gate(baseline_uuid=base.uuid,
                                formatting_uuid=drifted.uuid)
    assert report["valid"] is False
    assert any("current membership" in p for p in report["problems"])

    # A scored repetition without model identity.
    rows = _rows(manifest, ids, scores_by_family=_formatting_scores())
    rows[ids["locale.date_order.de"]]["reps"] = [
        {"score": 0.9}, {"score": 0.9, "model_uuid": MEMBERS[0]},
        {"score": 0.9, "model_uuid": MEMBERS[0]}]
    anon = _make_run(create, rows, variant="formatting_only")
    report = gate.evaluate_gate(baseline_uuid=base.uuid,
                                formatting_uuid=anon.uuid)
    assert report["valid"] is False
    assert any("without model provenance" in p for p in report["problems"])


def test_invalid_later_run_withholds_earlier_decisions(app_ctx, inventory):
    _, create, _ = app_ctx
    manifest, ids = inventory
    base = _make_run(create, _rows(manifest, ids), variant="baseline")
    fmt = _make_run(create,
                    _rows(manifest, ids, scores_by_family=_formatting_scores()),
                    variant="formatting_only")
    bad_cal = _make_run(create, _rows(manifest, ids),
                        variant="calibration_only", live=False)
    com = _make_run(create,
                    _rows(manifest, ids, scores_by_family=_formatting_scores()),
                    variant="combined")
    report = gate.evaluate_gate(baseline_uuid=base.uuid,
                                formatting_uuid=fmt.uuid,
                                calibration_uuid=bad_cal.uuid,
                                combined_uuid=com.uuid)
    assert report["valid"] is False
    assert report["decisions"] == {}
    assert report["allowed_enablement"] == {
        "formatting_alone": False, "calibration_alone": False, "both": False}
