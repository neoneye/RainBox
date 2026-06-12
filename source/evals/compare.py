"""Compare an `EvalRun` against a baseline `EvalRun` and report
improvements, regressions, and a pass/fail gate decision.

Pure-deterministic: this module reads existing EvalRun + EvalResult
rows and joins them to EvalCase for split metadata. No LLM, no live
chat input, no DB writes.
"""

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import db
from db import EvalCase, EvalResult, EvalRun

logger = logging.getLogger(__name__)

DEFAULT_MAX_MEAN_DROP: float = 0.02
EPSILON: float = 1e-9


@dataclass(frozen=True)
class EvalComparison:
    """Structural diff between a baseline run and a candidate run."""
    baseline_uuid: UUID
    candidate_uuid: UUID
    baseline_mean: float
    candidate_mean: float
    delta_mean: float
    baseline_passed: int
    candidate_passed: int
    delta_passed: int
    new_failures: list[dict[str, Any]]
    improved: list[dict[str, Any]]
    regressed: list[dict[str, Any]]
    common: list[dict[str, Any]] = field(default_factory=list)
    only_in_baseline: list[str] = field(default_factory=list)
    only_in_candidate: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class GateDecision:
    """Pass/fail verdict + structured reasons + readable warnings."""
    passed: bool
    reasons: list[str]
    warnings: list[str]
    comparison: EvalComparison

    def to_json(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "comparison": {
                "baseline_uuid": str(self.comparison.baseline_uuid),
                "candidate_uuid": str(self.comparison.candidate_uuid),
                "baseline_mean": self.comparison.baseline_mean,
                "candidate_mean": self.comparison.candidate_mean,
                "delta_mean": self.comparison.delta_mean,
                "baseline_passed": self.comparison.baseline_passed,
                "candidate_passed": self.comparison.candidate_passed,
                "delta_passed": self.comparison.delta_passed,
                "new_failures": list(self.comparison.new_failures),
                "improved": list(self.comparison.improved),
                "regressed": list(self.comparison.regressed),
                "common": list(self.comparison.common),
                "only_in_baseline": list(self.comparison.only_in_baseline),
                "only_in_candidate": list(self.comparison.only_in_candidate),
            },
        }

    def to_text(self) -> str:
        c = self.comparison
        lines = [
            f"Gate: {'PASS' if self.passed else 'FAIL'}",
            f"Mean score: {c.baseline_mean:.2f} -> {c.candidate_mean:.2f} "
            f"({c.delta_mean:+.2f})",
            f"Pass count: {c.baseline_passed} -> {c.candidate_passed} "
            f"({c.delta_passed:+d})",
        ]
        if self.reasons:
            lines.append("Reasons:")
            for r in self.reasons:
                lines.append(f"- {r}")
        if self.warnings:
            lines.append("Warnings:")
            for w in self.warnings:
                lines.append(f"- {w}")
        if c.new_failures:
            lines.append("New failures:")
            for f in c.new_failures:
                name = f.get("eval_case_name") or f.get("eval_case_uuid")
                reason = f.get("reason") or "below threshold"
                lines.append(f"- {name}: {reason}")
        return "\n".join(lines)


def _load_results(run_uuid: UUID) -> dict[str, EvalResult]:
    rows = (
        db.db.session.query(EvalResult)
        .filter(EvalResult.eval_run_uuid == run_uuid)
        .all()
    )
    return {str(r.eval_case_uuid): r for r in rows}


def _load_case_meta(case_uuids: set[str]) -> dict[str, EvalCase]:
    if not case_uuids:
        return {}
    rows = (
        db.db.session.query(EvalCase)
        .filter(EvalCase.uuid.in_([UUID(u) for u in case_uuids]))
        .all()
    )
    return {str(c.uuid): c for c in rows}


def _failure_reason(details: dict[str, Any]) -> str:
    """Pull a short human-readable reason out of an EvalResult.details
    dict. Mirrors the evals.runner CLI's reason heuristic."""
    reasons: list[str] = []
    for key in ("must_include", "must_not_include",
                "expected_memories", "forbidden_memories"):
        d = (details or {}).get(key) or {}
        if d.get("total", 0) > 0 and (
            d.get("matched", d.get("absent", 0)) < d.get("total", 0)
        ):
            reasons.append(key)
    rj = (details or {}).get("requires_json") or {}
    if rj.get("valid_json") is False:
        reasons.append("requires_json")
    return ", ".join(reasons) or "below threshold"


