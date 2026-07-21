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
- The blocks gate independently on their own variants; enabling BOTH
  requires all three candidate variants — a combined run is mandatory
  whenever both individual candidates are judged.

The gate trusts nothing it can verify instead. Every run must be a finished
live profile-guidance run of the exact expected variant, produced against
the assistant's CURRENTLY bound model group with an identical member
snapshot, carrying exactly the configured repetition count (three) of finite
in-range scores per case, no duplicate results, and a per-case manifest
(definition fingerprint, family, threshold, seed id) identical between
baseline and candidate — so a case cannot be redefined, relabeled out of
hard-zero, or re-thresholded between runs while keeping its uuid. The
mandatory families must all be present. Any violation, or any repetition
flagged `invalid`, makes the verdict INVALID with every decision withheld —
an invalid run must never look like a decision.

The verdict is durable: a `profile-gate` EvalRun row records the inputs and
the full report (including `allowed_enablement`: none / formatting /
calibration / both) in its summary. The CLI exits 0 only when every
requested decision passes, 1 on a failed gate, 2 on invalid/incompatible
data. Each passing block is enabled in production by flipping its
default-off switch (`assistant.formatting_guide` /
`assistant.knowledge_calibration`).
"""

import argparse
import math
import statistics
import sys
from typing import Any
from uuid import UUID

import db
from agents.config import ASSISTANT_UUID

HARD_ZERO_FAMILY = "exact_source"
OVERRIDE_FAMILY = "override"
OVERRIDE_REPETITION_RATE = 0.90
LOCALE_MARGIN = 0.15
CALIBRATION_MARGIN = 0.10
CASE_PASS_FRACTION = 2 / 3
REQUIRED_REPETITIONS = 3
# Families that must be present in the evidence: their complete absence is
# not "nothing to check" — it is a broken case set.
MANDATORY_FAMILIES = frozenset(
    {"locale", "calibration", "exact_source", "override", "injection"})

_EXPECTED_VARIANT = {"baseline": "baseline", "formatting": "formatting_only",
                     "calibration": "calibration_only", "combined": "combined"}


def _case_rows(run_uuid: UUID) -> tuple[dict[UUID, dict[str, Any]], list[str]]:
    """Per-case gate-relevant data for one run plus its integrity problems:
    duplicate results, wrong repetition counts, and out-of-range scores are
    evidence defects, not model failures."""
    rows: dict[UUID, dict[str, Any]] = {}
    problems: list[str] = []
    for result in db.list_eval_results_for_run(run_uuid):
        if result.eval_case_uuid in rows:
            problems.append(
                f"duplicate result rows for case {result.eval_case_uuid}")
            continue
        details = result.details or {}
        reps = details.get("repetitions") or []
        scores = []
        for rep in reps:
            score = rep.get("score")
            if (not isinstance(score, (int, float)) or isinstance(score, bool)
                    or not math.isfinite(score) or not 0.0 <= score <= 1.0):
                problems.append(
                    f"case {result.eval_case_uuid} has a non-finite or "
                    f"out-of-range repetition score: {score!r}")
                score = 0.0
            scores.append(float(score))
        if len(scores) != REQUIRED_REPETITIONS:
            problems.append(
                f"case {result.eval_case_uuid} recorded {len(scores)} "
                f"repetition(s); the gate requires exactly "
                f"{REQUIRED_REPETITIONS}")
        rows[result.eval_case_uuid] = {
            "family": details.get("family"),
            "threshold": float(details.get("threshold", 0.7)),
            "fingerprint": details.get("case_fingerprint"),
            "seed_id": details.get("seed_id"),
            "scores": scores,
            "invalid": any(r.get("invalid") for r in reps),
        }
    return rows, problems


def _case_passes(row: dict[str, Any]) -> bool:
    scores = row["scores"]
    if len(scores) != REQUIRED_REPETITIONS:
        return False
    passing = sum(1 for s in scores if s >= row["threshold"])
    if row["family"] == HARD_ZERO_FAMILY:
        return passing == len(scores)
    return passing / len(scores) >= CASE_PASS_FRACTION


def _family_mean(rows: dict[UUID, dict[str, Any]], family: str) -> float | None:
    scores = [s for row in rows.values() if row["family"] == family
              for s in row["scores"]]
    return statistics.fmean(scores) if scores else None


def _validate_run(
    run: "db.EvalRun", rows: dict[UUID, dict[str, Any]],
    *, slot: str, bound_group: str | None,
) -> list[str]:
    """Provenance and integrity checks one run must clear before its numbers
    mean anything."""
    problems: list[str] = []
    cfg = run.config or {}
    if not cfg.get("live"):
        problems.append("not a live profile-guidance run")
    if run.finished_at is None:
        problems.append("run is not finished")
    if run.agent_role != "assistant":
        problems.append(f"agent_role is {run.agent_role!r}, not 'assistant'")
    expected_variant = _EXPECTED_VARIANT[slot]
    if cfg.get("variant") != expected_variant:
        problems.append(
            f"variant is {cfg.get('variant')!r}; the {slot} slot requires "
            f"{expected_variant!r}")
    if cfg.get("repetitions") != REQUIRED_REPETITIONS:
        problems.append(
            f"configured repetitions {cfg.get('repetitions')!r} != "
            f"{REQUIRED_REPETITIONS}")
    if not rows:
        problems.append("run has no results")
    if bound_group is None:
        problems.append("the assistant has no bound model group to gate "
                        "against")
    elif cfg.get("model_group_uuid") != bound_group:
        problems.append(
            f"run model group {cfg.get('model_group_uuid')} is not the "
            f"assistant's currently bound group {bound_group}")
    member_snapshot = set(cfg.get("model_member_uuids") or [])
    recorded_models = {
        rep.get("model_uuid")
        for result in db.list_eval_results_for_run(run.uuid)
        for rep in (result.details or {}).get("repetitions") or []
        if rep.get("model_uuid")}
    strays = recorded_models - member_snapshot
    if strays:
        problems.append(
            f"repetitions used models outside the run's member snapshot: "
            f"{sorted(strays)}")
    invalid = [str(cu) for cu, row in rows.items() if row["invalid"]]
    if invalid:
        problems.append(f"run contains invalid repetitions: {invalid}")
    return problems


def _manifest_problems(base_rows: dict, cand_rows: dict) -> list[str]:
    """The per-case manifest must be identical between baseline and
    candidate: same definition fingerprint, family, and threshold — a case
    that mutated between runs is not the same evidence."""
    problems = []
    if set(base_rows) != set(cand_rows):
        problems.append("case set differs from baseline")
        return problems
    for cu, base in base_rows.items():
        cand = cand_rows[cu]
        for key in ("fingerprint", "family", "threshold", "seed_id"):
            if base.get(key) != cand.get(key):
                problems.append(
                    f"case {cu} {key} changed between baseline "
                    f"({base.get(key)!r}) and candidate ({cand.get(key)!r})")
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
    {"valid": bool, "problems": [...], "decisions": {block: verdict},
    "allowed_enablement": "none"|"formatting"|"calibration"|"both"}.
    `valid: False` means the inputs cannot support ANY decision — every
    decision is withheld; never read it as a fail."""
    report: dict[str, Any] = {"valid": True, "problems": [], "decisions": {},
                              "allowed_enablement": "none"}
    binding = db.get_agent_model_binding(ASSISTANT_UUID)
    bound_group = (str(binding.model_group_uuid)
                   if binding is not None and binding.model_group_uuid
                   else None)
    slots: dict[str, tuple[UUID | None, dict[str, float]]] = {
        "formatting": (formatting_uuid, {"locale": LOCALE_MARGIN}),
        "calibration": (calibration_uuid, {"calibration": CALIBRATION_MARGIN}),
        "combined": (combined_uuid, {"locale": LOCALE_MARGIN,
                                     "calibration": CALIBRATION_MARGIN}),
    }
    if formatting_uuid and calibration_uuid and not combined_uuid:
        report["valid"] = False
        report["problems"].append(
            "both individual candidates supplied without a combined run — "
            "enabling both blocks requires the combined interaction variant")

    baseline = db.get_eval_run(baseline_uuid)
    baseline_rows: dict[UUID, dict[str, Any]] = {}
    if baseline is None:
        report["valid"] = False
        report["problems"].append(f"baseline run {baseline_uuid} not found")
    else:
        baseline_rows, row_problems = _case_rows(baseline_uuid)
        problems = row_problems + _validate_run(
            baseline, baseline_rows, slot="baseline", bound_group=bound_group)
        families = {row["family"] for row in baseline_rows.values()}
        missing = MANDATORY_FAMILIES - families
        if missing:
            problems.append(
                f"mandatory families absent from the case set: "
                f"{sorted(missing)}")
        if problems:
            report["valid"] = False
            report["problems"].extend(f"baseline: {p}" for p in problems)

    if report["valid"]:
        assert baseline is not None
        for block, (run_uuid, margins) in slots.items():
            if run_uuid is None:
                continue
            candidate = db.get_eval_run(run_uuid)
            if candidate is None:
                report["valid"] = False
                report["problems"].append(f"{block} run {run_uuid} not found")
                continue
            candidate_rows, row_problems = _case_rows(run_uuid)
            problems = row_problems + _validate_run(
                candidate, candidate_rows, slot=block,
                bound_group=bound_group)
            problems += _manifest_problems(baseline_rows, candidate_rows)
            base_snapshot = (baseline.config or {}).get("model_member_uuids")
            cand_snapshot = (candidate.config or {}).get("model_member_uuids")
            if base_snapshot != cand_snapshot:
                problems.append("model member snapshot differs from baseline")
            if problems:
                report["valid"] = False
                report["problems"].extend(f"{block}: {p}" for p in problems)
                continue
            report["decisions"][block] = _judge_variant(
                block, baseline_rows, candidate_rows, margins=margins)

    if not report["valid"]:
        # Invalid input supports NO decision — a verdict computed before a
        # later run turned out broken must not survive into the report.
        report["decisions"] = {}
    else:
        decisions = report["decisions"]
        fmt_ok = decisions.get("formatting", {}).get("passed", False)
        cal_ok = decisions.get("calibration", {}).get("passed", False)
        com_ok = decisions.get("combined", {}).get("passed", False)
        if fmt_ok and cal_ok and com_ok:
            report["allowed_enablement"] = "both"
        elif fmt_ok and not cal_ok:
            report["allowed_enablement"] = "formatting"
        elif cal_ok and not fmt_ok:
            report["allowed_enablement"] = "calibration"

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
    print(f"allowed enablement: {report['allowed_enablement']}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
