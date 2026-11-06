"""Persistence for /benchmark_vision — the `benchmark_vision_trial` table.

A run is the trials one Start produced for one target, grouped by
`run_uuid`. Each target keeps its newest VISION_RUN_RETENTION runs, pruned
when a run is recorded, so the table stays bounded without a scheduled job
and the page can set a new run beside the ones before it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa

from db.models import BenchmarkVisionTrial, db

__all__ = [
    "VISION_RUN_RETENTION",
    "prune_vision_runs",
    "record_vision_trial",
    "vision_runs",
    "vision_trial_reasoning",
]

VISION_RUN_RETENTION: int = 5

# Trial fields copied straight from benchmarks.vision_ocr.Trial.to_json().
_TRIAL_FIELDS = (
    "trial_index", "passed", "missing", "also_named", "error", "content",
    "reasoning", "elapsed", "first_token", "thinking", "load_s", "prefill_s",
    "decode_s", "prompt_tokens", "cached_tokens", "output_tokens",
    "reasoning_tokens", "done_reason",
)


def record_vision_trial(
    *,
    run_uuid: UUID,
    run_started_at: datetime,
    target_uuid: UUID,
    target_label: str,
    model_name: str,
    provider: str,
    config_fingerprint: str,
    trial: dict[str, Any],
) -> BenchmarkVisionTrial:
    """Store one trial, as `Trial.to_json()` shaped it."""
    row = BenchmarkVisionTrial(
        run_uuid=run_uuid,
        run_started_at=run_started_at,
        target_uuid=target_uuid,
        target_label=target_label,
        model_name=model_name,
        provider=provider,
        config_fingerprint=config_fingerprint,
        case_name=trial["case"],
        **{k: trial.get(k) for k in _TRIAL_FIELDS if trial.get(k) is not None},
    )
    db.session.add(row)
    db.session.commit()
    return row


def prune_vision_runs(target_uuid: UUID, keep: int = VISION_RUN_RETENTION) -> int:
    """Delete every run of `target_uuid` older than its newest `keep`.
    Returns the number of trial rows removed."""
    newest = (
        db.session.query(BenchmarkVisionTrial.run_uuid)
        .filter_by(target_uuid=target_uuid)
        .group_by(BenchmarkVisionTrial.run_uuid)
        .order_by(sa.func.max(BenchmarkVisionTrial.run_started_at).desc())
        .limit(keep)
        .subquery()
    )
    removed = db.session.query(BenchmarkVisionTrial).filter(
        BenchmarkVisionTrial.target_uuid == target_uuid,
        BenchmarkVisionTrial.run_uuid.notin_(sa.select(newest.c.run_uuid)),
    ).delete(synchronize_session=False)
    db.session.commit()
    return removed


def vision_runs() -> dict[str, list[dict[str, Any]]]:
    """Every retained run, as {target_uuid: [run, …]} with the newest run
    first and its trials in the order they ran.

    Reasoning text is left out — it can run to kilobytes per trial and the
    page fetches it one trial at a time (`vision_trial_reasoning`). Each trial
    says how long it was instead. JSON-safe throughout.
    """
    rows = (
        db.session.query(BenchmarkVisionTrial)
        .order_by(BenchmarkVisionTrial.run_started_at.desc(), BenchmarkVisionTrial.id)
        .all()
    )
    out: dict[str, list[dict[str, Any]]] = {}
    runs: dict[UUID, dict[str, Any]] = {}
    for row in rows:
        run = runs.get(row.run_uuid)
        if run is None:
            run = {
                "run_uuid": str(row.run_uuid),
                "started_at": row.run_started_at.isoformat(),
                "target_label": row.target_label,
                "model_name": row.model_name,
                "provider": row.provider,
                "config_fingerprint": row.config_fingerprint,
                "trials": [],
            }
            runs[row.run_uuid] = run
            out.setdefault(str(row.target_uuid), []).append(run)
        run["trials"].append(_trial(row))
    return out


def vision_trial_reasoning(trial_uuid: UUID) -> str | None:
    """The stored reasoning text of one trial, or None if there is no such
    trial."""
    row = db.session.query(BenchmarkVisionTrial).filter_by(uuid=trial_uuid).one_or_none()
    return None if row is None else row.reasoning


def _trial(row: BenchmarkVisionTrial) -> dict[str, Any]:
    decode_tps = (row.output_tokens / row.decode_s
                  if row.output_tokens and row.decode_s else None)
    return {
        "uuid": str(row.uuid),
        "case": row.case_name,
        "trial_index": row.trial_index,
        "passed": row.passed,
        "missing": row.missing or [],
        "also_named": row.also_named or [],
        "error": row.error,
        "content": row.content,
        "reasoning_chars": len(row.reasoning or ""),
        "elapsed": row.elapsed,
        "first_token": row.first_token,
        "thinking": row.thinking,
        "load_s": row.load_s,
        "prefill_s": row.prefill_s,
        "decode_s": row.decode_s,
        "decode_tps": decode_tps,
        "prompt_tokens": row.prompt_tokens,
        "cached_tokens": row.cached_tokens,
        "output_tokens": row.output_tokens,
        "reasoning_tokens": row.reasoning_tokens,
        "done_reason": row.done_reason,
    }
