"""The executable release gate for the profile-guidance blocks.

Applies the quantitative contract fixed in the proposal's Resolved decisions
over recorded profile-guidance EvalRuns (evals/profile_guidance.py) — the
margins were chosen before any baseline existed so they cannot be chosen
after seeing results:

- A repetition passes when its recorded score meets the case threshold
  (default 0.7). A case passes at 2-of-3 repetitions — except the hard-zero
  `exact_source` family, where EVERY repetition must pass.
- Explicit-override family: every case 2-of-3 AND at least 90% of all
  override repetitions pass.
- No regressions: no case that passed at baseline may fail in a candidate
  variant.
- Improvement margins: locale family mean +0.15 (formatting), calibration
  family mean +0.10 (calibration), computed over identical case uuids and
  repetition counts as the baseline run.
- The blocks gate independently on their own variants; a combined run, when
  supplied, must satisfy hard-zero, the override family, no-regression, and
  BOTH margins before the two switches may be enabled together.
- Run compatibility is a precondition: same model group, same repetition
  count, same case set. Any repetition flagged `invalid` (a violated pair
  invariant) poisons its run: the gate reports INVALID instead of a
  pass/fail — an invalid run must never look like a decision.

The verdict is durable: a `profile-gate` EvalRun row records the inputs and
the full report in its summary. The CLI exits 0 only when every requested
decision passes, 1 on a failed gate, 2 on invalid/incompatible data. Each
passing block is enabled in production by flipping its default-off switch
(`assistant.formatting_guide` / `assistant.knowledge_calibration`).
"""

import argparse
import statistics
import sys
from typing import Any
from uuid import UUID

import db

HARD_ZERO_FAMILY = "exact_source"
OVERRIDE_FAMILY = "override"
OVERRIDE_REPETITION_RATE = 0.90
LOCALE_MARGIN = 0.15
CALIBRATION_MARGIN = 0.10
CASE_PASS_FRACTION = 2 / 3  # 2-of-3 for the default repetition count


def _case_rows(run_uuid: UUID) -> dict[UUID, dict[str, Any]]:
    """Per-case gate-relevant data for one run: family, threshold, and the
    recorded repetition scores/validity."""
    rows: dict[UUID, dict[str, Any]] = {}
    for result in db.list_eval_results_for_run(run_uuid):
        details = result.details or {}
        reps = details.get("repetitions") or []
        rows[result.eval_case_uuid] = {
            "family": details.get("family"),
            "threshold": float(details.get("threshold", 0.7)),
            "scores": [float(r.get("score") or 0.0) for r in reps],
            "invalid": any(r.get("invalid") for r in reps),
        }
    return rows


def _case_passes(row: dict[str, Any]) -> bool:
    scores = row["scores"]
    if not scores:
        return False
    passing = sum(1 for s in scores if s >= row["threshold"])
    if row["family"] == HARD_ZERO_FAMILY:
        return passing == len(scores)
    return passing / len(scores) >= CASE_PASS_FRACTION


def _family_mean(rows: dict[UUID, dict[str, Any]], family: str) -> float | None:
    scores = [s for row in rows.values() if row["family"] == family
              for s in row["scores"]]
    return statistics.fmean(scores) if scores else None


def _compatibility(baseline: "db.EvalRun", candidate: "db.EvalRun",
                   base_rows: dict, cand_rows: dict) -> list[str]:
    problems = []
    b_cfg, c_cfg = baseline.config or {}, candidate.config or {}
    if b_cfg.get("model_group_uuid") != c_cfg.get("model_group_uuid"):
        problems.append("model group differs from baseline")
    if b_cfg.get("repetitions") != c_cfg.get("repetitions"):
        problems.append("repetition count differs from baseline")
    if set(base_rows) != set(cand_rows):
        problems.append("case set differs from baseline")
    return problems


def _judge_variant(
    name: str, baseline_rows: dict, candidate_rows: dict,
    *, margins: dict[str, float],
) -> dict[str, Any]:
    """One variant's verdict against baseline: hard-zero, override family,
    no-regression, and the requested family margins."""
    reasons: list[str] = []

    for cu, row in candidate_rows.items():
        if row["family"] == HARD_ZERO_FAMILY and not _case_passes(row):
            reasons.append(f"hard-zero case {cu} failed a repetition")

    override_rows = [r for r in candidate_rows.values()
                     if r["family"] == OVERRIDE_FAMILY]
    for cu, row in candidate_rows.items():
        if row["family"] == OVERRIDE_FAMILY and not _case_passes(row):
            reasons.append(f"override case {cu} missed 2-of-3")
    override_scores = [(s >= r["threshold"]) for r in override_rows
                       for s in r["scores"]]
    if override_scores:
        rate = sum(override_scores) / len(override_scores)
        if rate < OVERRIDE_REPETITION_RATE:
            reasons.append(
                f"override repetition pass rate {rate:.2f} below "
                f"{OVERRIDE_REPETITION_RATE:.2f}")

    for cu, base_row in baseline_rows.items():
        if _case_passes(base_row) and not _case_passes(candidate_rows[cu]):
            reasons.append(f"regression: case {cu} passed at baseline")

    margin_report = {}
    for family, required in margins.items():
        base_mean = _family_mean(baseline_rows, family)
        cand_mean = _family_mean(candidate_rows, family)
        if base_mean is None or cand_mean is None:
            reasons.append(f"no {family} cases recorded — margin unmeasurable")
            margin_report[family] = None
            continue
        delta = cand_mean - base_mean
        margin_report[family] = round(delta, 4)
        if delta < required:
            reasons.append(
                f"{family} improvement {delta:+.3f} below required "
                f"{required:+.2f}")

    return {"variant": name, "passed": not reasons, "reasons": reasons,
            "margins": margin_report}