def compare_eval_runs(
    baseline_uuid: UUID, candidate_uuid: UUID,
) -> EvalComparison:
    """Compute the structural diff between two runs. Joins to EvalCase
    so per-case `split` and `name` are available for the gate."""
    baseline_run = db.get_eval_run(baseline_uuid)
    candidate_run = db.get_eval_run(candidate_uuid)
    if baseline_run is None:
        raise ValueError(f"baseline run not found: {baseline_uuid}")
    if candidate_run is None:
        raise ValueError(f"candidate run not found: {candidate_uuid}")

    base_results = _load_results(baseline_uuid)
    cand_results = _load_results(candidate_uuid)
    all_uuids = set(base_results) | set(cand_results)
    case_meta = _load_case_meta(all_uuids)

    only_in_baseline = sorted(set(base_results) - set(cand_results))
    only_in_candidate = sorted(set(cand_results) - set(base_results))

    common = sorted(set(base_results) & set(cand_results))
    new_failures: list[dict[str, Any]] = []
    improved: list[dict[str, Any]] = []
    regressed: list[dict[str, Any]] = []
    common_entries: list[dict[str, Any]] = []
    for cuuid in common:
        b = base_results[cuuid]
        c = cand_results[cuuid]
        case = case_meta.get(cuuid)
        case_name = case.name if case is not None else cuuid
        case_split = case.split if case is not None else None
        entry = {
            "eval_case_uuid": cuuid,
            "eval_case_name": case_name,
            "split": case_split,
            "baseline_score": b.score,
            "candidate_score": c.score,
            "baseline_passed": b.passed,
            "candidate_passed": c.passed,
        }
        common_entries.append(entry)
        if b.passed and not c.passed:
            new_failures.append({**entry, "reason": _failure_reason(c.details or {})})
        if (not b.passed) and c.passed:
            improved.append(entry)
        if c.score < b.score - EPSILON:
            regressed.append(entry)

    base_mean = (baseline_run.summary or {}).get("mean_score", 0.0)
    cand_mean = (candidate_run.summary or {}).get("mean_score", 0.0)
    base_passed = (baseline_run.summary or {}).get("passed", 0)
    cand_passed = (candidate_run.summary or {}).get("passed", 0)

    return EvalComparison(
        baseline_uuid=baseline_uuid,
        candidate_uuid=candidate_uuid,
        baseline_mean=float(base_mean),
        candidate_mean=float(cand_mean),
        delta_mean=float(cand_mean) - float(base_mean),
        baseline_passed=int(base_passed),
        candidate_passed=int(cand_passed),
        delta_passed=int(cand_passed) - int(base_passed),
        new_failures=new_failures,
        improved=improved,
        regressed=regressed,
        common=common_entries,
        only_in_baseline=only_in_baseline,
        only_in_candidate=only_in_candidate,
    )


def _split_mean(comparison_entries: list[dict[str, Any]], split: str,
                key: str) -> float | None:
    """Mean of `key` across entries with the given split. None if no
    entries match (so the caller can skip the warning)."""
    rows = [e[key] for e in comparison_entries if e.get("split") == split]
    if not rows:
        return None
    return sum(rows) / len(rows)


def _format_missing_baseline_cases_reason(
    only_in_baseline: list[str],
) -> str | None:
    """Format the shared "candidate_missing_baseline_cases" rejection
    reason. Returns None when there are no missing cases (so callers
    can `if reason: reasons.append(reason)`). Used by both
    `gate_candidate_run` and `evals.optimizer._evaluate_candidate` so
    the wording is locked at one source — drift will surface as a
    single-test failure rather than the two sites disagreeing
    silently."""
    if not only_in_baseline:
        return None
    sample = ", ".join(only_in_baseline[:5])
    if len(only_in_baseline) > 5:
        sample = f"{sample}, … (+{len(only_in_baseline) - 5} more)"
    return (
        f"candidate_missing_baseline_cases: candidate omitted "
        f"{len(only_in_baseline)} baseline case(s): {sample}"
    )


def _format_extra_candidate_cases_reason(
    only_in_candidate: list[str],
) -> str | None:
    """Format the shared "candidate_added_unmatched_cases" rejection
    reason. Returns None when there are no candidate-only cases.
    Mirrors `_format_missing_baseline_cases_reason` so wording stays
    locked between gate and optimizer."""
    if not only_in_candidate:
        return None
    sample = ", ".join(only_in_candidate[:5])
    if len(only_in_candidate) > 5:
        sample = f"{sample}, … (+{len(only_in_candidate) - 5} more)"
    return (
        f"candidate_added_unmatched_cases: candidate ran "
        f"{len(only_in_candidate)} case(s) not present in baseline: "
        f"{sample}"
    )


