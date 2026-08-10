"""Aggregation over `llm_call` — everything the /activity page reads.

Two audiences share this module. The instrumentation handler asks the two
per-model questions it needs to score a call it is about to record
(`recent_throughputs`, `recent_prefix_chains`); the page asks for windowed
rollups. Both go through the same indexes.

Cached tokens are always read as `COALESCE(reported, estimated, 0)`: a
provider that states its own cache usage is believed, our timing-derived
estimate fills the gap on the local backends that state nothing, and a call
we could not judge counts as uncached rather than dropping out of the
denominator and flattering the hit rate.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa

from db.models import LlmCall, db

logger = logging.getLogger(__name__)

# How many recent calls inform a model's cold-prefill baseline. Enough to
# contain some genuinely cold calls, recent enough to track the machine as it
# is now rather than as it was before a model or hardware change.
THROUGHPUT_WINDOW: int = 200

# How many recent prompts a new one is scored against. Small because the
# runtime itself only keeps a handful of prefixes warm.
PREFIX_WINDOW: int = 8

RETENTION_DAYS: int = 90

# Dimensions the page may group by. A whitelist, not a format string: the
# value arrives from a query parameter.
_DIMENSIONS: dict[str, Any] = {
    "model": LlmCall.model,
    "caller": LlmCall.caller,
    "provider": LlmCall.provider,
}

_WRITABLE_COLUMNS: frozenset[str] = frozenset(sa.inspect(LlmCall).columns.keys())


def _cached_tokens():
    """Effective cached-token count for a row, in preference order."""
    return sa.func.coalesce(
        LlmCall.cached_tokens_reported, LlmCall.cached_tokens_estimated, 0
    )


def _prefill_throughput():
    """Prompt tokens per second of prefill — the quantity whose slow tail is
    a model's cold baseline. NULL where the provider reported no timing, so
    percentile and average both skip those rows."""
    return sa.case(
        (
            sa.and_(LlmCall.prefill_ms > 0, LlmCall.prompt_tokens.is_not(None)),
            LlmCall.prompt_tokens * 1000.0 / LlmCall.prefill_ms,
        ),
        else_=None,
    )


def _decode_throughput():
    """Completion tokens per second — generation speed, the "throughput" a
    reader expects from a dashboard."""
    return sa.case(
        (
            sa.and_(LlmCall.decode_ms > 0, LlmCall.completion_tokens.is_not(None)),
            LlmCall.completion_tokens * 1000.0 / LlmCall.decode_ms,
        ),
        else_=None,
    )


def record_llm_call(row: dict) -> LlmCall:
    """Insert one recorded call. Rejects unknown keys loudly — a mistyped
    column name would otherwise vanish and quietly under-report."""
    unknown = set(row) - _WRITABLE_COLUMNS
    if unknown:
        raise TypeError(f"llm_call has no column(s): {sorted(unknown)}")
    call = LlmCall(**row)
    db.session.add(call)
    db.session.commit()
    return call


def recent_throughputs(model: str | None, limit: int = THROUGHPUT_WINDOW) -> list[float]:
    """Recent prefill throughputs (tokens/sec) for one model, newest first."""
    if not model:
        return []
    throughput = _prefill_throughput()
    rows = db.session.execute(
        sa.select(throughput)
        .where(LlmCall.model == model, throughput.is_not(None))
        .order_by(LlmCall.started_at.desc())
        .limit(limit)
    ).all()
    return [float(r[0]) for r in rows]


def recent_prefix_chains(
    model: str | None, limit: int = PREFIX_WINDOW
) -> list[list[str]]:
    """Prompt-prefix hash chains from this model's most recent calls, newest
    first. Empty chains are omitted — they can't match anything."""
    if not model:
        return []
    rows = db.session.execute(
        sa.select(LlmCall.prefix_chain)
        .where(
            LlmCall.model == model,
            LlmCall.prefix_chain.is_not(None),
            sa.func.jsonb_array_length(LlmCall.prefix_chain) > 0,
        )
        .order_by(LlmCall.started_at.desc())
        .limit(limit)
    ).all()
    return [r[0] for r in rows if r[0]]


def _window(start: datetime, end: datetime, model: str | None):
    clauses = [LlmCall.started_at >= start, LlmCall.started_at < end]
    if model:
        clauses.append(LlmCall.model == model)
    return clauses


def activity_summary(
    start: datetime, end: datetime, model: str | None = None
) -> dict[str, Any]:
    """Headline totals for the window.

    Rates are token-weighted, not call-weighted: caching a 20k-token prompt
    is worth a hundred times caching a 200-token one, and a per-call average
    would say they were equal.
    """
    cached = _cached_tokens()
    row = db.session.execute(
        sa.select(
            sa.func.count().label("calls"),
            sa.func.coalesce(sa.func.sum(LlmCall.prompt_tokens), 0),
            sa.func.coalesce(sa.func.sum(LlmCall.completion_tokens), 0),
            sa.func.coalesce(sa.func.sum(cached), 0),
            sa.func.coalesce(sa.func.sum(LlmCall.reusable_prefix_tokens), 0),
            sa.func.count().filter(LlmCall.ok.is_(False)).label("failures"),
        ).where(*_window(start, end, model))
    ).one()
    calls, prompt_tokens, completion_tokens, cached_tokens, reusable, failures = row
    return {
        "calls": int(calls),
        "failures": int(failures),
        "prompt_tokens": int(prompt_tokens),
        "completion_tokens": int(completion_tokens),
        "cached_tokens": int(cached_tokens),
        "reusable_tokens": int(reusable),
        "hit_rate": (cached_tokens / prompt_tokens) if prompt_tokens else None,
        "reusable_rate": (reusable / prompt_tokens) if prompt_tokens else None,
        "seconds_saved": _seconds_saved(start, end, model),
    }


