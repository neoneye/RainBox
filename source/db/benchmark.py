"""Persistence for the benchmark pages — the `benchmark_result` table.

The three suites (/benchmark_basic, /benchmark_story, /benchmark_kanban) are
three BenchmarkRunner instances whose live results live in memory. This module
is what makes a result outlast the process, so a newly-added model can be read
against what the others scored.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa

from db.models import BenchmarkResult, db

logger = logging.getLogger(__name__)

__all__ = [
    "COMPLETE_RETENTION",
    "PARTIAL_RETENTION",
    "benchmark_fingerprint",
    "benchmark_history",
    "record_benchmark_result",
]

# How many results each cell keeps, per bucket. Two buckets rather than one
# so a run of failures cannot evict the last known-good baseline — which is
# the number the page exists to show.
COMPLETE_RETENTION: int = 3
PARTIAL_RETENTION: int = 3


def benchmark_fingerprint(payload: Any) -> str:
    """A short stable hash of `payload`, for spotting that a stored result was
    measured under different settings than the current ones.

    Sorted keys: model kwargs arrive from JSONB in arbitrary order, and a
    fingerprint that moved with key order would flag every past entry as
    changed. Comparison only — never an identity, and never a reason to hide
    or delete a stored result.
    """
    canonical = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.blake2b(canonical.encode("utf-8"), digest_size=8).hexdigest()


def record_benchmark_result(
    *,
    spec_set: str,
    benchmark_name: str,
    target_uuid: UUID,
    target_label: str,
    model_name: str,
    provider: str,
    status: str,
    trials_done: int,
    trials_total: int,
    correct: int,
    mistakes: int,
    failures: int,
    total_elapsed: float,
    reasoning_chars: int | None,
    content_chars: int | None,
    error: str | None,
    config_fingerprint: str,
    spec_fingerprint: str,
    started_at: datetime | None,
    ended_at: datetime,
) -> BenchmarkResult:
    """Store one cell's terminal result and prune that cell to its retention.

    `completed` is derived here rather than passed: it is true only when the
    run finished AND every trial actually ran. A "done" that stopped at 3 of 5
    is not a baseline and must not evict one, and deriving it in one place is
    what keeps every caller from having to remember that.
    """
    completed = status == "done" and trials_done >= trials_total > 0
    row = BenchmarkResult(
        spec_set=spec_set,
        benchmark_name=benchmark_name,
        target_uuid=target_uuid,
        target_label=target_label,
        model_name=model_name,
        provider=provider,
        completed=completed,
        status=status,
        trials_done=trials_done,
        trials_total=trials_total,
        correct=correct,
        mistakes=mistakes,
        failures=failures,
        total_elapsed=total_elapsed,
        reasoning_chars=reasoning_chars,
        content_chars=content_chars,
        error=error,
        config_fingerprint=config_fingerprint,
        spec_fingerprint=spec_fingerprint,
        started_at=started_at,
        ended_at=ended_at,
    )
    db.session.add(row)
    db.session.flush()
    _prune_cell(spec_set, benchmark_name, target_uuid, completed)
    db.session.commit()
    return row


def _prune_cell(
    spec_set: str, benchmark_name: str, target_uuid: UUID, completed: bool
) -> None:
    """Drop everything past the retention for one cell's one bucket.

    At write time rather than on a schedule: the transaction is already open,
    and a bounded table needs no cron.
    """
    keep = COMPLETE_RETENTION if completed else PARTIAL_RETENTION
    survivors = (
        db.session.query(BenchmarkResult.id)
        .filter_by(
            spec_set=spec_set,
            benchmark_name=benchmark_name,
            target_uuid=target_uuid,
            completed=completed,
        )
        .order_by(BenchmarkResult.id.desc())
        .limit(keep)
        .subquery()
    )
    db.session.query(BenchmarkResult).filter(
        BenchmarkResult.spec_set == spec_set,
        BenchmarkResult.benchmark_name == benchmark_name,
        BenchmarkResult.target_uuid == target_uuid,
        BenchmarkResult.completed == completed,
        BenchmarkResult.id.notin_(sa.select(survivors.c.id)),
    ).delete(synchronize_session=False)


def benchmark_history(spec_set: str) -> dict[str, dict[str, dict[str, list[dict]]]]:
    """Every retained result for one suite, as
    {benchmark_name: {target_uuid: {"complete": [...], "partial": [...]}}},
    newest first within each bucket.

    Nested by name then target so the page can look a cell up directly.
    Everything is JSON-safe: this is handed to json.dumps by the /history
    endpoint, where a stray datetime becomes a 500 at request time.
    """
    rows = (
        db.session.query(BenchmarkResult)
        .filter_by(spec_set=spec_set)
        .order_by(BenchmarkResult.id.desc())
        .all()
    )
    out: dict[str, dict[str, dict[str, list[dict]]]] = {}
    for row in rows:
        by_target = out.setdefault(row.benchmark_name, {})
        buckets = by_target.setdefault(
            str(row.target_uuid), {"complete": [], "partial": []}
        )
        buckets["complete" if row.completed else "partial"].append(_entry(row))
    return out


def _entry(row: BenchmarkResult) -> dict[str, Any]:
    """One stored result as the page reads it."""
    return {
        "status": row.status,
        "completed": row.completed,
        "trials_done": row.trials_done,
        "trials_total": row.trials_total,
        "correct": row.correct,
        "mistakes": row.mistakes,
        "failures": row.failures,
        "total_elapsed": row.total_elapsed,
        "reasoning_chars": row.reasoning_chars,
        "content_chars": row.content_chars,
        "error": row.error,
        "config_fingerprint": row.config_fingerprint,
        "spec_fingerprint": row.spec_fingerprint,
        "target_label": row.target_label,
        "model_name": row.model_name,
        "provider": row.provider,
        "ended_at": row.ended_at.isoformat() if row.ended_at else None,
    }