def evaluate_gate(
    *,
    baseline_uuid: UUID,
    formatting_uuid: UUID | None = None,
    calibration_uuid: UUID | None = None,
    combined_uuid: UUID | None = None,
) -> dict[str, Any]:
    """Apply the release contract and persist the verdict as a durable
    `profile-gate` EvalRun. Returns the report:
    {"valid": bool, "problems": [...], "decisions": {block: verdict}}.
    `valid: False` means the inputs cannot support ANY decision (missing or
    incompatible runs, invalid repetitions) — never read it as a fail."""
    report: dict[str, Any] = {"valid": True, "problems": [], "decisions": {}}
    runs: dict[str, tuple[UUID | None, dict[str, float]]] = {
        "formatting": (formatting_uuid, {"locale": LOCALE_MARGIN}),
        "calibration": (calibration_uuid, {"calibration": CALIBRATION_MARGIN}),
        "combined": (combined_uuid, {"locale": LOCALE_MARGIN,
                                     "calibration": CALIBRATION_MARGIN}),
    }

    baseline = db.get_eval_run(baseline_uuid)
    baseline_rows: dict[UUID, dict[str, Any]] = {}
    if baseline is None:
        report["valid"] = False
        report["problems"].append(f"baseline run {baseline_uuid} not found")
    else:
        baseline_rows = _case_rows(baseline_uuid)
        if not baseline_rows:
            report["valid"] = False
            report["problems"].append("baseline run has no results")
        invalid = [str(cu) for cu, row in baseline_rows.items()
                   if row["invalid"]]
        if invalid:
            report["valid"] = False
            report["problems"].append(
                f"baseline contains invalid repetitions: {invalid}")

    if report["valid"]:
        assert baseline is not None
        for block, (run_uuid, margins) in runs.items():
            if run_uuid is None:
                continue
            candidate = db.get_eval_run(run_uuid)
            if candidate is None:
                report["valid"] = False
                report["problems"].append(f"{block} run {run_uuid} not found")
                continue
            candidate_rows = _case_rows(run_uuid)
            problems = _compatibility(baseline, candidate,
                                      baseline_rows, candidate_rows)
            invalid = [str(cu) for cu, row in candidate_rows.items()
                       if row["invalid"]]
            if invalid:
                problems.append(
                    f"run contains invalid repetitions: {invalid}")
            if problems:
                report["valid"] = False
                report["problems"].extend(f"{block}: {p}" for p in problems)
                continue
            report["decisions"][block] = _judge_variant(
                block, baseline_rows, candidate_rows, margins=margins)

    gate_run = db.create_eval_run(
        name="profile-gate",
        agent_role="assistant",
        config={"gate": True,
                "baseline": str(baseline_uuid),
                "formatting": str(formatting_uuid) if formatting_uuid else None,
                "calibration": str(calibration_uuid) if calibration_uuid else None,
                "combined": str(combined_uuid) if combined_uuid else None},
    )
    db.finish_eval_run(gate_run.uuid, summary=report)
    report["gate_run_uuid"] = str(gate_run.uuid)
    return report


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="evals.profile_gate",
        description="Apply the profile-guidance release gate over recorded "
                    "live eval runs. Exit 0: every requested decision "
                    "passed; 1: a decision failed; 2: invalid/incompatible "
                    "input runs.",
    )
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--formatting", default=None)
    parser.add_argument("--calibration", default=None)
    parser.add_argument("--combined", default=None)
    args = parser.parse_args(argv)

    def _uuid(raw):
        if raw is None:
            return None
        try:
            return UUID(raw)
        except (ValueError, TypeError):
            parser.error(f"invalid run uuid: {raw}")

    app = db.make_app()
    db.init_db(app)
    with app.app_context():
        report = evaluate_gate(
            baseline_uuid=_uuid(args.baseline),
            formatting_uuid=_uuid(args.formatting),
            calibration_uuid=_uuid(args.calibration),
            combined_uuid=_uuid(args.combined),
        )
    print(f"Gate run {report.get('gate_run_uuid')}")
    if not report["valid"]:
        print("INVALID — no decision possible:")
        for problem in report["problems"]:
            print(f"  - {problem}")
        return 2
    if not report["decisions"]:
        print("INVALID — no candidate runs supplied")
        return 2
    failed = False
    for block, verdict in report["decisions"].items():
        state = "PASS" if verdict["passed"] else "FAIL"
        print(f"{block}: {state}  margins={verdict['margins']}")
        for reason in verdict["reasons"]:
            print(f"  - {reason}")
        failed = failed or not verdict["passed"]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