def _seconds_saved(start: datetime, end: datetime, model: str | None) -> float:
    """Prefill seconds the cache avoided, per model against that model's own
    cold rate. Summed across models, because a fast small model and a slow
    large one save very different amounts of time per cached token."""
    total = 0.0
    for row in activity_rollup(start, end, dimension="model", model=model):
        total += row["seconds_saved"]
    return total


def activity_series(
    start: datetime,
    end: datetime,
    bucket_seconds: int,
    model: str | None = None,
) -> list[dict[str, Any]]:
    """Per-bucket totals across the window, including buckets with no calls.

    Empty buckets are emitted deliberately: a quiet period should render as a
    gap in the chart, not close up and put Monday next to Wednesday.

    Buckets are aligned to `start` rather than to the epoch, so the last
    bucket always ends at `end` and "past 3 hours" means exactly that.
    """
    if bucket_seconds <= 0:
        raise ValueError("bucket_seconds must be positive")
    span = (end - start).total_seconds()
    count = max(1, int(-(-span // bucket_seconds)))  # ceil

    cached = _cached_tokens()
    latency = LlmCall.total_ms
    decode_tps = _decode_throughput()
    index = sa.cast(
        sa.func.floor(
            sa.extract("epoch", LlmCall.started_at - sa.literal(start))
            / bucket_seconds
        ),
        sa.Integer,
    )
    rows = db.session.execute(
        sa.select(
            index.label("idx"),
            sa.func.count(),
            sa.func.coalesce(sa.func.sum(LlmCall.prompt_tokens), 0),
            sa.func.coalesce(sa.func.sum(LlmCall.completion_tokens), 0),
            sa.func.coalesce(sa.func.sum(cached), 0),
            sa.func.coalesce(sa.func.sum(LlmCall.reusable_prefix_tokens), 0),
            sa.func.avg(latency),
            sa.func.percentile_cont(0.5).within_group(latency.asc()),
            sa.func.percentile_cont(0.9).within_group(latency.asc()),
            sa.func.percentile_cont(0.99).within_group(latency.asc()),
            sa.func.avg(decode_tps),
            sa.func.percentile_cont(0.5).within_group(decode_tps.asc()),
            sa.func.percentile_cont(0.9).within_group(decode_tps.asc()),
        )
        .where(*_window(start, end, model))
        .group_by(index)
    ).all()
    by_index = {int(r[0]): r for r in rows}

    buckets: list[dict[str, Any]] = []
    for i in range(count):
        row = by_index.get(i)
        prompt_tokens = int(row[2]) if row else 0
        cached_tokens = int(row[4]) if row else 0
        buckets.append(
            {
                "start": start + timedelta(seconds=bucket_seconds * i),
                "calls": int(row[1]) if row else 0,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": int(row[3]) if row else 0,
                "cached_tokens": cached_tokens,
                "uncached_tokens": max(0, prompt_tokens - cached_tokens),
                "reusable_tokens": int(row[5]) if row else 0,
                # None, not zero, in an empty bucket: zero milliseconds would
                # draw as a real and impossibly fast bar.
                "avg_latency_ms": _f(row[6]) if row else None,
                "p50_latency_ms": _f(row[7]) if row else None,
                "p90_latency_ms": _f(row[8]) if row else None,
                "p99_latency_ms": _f(row[9]) if row else None,
                "avg_throughput_tps": _f(row[10]) if row else None,
                "p50_throughput_tps": _f(row[11]) if row else None,
                "p90_throughput_tps": _f(row[12]) if row else None,
                "hit_rate": (cached_tokens / prompt_tokens) if prompt_tokens else None,
            }
        )
    return buckets


def activity_rollup(
    start: datetime,
    end: datetime,
    dimension: str = "model",
    model: str | None = None,
) -> list[dict[str, Any]]:
    """Per-group aggregates: volume, cache behaviour, latency and throughput
    percentiles, and the seconds the cache saved.

    `dimension` is looked up in a whitelist rather than interpolated — it
    arrives from a query string.
    """
    try:
        dim_col = _DIMENSIONS[dimension]
    except KeyError:
        raise ValueError(
            f"unknown dimension {dimension!r}; expected one of "
            f"{sorted(_DIMENSIONS)}"
        ) from None

    cached = _cached_tokens()
    prefill_tps = _prefill_throughput()
    decode_tps = _decode_throughput()
    latency = LlmCall.total_ms

    rows = db.session.execute(
        sa.select(
            dim_col.label("key"),
            sa.func.count().label("calls"),
            sa.func.count().filter(LlmCall.ok.is_(False)).label("failures"),
            # Calls we could actually judge. A model that is still
            # calibrating produces rows with no verdict either way, and
            # "we don't know yet" must not read as "the cache did nothing".
            sa.func.count()
            .filter(
                sa.or_(
                    LlmCall.cached_tokens_reported.is_not(None),
                    LlmCall.cached_tokens_estimated.is_not(None),
                )
            )
            .label("judged_calls"),
            sa.func.coalesce(sa.func.sum(LlmCall.prompt_tokens), 0),
            sa.func.coalesce(sa.func.sum(LlmCall.completion_tokens), 0),
            sa.func.coalesce(sa.func.sum(cached), 0),
            sa.func.coalesce(sa.func.sum(LlmCall.reusable_prefix_tokens), 0),
            sa.func.avg(latency),
            sa.func.percentile_cont(0.5).within_group(latency.asc()),
            sa.func.percentile_cont(0.9).within_group(latency.asc()),
            sa.func.percentile_cont(0.99).within_group(latency.asc()),
            sa.func.avg(decode_tps),
            sa.func.percentile_cont(0.5).within_group(decode_tps.asc()),
            sa.func.percentile_cont(0.9).within_group(decode_tps.asc()),
            # The slow tail of prefill throughput: this model's cold rate.
            sa.func.percentile_cont(0.05).within_group(prefill_tps.asc()),
            sa.func.avg(prefill_tps),
        )
        .where(*_window(start, end, model))
        .group_by(dim_col)
        .order_by(sa.func.count().desc())
    ).all()

    out: list[dict[str, Any]] = []
    for r in rows:
        (
            key, calls, failures, judged, prompt_tokens, completion_tokens,
            cached_tokens, reusable, avg_lat, p50_lat, p90_lat, p99_lat,
            avg_tps, p50_tps, p90_tps, cold_rate, avg_prefill_tps,
        ) = r
        prompt_tokens = int(prompt_tokens)
        cached_tokens = int(cached_tokens)
        out.append(
            {
                "key": key or "unknown",
                "calls": int(calls),
                "failures": int(failures),
                "judged_calls": int(judged),
                "prompt_tokens": prompt_tokens,
                "completion_tokens": int(completion_tokens),
                "cached_tokens": cached_tokens,
                "uncached_tokens": max(0, prompt_tokens - cached_tokens),
                "reusable_tokens": int(reusable),
                "hit_rate": (cached_tokens / prompt_tokens) if prompt_tokens else None,
                "reusable_rate": (int(reusable) / prompt_tokens)
                if prompt_tokens
                else None,
                "avg_latency_ms": _f(avg_lat),
                "p50_latency_ms": _f(p50_lat),
                "p90_latency_ms": _f(p90_lat),
                "p99_latency_ms": _f(p99_lat),
                "avg_throughput_tps": _f(avg_tps),
                "p50_throughput_tps": _f(p50_tps),
                "p90_throughput_tps": _f(p90_tps),
                "cold_rate_tps": _f(cold_rate),
                "avg_prefill_tps": _f(avg_prefill_tps),
                "seconds_saved": (cached_tokens / float(cold_rate))
                if cold_rate
                else 0.0,
            }
        )
    return out


def _f(value: Any) -> float | None:
    return None if value is None else float(value)


def recent_llm_calls(limit: int = 50, model: str | None = None) -> list[LlmCall]:
    """The newest calls, for the drill-down table."""
    query = db.session.query(LlmCall)
    if model:
        query = query.filter(LlmCall.model == model)
    return query.order_by(LlmCall.started_at.desc()).limit(limit).all()


_last_prune_at: datetime | None = None


def reset_llm_call_prune_clock() -> None:
    """Forget when the last prune ran. For tests."""
    global _last_prune_at
    _last_prune_at = None


def maybe_prune_llm_calls(now: datetime) -> bool:
    """Prune at most once a day, for the cron tick to call on every pass.

    The guard is per-process rather than persisted: a restart costing one
    extra indexed DELETE is cheaper than a settings round-trip every second,
    and pruning twice is harmless. Returns whether it actually pruned —
    failures are swallowed, because housekeeping must never wedge the
    scheduler loop.
    """
    global _last_prune_at
    if _last_prune_at is not None and now - _last_prune_at < timedelta(days=1):
        return False
    try:
        deleted = prune_llm_calls()
    except Exception:
        logger.warning("llm_call prune failed; will retry later", exc_info=True)
        return False
    _last_prune_at = now
    if deleted:
        logger.info("pruned %d llm_call rows past %d days", deleted, RETENTION_DAYS)
    return True


def prune_llm_calls(older_than_days: int = RETENTION_DAYS) -> int:
    """Drop rows past the retention horizon. Returns how many went."""
    cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
    deleted = (
        db.session.query(LlmCall)
        .filter(LlmCall.started_at < cutoff)
        .delete(synchronize_session=False)
    )
    db.session.commit()
    return int(deleted)
