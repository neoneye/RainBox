"""Bounded candidate-configuration optimizer for the eval loop.

This module proves the loop without yet wiring candidate configs into agent
behavior. `generate_candidate_configs` returns a small, deterministic matrix
of variants; `select_best_candidate` (added in WP05 Task 2) consumes pre-run
baseline + candidate `EvalRun` rows and decides whether any candidate is safe
to promote under stricter-than-gate rules. `run_candidate_matrix` (WP05
Task 3) is a thin loop that runs the eval suite per candidate via an
injectable runner.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Callable
from uuid import UUID

import db
from db import EvalResult, EvalRun

from evals.compare import (
    _format_extra_candidate_cases_reason,
    _format_missing_baseline_cases_reason,
    compare_eval_runs,
)

logger = logging.getLogger(__name__)


BASE_CONFIG: dict[str, Any] = {
    "memory_retrieval_limit": 4,
    "memory_include_secret": False,   # WP07 Finding 2: renamed from
                                       # memory_include_private (which
                                       # silently granted secret-memory
                                       # access); default False so
                                       # optimizer runs match normal
                                       # ChatAgent retrieval.
    "memory_stopword_profile": "default",
    "chat_prompt_variant": "baseline",
}

# Initial knob-grid. Keep deliberately small; spec says "the first
# optimizer mostly proves the loop". Add knobs in later WPs.
CANDIDATE_MATRIX: dict[str, list[Any]] = {
    "memory_retrieval_limit": [3, 6, 10],
}


def generate_candidate_configs(base_config: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the deterministic candidate matrix. Each output dict is a
    full copy of `base_config` with one knob substituted from
    `CANDIDATE_MATRIX`. We do NOT take the cartesian product across
    knobs — that grows too fast. With only one knob enabled today we
    return one variant per value.
    """
    configs: list[dict[str, Any]] = []
    for knob, values in CANDIDATE_MATRIX.items():
        for v in values:
            variant = dict(base_config)
            variant[knob] = v
            configs.append(variant)
    return configs


@dataclass(frozen=True)
class OptimizerDecision:
    """Result of `select_best_candidate`. `selected_uuid` is None when
    no candidate passes every safety rule. `safe_candidates` and
    `rejected_candidates` are dicts with at least `uuid`, `mean`, and
    (for rejections) `reasons`."""
    selected_uuid: UUID | None = None
    reason: str = ""
    safe_candidates: list[dict[str, Any]] = field(default_factory=list)
    rejected_candidates: list[dict[str, Any]] = field(default_factory=list)


def _forbidden_memory_failure_uuids(run_uuid: UUID) -> list[str]:
    """Return the eval_case_uuids (stringified) of any EvalResult in
    this run whose details.forbidden_memories shows a leak
    (absent < total). Used as a safety rule on top of the gate.

    This rule is absolute (not delta-vs-baseline) — a candidate with any
    leaked forbidden memory is rejected even if the baseline had the
    same leak."""
    rows = (
        db.db.session.query(EvalResult)
        .filter(EvalResult.eval_run_uuid == run_uuid)
        .all()
    )
    leaked: list[str] = []
    for r in rows:
        fm = (r.details or {}).get("forbidden_memories") or {}
        total = fm.get("total", 0)
        absent = fm.get("absent", 0)
        if total > 0 and absent < total:
            leaked.append(str(r.eval_case_uuid))
    return leaked