def gate_candidate_run(
    baseline_uuid: UUID, candidate_uuid: UUID,
    *,
    max_mean_drop: float = DEFAULT_MAX_MEAN_DROP,
) -> GateDecision:
    """Apply the default gate rules:
    - FAIL if delta_mean < -max_mean_drop.
    - FAIL if any case in split='regression' went pass -> fail.
    - WARN if train mean improved but holdout mean dropped (both
      subsets non-empty)."""
    comp = compare_eval_runs(baseline_uuid, candidate_uuid)
    reasons: list[str] = []
    warnings: list[str] = []

    if comp.delta_mean < -max_mean_drop - EPSILON:
        reasons.append(
            f"mean score dropped by {-comp.delta_mean:.3f} "
            f"(threshold: {max_mean_drop:.3f})"
        )

    regression_pin_failures = [
        f for f in comp.new_failures if f.get("split") == "regression"
    ]
    if regression_pin_failures:
        names = ", ".join(
            str(f.get("eval_case_name") or f.get("eval_case_uuid"))
            for f in regression_pin_failures
        )
        reasons.append(
            f"regression-split case(s) went pass -> fail: {names}"
        )

    # Finding 1 (WP06): refuse to gate on an unequal case set. A
    # candidate that skips a baseline case shouldn't pass on a higher
    # mean over the surviving cases — that's how you accidentally
    # green-light a run that omits hard / regression / forbidden-memory
    # pins. Default to "equivalent case sets"; an intentional partial
    # mode is a future feature with a named option.
    reason = _format_missing_baseline_cases_reason(comp.only_in_baseline)
    if reason is not None:
        reasons.append(reason)

    reason = _format_extra_candidate_cases_reason(comp.only_in_candidate)
    if reason is not None:
        reasons.append(reason)

    # Use every common-case entry (regardless of classifier bucket) so the
    # per-split mean reflects all shared cases — not just those that flipped
    # pass status or regressed in score.
    all_common_entries = comp.common

    train_base = _split_mean(all_common_entries, "train", "baseline_score")
    train_cand = _split_mean(all_common_entries, "train", "candidate_score")
    holdout_base = _split_mean(all_common_entries, "holdout", "baseline_score")
    holdout_cand = _split_mean(all_common_entries, "holdout", "candidate_score")
    if (train_base is not None and train_cand is not None
        and holdout_base is not None and holdout_cand is not None):
        if train_cand > train_base + EPSILON and holdout_cand < holdout_base - EPSILON:
            warnings.append(
                "train mean improved but holdout mean dropped "
                f"(train {train_base:.2f} -> {train_cand:.2f}, "
                f"holdout {holdout_base:.2f} -> {holdout_cand:.2f})"
            )

    return GateDecision(
        passed=len(reasons) == 0,
        reasons=reasons,
        warnings=warnings,
        comparison=comp,
    )


# --- CLI --------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="evals.compare",
        description="Compare a candidate EvalRun against a baseline and "
                    "decide whether to gate the change.",
    )
    parser.add_argument(
        "--baseline", required=True,
        help="Baseline EvalRun uuid.",
    )
    parser.add_argument(
        "--candidate", required=True,
        help="Candidate EvalRun uuid.",
    )
    parser.add_argument(
        "--max-mean-drop", type=float, default=DEFAULT_MAX_MEAN_DROP,
        help=f"Fail the gate if mean score drops by more than this "
             f"(default: {DEFAULT_MAX_MEAN_DROP}).",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit the full GateDecision as JSON instead of the human "
             "summary. The exit code is unchanged.",
    )
    args = parser.parse_args(argv)

    try:
        baseline_uuid = UUID(args.baseline)
        candidate_uuid = UUID(args.candidate)
    except (ValueError, TypeError):
        parser.error("baseline and candidate must be valid uuids")

    app = db.make_app()
    # NB: we deliberately do NOT call db.init_db(app) here. The CLI is a
    # read-only consumer: the schema is already set up by webapp/main, and
    # init_db's `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` calls need an
    # AccessExclusive lock that conflicts with the AccessShare locks a
    # caller's still-open SQLAlchemy session is holding (e.g. when invoked
    # as a subprocess from a test that has an app_context pushed).
    with app.app_context():
        gate = gate_candidate_run(
            baseline_uuid, candidate_uuid,
            max_mean_drop=args.max_mean_drop,
        )
        if args.json:
            print(json.dumps(gate.to_json(), default=str, indent=2))
        else:
            print(gate.to_text())
    return 0 if gate.passed else 1


if __name__ == "__main__":
    sys.exit(_main())