def _evaluate_candidate(
    baseline_uuid: UUID, candidate_uuid: UUID,
    *, holdout_tolerance: float,
) -> tuple[bool, list[str], float]:
    """Apply the optimizer's stricter-than-gate safety rules.

    Returns (safe, reasons, candidate_mean). `reasons` lists why a
    candidate was rejected; empty when safe."""
    comp = compare_eval_runs(baseline_uuid, candidate_uuid)
    reasons: list[str] = []

    reason = _format_missing_baseline_cases_reason(comp.only_in_baseline)
    if reason is not None:
        reasons.append(reason)

    reason = _format_extra_candidate_cases_reason(comp.only_in_candidate)
    if reason is not None:
        reasons.append(reason)

    if comp.delta_mean < 0:
        reasons.append(
            f"mean dropped {-comp.delta_mean:.3f} (optimizer requires >= 0)"
        )

    pin_failures = [
        f for f in comp.new_failures if f.get("split") == "regression"
    ]
    if pin_failures:
        names = ", ".join(
            str(f.get("eval_case_name") or f.get("eval_case_uuid"))
            for f in pin_failures
        )
        reasons.append(f"regression-split pin broke: {names}")

    holdout_base = [e["baseline_score"] for e in comp.common
                    if e.get("split") == "holdout"]
    holdout_cand = [e["candidate_score"] for e in comp.common
                    if e.get("split") == "holdout"]
    if holdout_base and holdout_cand:
        mean_h_base = sum(holdout_base) / len(holdout_base)
        mean_h_cand = sum(holdout_cand) / len(holdout_cand)
        drop = mean_h_base - mean_h_cand
        if drop > holdout_tolerance:
            reasons.append(
                f"holdout dropped {drop:.3f} (> tolerance {holdout_tolerance})"
            )

    leaked = _forbidden_memory_failure_uuids(candidate_uuid)
    if leaked:
        reasons.append(
            f"forbidden-memory leak in {len(leaked)} case(s): "
            f"{', '.join(leaked)}"
        )

    return (len(reasons) == 0, reasons, comp.candidate_mean)


def select_best_candidate(
    baseline_run_uuid: UUID,
    candidate_run_uuids: list[UUID],
    *,
    holdout_tolerance: float = 0.05,
) -> OptimizerDecision:
    """Apply the optimizer safety rules to every candidate and pick the
    safe candidate with the highest mean. Returns an OptimizerDecision
    with `selected_uuid=None` when no candidate is safe."""
    safe: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for cuuid in candidate_run_uuids:
        is_safe, reasons, cand_mean = _evaluate_candidate(
            baseline_run_uuid, cuuid, holdout_tolerance=holdout_tolerance,
        )
        entry = {"uuid": cuuid, "mean": cand_mean}
        if is_safe:
            safe.append(entry)
        else:
            rejected.append({**entry, "reasons": reasons})

    if not safe:
        return OptimizerDecision(
            selected_uuid=None,
            reason="no safe improvement",
            safe_candidates=[],
            rejected_candidates=rejected,
        )

    best = max(safe, key=lambda e: e["mean"])
    return OptimizerDecision(
        selected_uuid=best["uuid"],
        reason=f"selected candidate improves mean to {best['mean']:.3f}",
        safe_candidates=safe,
        rejected_candidates=rejected,
    )


def _default_runner(
    config: dict[str, Any], case_filter: dict[str, Any],
) -> EvalRun:
    """Default candidate-matrix runner: invokes evals.runner.run_eval_suite
    threading the candidate config and case_filter through. Supported
    knobs (memory_retrieval_limit, memory_include_secret) are applied
    to memory_retrieval cases; unsupported knobs are recorded on the
    EvalRun.config under `unsupported_config_keys` instead of being
    silently dropped."""
    from evals.runner import run_eval_suite
    name = (
        "optimizer-candidate: "
        f"limit={config.get('memory_retrieval_limit', '?')}"
    )
    return run_eval_suite(
        name=name,
        agent_role="chat",
        config=config,
        case_filter=case_filter,
    )


def run_candidate_matrix(
    configs: list[dict[str, Any]],
    case_filter: dict[str, Any],
    *,
    runner: Callable[[dict[str, Any], dict[str, Any]], EvalRun] | None = None,
) -> list[EvalRun]:
    """Run one EvalRun per candidate config. `runner` is injectable for
    tests; defaults to a thin wrapper around evals.runner.run_eval_suite.

    The returned list preserves the input `configs` order; downstream
    tie-break in `select_best_candidate` depends on this."""
    runner = runner or _default_runner
    return [runner(cfg, case_filter) for cfg in configs]
