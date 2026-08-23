# Benchmark Result History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist every benchmark cell's result so each `(spec_set, benchmark, target)` keeps its last 3 complete and 3 partial runs, the table repopulates after a restart, and hovering a cell shows what it scored before.

**Architecture:** A new `benchmark_result` table with accessors in `db/benchmark.py`. `BenchmarkRunner` gains one write hook at the two points a cell reaches a terminal state. The runner's `_state` stays purely live; a separate `/history` endpoint per page serves stored results, which the page fetches on load and merges into cells that are still `pending`.

**Tech Stack:** Python 3, Flask, SQLAlchemy + Postgres (JSONB), vanilla JS in a `render_template_string` page.

**Spec:** `docs/superpowers/specs/2026-08-23-benchmark-history-design.md`

## Global Constraints

- All commands run from `/Users/neoneye/git/rainbox/source`.
- Test runner is `./venv/bin/python -m pytest`. Never a bare `pytest`.
- Never run ad-hoc scripts against `rainbox_production`. Tests are already forced onto `rainbox_claude` by `rainbox/conftest.py`; nothing extra is needed for the test path.
- `/benchmark_editdocument` is **out of scope**. It has its own runner and views; do not touch `benchmarks/editdocument*.py` or `webapp/benchmark_editdocument_views.py`.
- History is keyed on `benchmark_name`, never on the benchmark's index into a spec list. Indices shift when a spec set is reordered.
- A history entry whose fingerprint differs from the current one is **flagged, never hidden or deleted**. The page exists to compare; dropping the before-value defeats it.
- Persistence must never break a run. Every write path logs and swallows its exception, the posture `llm/activity.py` takes for its own recording.
- The DB write must not be made while holding `BenchmarkRunner._lock` — the page polls `get_state()` on that same lock once a second.
- Comments and docstrings describe how the code works now. No "previously", no migration notes — git holds the history.
- Commit after every task. Never amend; each revision is its own commit.

---

### Task 1: The `benchmark_result` table and its write path

**Files:**
- Modify: `db/models.py` (append a model class)
- Create: `db/benchmark.py`
- Modify: `db/__init__.py` (re-export)
- Test: `db/test_benchmark.py` (create)

**Interfaces:**
- Produces:
  - `db.record_benchmark_result(*, spec_set, benchmark_name, target_uuid, target_label, model_name, provider, status, trials_done, trials_total, correct, mistakes, failures, total_elapsed, reasoning_chars, content_chars, error, config_fingerprint, spec_fingerprint, started_at, ended_at) -> BenchmarkResult` — derives `completed` itself and prunes.
  - `db.benchmark_fingerprint(payload: Any) -> str`
  - `db.COMPLETE_RETENTION = 3`, `db.PARTIAL_RETENTION = 3`
- Tasks 2–4 consume these.

- [x] **Step 1: Write the failing tests**

Create `db/test_benchmark.py`:

```python
"""benchmark_result: per-cell retention and the fingerprint helper."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

import db
from db.models import BenchmarkResult, db as _db
from webapp.core import app


def _record(benchmark_name="base64_decode", target_uuid=None, status="done",
            trials_done=5, trials_total=5, correct=5, spec_set="general",
            config_fingerprint="cfg", spec_fingerprint="spec"):
    return db.record_benchmark_result(
        spec_set=spec_set,
        benchmark_name=benchmark_name,
        target_uuid=target_uuid or _TARGET,
        target_label="t0.15 c8k struct",
        model_name="gemma4:e4b",
        provider="ollama",
        status=status,
        trials_done=trials_done,
        trials_total=trials_total,
        correct=correct,
        mistakes=0,
        failures=0,
        total_elapsed=10.5,
        reasoning_chars=None,
        content_chars=None,
        error=None,
        config_fingerprint=config_fingerprint,
        spec_fingerprint=spec_fingerprint,
        started_at=datetime.now(UTC),
        ended_at=datetime.now(UTC),
    )


_TARGET = uuid4()


@pytest.fixture(autouse=True)
def clean_rows():
    with app.app_context():
        _db.session.query(BenchmarkResult).delete()
        _db.session.commit()
        yield
        _db.session.query(BenchmarkResult).delete()
        _db.session.commit()


def test_a_finished_run_is_complete():
    with app.app_context():
        row = _record()
        assert row.completed is True
        assert row.status == "done"


def test_an_errored_run_is_partial():
    with app.app_context():
        assert _record(status="error", trials_done=0, correct=0).completed is False


def test_a_stopped_run_is_partial_even_with_trials_done():
    """Stopping after 2 of 5 trials is real data, but not a baseline."""
    with app.app_context():
        row = _record(status="stopped", trials_done=2, correct=2)
        assert row.completed is False


def test_a_done_run_missing_trials_is_partial():
    """status alone is not enough — a 'done' that ran 3 of 5 trials is not a
    complete result and must not evict one."""
    with app.app_context():
        assert _record(status="done", trials_done=3, correct=3).completed is False


def test_only_the_newest_three_complete_results_are_kept():
    with app.app_context():
        for i in range(5):
            _record(correct=i)
        rows = (_db.session.query(BenchmarkResult)
                .filter_by(completed=True).order_by(BenchmarkResult.id).all())
        assert len(rows) == db.COMPLETE_RETENTION
        assert [r.correct for r in rows] == [2, 3, 4]


def test_partials_do_not_evict_completes():
    """The whole reason partials get their own bucket: a run of failures must
    not cost you the last known-good baseline."""
    with app.app_context():
        for i in range(3):
            _record(correct=i)
        for _ in range(5):
            _record(status="error", trials_done=0, correct=0)

        complete = _db.session.query(BenchmarkResult).filter_by(completed=True).all()
        partial = _db.session.query(BenchmarkResult).filter_by(completed=False).all()
        assert len(complete) == db.COMPLETE_RETENTION
        assert sorted(r.correct for r in complete) == [0, 1, 2]
        assert len(partial) == db.PARTIAL_RETENTION


def test_retention_is_per_cell_not_global():
    """Two benchmarks on one target, and one benchmark on two targets, each
    keep their own three."""
    other_target = uuid4()
    with app.app_context():
        for i in range(4):
            _record(benchmark_name="base64_decode", correct=i)
            _record(benchmark_name="reverse_string", correct=i)
            _record(benchmark_name="base64_decode", target_uuid=other_target, correct=i)

        for name, tgt in (("base64_decode", _TARGET),
                          ("reverse_string", _TARGET),
                          ("base64_decode", other_target)):
            rows = (_db.session.query(BenchmarkResult)
                    .filter_by(benchmark_name=name, target_uuid=tgt).all())
            assert len(rows) == db.COMPLETE_RETENTION, (name, tgt)


def test_retention_is_per_spec_set():
    """The three pages share the table; a kanban cell must not evict a
    general-suite cell that happens to share a name."""
    with app.app_context():
        for i in range(4):
            _record(spec_set="general", correct=i)
            _record(spec_set="kanban", correct=i)

        for spec_set in ("general", "kanban"):
            rows = _db.session.query(BenchmarkResult).filter_by(spec_set=spec_set).all()
            assert len(rows) == db.COMPLETE_RETENTION


def test_fingerprint_is_stable_and_order_independent():
    """Kwargs come out of JSONB in arbitrary order; a fingerprint that moved
    with key order would flag every entry as changed."""
    a = db.benchmark_fingerprint({"temperature": 0.15, "context_window": 8192})
    b = db.benchmark_fingerprint({"context_window": 8192, "temperature": 0.15})
    assert a == b
    assert a != db.benchmark_fingerprint({"temperature": 0.2, "context_window": 8192})
```

- [x] **Step 2: Run to verify it fails**

Run: `./venv/bin/python -m pytest db/test_benchmark.py -q`
Expected: FAIL — `ImportError: cannot import name 'BenchmarkResult' from 'db.models'`.

- [x] **Step 3: Add the model**

Append to `db/models.py`, after the `EvalResult` class:

```python
class BenchmarkResult(db.Model):
    """One benchmark cell's outcome — a (spec_set, benchmark, target) triple.

    The durable record behind the three benchmark pages, whose live state is
    an in-memory dict on a BenchmarkRunner and does not survive a restart.
    Retention is per cell: the newest COMPLETE_RETENTION complete results and
    the newest PARTIAL_RETENTION partial ones (see db.benchmark).

    `benchmark_name` rather than the benchmark's index into its spec list:
    indices shift whenever a spec set is reordered, which would silently
    re-attach a cell's history to a different column.

    The target label columns are denormalized rather than joined. Targets are
    model_config_override rows, which the operator deletes and recreates
    freely; a join would make a removed override's history unreadable, while
    a stored name still says what was measured.
    """

    __tablename__ = "benchmark_result"
    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[UUID] = mapped_column(unique=True, default=uuid4)
    spec_set: Mapped[str] = mapped_column(Text)
    benchmark_name: Mapped[str] = mapped_column(Text)
    target_uuid: Mapped[UUID] = mapped_column()
    target_label: Mapped[str] = mapped_column(Text, default="")
    model_name: Mapped[str] = mapped_column(Text, default="")
    provider: Mapped[str] = mapped_column(Text, default="")
    completed: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(Text)
    trials_done: Mapped[int] = mapped_column(default=0)
    trials_total: Mapped[int] = mapped_column(default=0)
    correct: Mapped[int] = mapped_column(default=0)
    mistakes: Mapped[int] = mapped_column(default=0)
    failures: Mapped[int] = mapped_column(default=0)
    total_elapsed: Mapped[float] = mapped_column(default=0.0)
    reasoning_chars: Mapped[int | None] = mapped_column()
    content_chars: Mapped[int | None] = mapped_column()
    error: Mapped[str | None] = mapped_column(Text)
    config_fingerprint: Mapped[str] = mapped_column(Text, default="")
    spec_fingerprint: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    __table_args__ = (
        Index("ix_benchmark_result_cell", "spec_set", "benchmark_name", "target_uuid"),
    )
```

If `Index` is not already imported in `db/models.py`, add it to the existing
`from sqlalchemy import ...` line.

- [x] **Step 4: Add the accessors**

Create `db/benchmark.py`:

```python
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
```

- [x] **Step 5: Re-export from the db package**

In `db/__init__.py`, beside the other `from db.<module> import *` lines (near
the `from db.activity import *` line), add:

```python
from db.benchmark import *  # noqa: F401,F403  benchmark_result recording + retention
```

If `db/benchmark.py` needs an `__all__` to satisfy the star import, add one
listing `COMPLETE_RETENTION`, `PARTIAL_RETENTION`, `benchmark_fingerprint`,
`record_benchmark_result`.

- [x] **Step 6: Run the tests**

Run: `./venv/bin/python -m pytest db/test_benchmark.py -q`
Expected: PASS, 9 passed.

- [x] **Step 7: Commit**

```bash
git add db/models.py db/benchmark.py db/__init__.py db/test_benchmark.py
git commit -m "feat(db): store benchmark results with per-cell retention"
```

---

### Task 2: Reading history back

**Files:**
- Modify: `db/benchmark.py`
- Test: `db/test_benchmark.py`

**Interfaces:**
- Consumes: `record_benchmark_result` from Task 1.
- Produces: `db.benchmark_history(spec_set: str) -> dict[str, dict[str, dict[str, list[dict]]]]`, shaped `{benchmark_name: {target_uuid_str: {"complete": [entry, ...], "partial": [entry, ...]}}}`, newest first. Each `entry` is a JSON-safe dict with keys `status, completed, trials_done, trials_total, correct, mistakes, failures, total_elapsed, reasoning_chars, content_chars, error, config_fingerprint, spec_fingerprint, target_label, model_name, provider, ended_at` (ended_at as an ISO-8601 string). Tasks 5 and 6 consume this.

- [x] **Step 1: Write the failing tests**

Append to `db/test_benchmark.py`:

```python
def test_history_groups_by_benchmark_then_target():
    other_target = uuid4()
    with app.app_context():
        _record(benchmark_name="base64_decode", correct=1)
        _record(benchmark_name="reverse_string", correct=2)
        _record(benchmark_name="base64_decode", target_uuid=other_target, correct=3)

        hist = db.benchmark_history("general")

    assert set(hist) == {"base64_decode", "reverse_string"}
    assert set(hist["base64_decode"]) == {str(_TARGET), str(other_target)}
    assert hist["reverse_string"][str(_TARGET)]["complete"][0]["correct"] == 2


def test_history_is_newest_first():
    with app.app_context():
        for i in range(3):
            _record(correct=i)
        entries = db.benchmark_history("general")["base64_decode"][str(_TARGET)]

    assert [e["correct"] for e in entries["complete"]] == [2, 1, 0]


def test_history_separates_complete_from_partial():
    with app.app_context():
        _record(correct=4)
        _record(status="error", trials_done=0, correct=0)
        entries = db.benchmark_history("general")["base64_decode"][str(_TARGET)]

    assert len(entries["complete"]) == 1
    assert len(entries["partial"]) == 1
    assert entries["partial"][0]["status"] == "error"


def test_history_is_scoped_to_one_spec_set():
    with app.app_context():
        _record(spec_set="general", correct=1)
        _record(spec_set="kanban", benchmark_name="kanban_md_struct", correct=2)

        assert set(db.benchmark_history("general")) == {"base64_decode"}
        assert set(db.benchmark_history("kanban")) == {"kanban_md_struct"}


def test_history_entries_are_json_serializable():
    """The endpoint hands these straight to json.dumps; a datetime or a UUID
    in there is a 500 at request time, not at write time."""
    import json as _json

    with app.app_context():
        _record()
        hist = db.benchmark_history("general")

    _json.dumps(hist)  # must not raise


def test_history_carries_the_label_of_a_deleted_target():
    """Denormalized columns earn their keep here: nothing joins back to a
    model_config_override row that no longer exists."""
    with app.app_context():
        _record()
        entry = db.benchmark_history("general")["base64_decode"][str(_TARGET)]

    assert entry["complete"][0]["model_name"] == "gemma4:e4b"
    assert entry["complete"][0]["target_label"] == "t0.15 c8k struct"


def test_history_is_empty_for_a_spec_set_with_no_rows():
    with app.app_context():
        assert db.benchmark_history("story") == {}
```

- [x] **Step 2: Run to verify it fails**

Run: `./venv/bin/python -m pytest db/test_benchmark.py -q -k history`
Expected: FAIL — `AttributeError: module 'db' has no attribute 'benchmark_history'`.

- [x] **Step 3: Implement**

Append to `db/benchmark.py`:

```python
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
```

Add `benchmark_history` to `__all__` if Task 1 introduced one.

- [x] **Step 4: Run the tests**

Run: `./venv/bin/python -m pytest db/test_benchmark.py -q`
Expected: PASS, 16 passed.

- [x] **Step 5: Commit**

```bash
git add db/benchmark.py db/test_benchmark.py
git commit -m "feat(db): read benchmark history back grouped by cell"
```

---

### Task 3: The runner writes a cell when it finishes

**Files:**
- Modify: `benchmarks/runner.py`
- Test: `benchmarks/test_runner_history.py` (create)

**Interfaces:**
- Consumes: `db.record_benchmark_result`, `db.benchmark_fingerprint` from Task 1.
- Produces: `BenchmarkRunner._persist_cell(target_index: int, bench_index: int, status: str) -> None`. Task 4 calls it too.

- [x] **Step 1: Write the failing tests**

Create `benchmarks/test_runner_history.py`:

```python
"""BenchmarkRunner's persistence hook: which cell states get stored, and the
guarantee that storing one can never break a run."""

from unittest.mock import patch

import pytest

from benchmarks.runner import BenchmarkRunner
from webapp.core import app


@pytest.fixture
def runner():
    """A runner with one target and its benchmark entries, without touching
    the /models tree or starting a thread."""
    from benchmarks.runner import _empty_benchmark_entry

    r = BenchmarkRunner()
    r._state["targets"] = [{
        "index": 0,
        "uuid": "11111111-1111-1111-1111-111111111111",
        "provider": "ollama",
        "model_name": "gemma4:e4b",
        "model_display_name": "gemma4",
        "display_name": "t0.15 c8k struct",
        "status": "pending",
        "benchmarks": [
            _empty_benchmark_entry(name, 5) for name, _, _ in r.specs
        ],
    }]
    r._state["total_targets"] = 1
    return r


def test_a_finished_cell_is_recorded(runner):
    runner._state["targets"][0]["benchmarks"][0].update(
        {"trials_done": 5, "trials_total": 5, "correct": 4, "mistakes": 1}
    )
    with app.app_context(), patch("db.record_benchmark_result") as rec:
        runner._persist_cell(0, 0, "done")

    kwargs = rec.call_args.kwargs
    assert kwargs["benchmark_name"] == runner.specs[0][0]
    assert kwargs["spec_set"] == "general"
    assert kwargs["status"] == "done"
    assert kwargs["correct"] == 4
    assert kwargs["model_name"] == "gemma4:e4b"


def test_the_benchmark_is_identified_by_name_not_index(runner):
    """Reordering a spec set must not re-point a cell's history at another
    column, which is exactly what storing the index would do."""
    with app.app_context(), patch("db.record_benchmark_result") as rec:
        runner._persist_cell(0, 2, "done")

    assert rec.call_args.kwargs["benchmark_name"] == runner.specs[2][0]


def test_a_cell_with_no_trials_done_is_not_recorded(runner):
    """A cell killed before its first trial measured nothing. Storing a row of
    zeroes would put a fake result into the baseline."""
    with app.app_context(), patch("db.record_benchmark_result") as rec:
        runner._persist_cell(0, 0, "stopped")

    rec.assert_not_called()


def test_an_error_with_no_trials_is_still_recorded(runner):
    """Unlike a stop, an error IS the finding: this target cannot do this
    benchmark, and that belongs in the partial bucket."""
    runner._state["targets"][0]["benchmarks"][0]["error"] = "no tool support"
    with app.app_context(), patch("db.record_benchmark_result") as rec:
        runner._persist_cell(0, 0, "error")

    assert rec.call_args.kwargs["status"] == "error"
    assert rec.call_args.kwargs["error"] == "no tool support"


def test_a_failing_write_does_not_escape(runner):
    """A telemetry bug must never take a benchmark run down with it."""
    runner._state["targets"][0]["benchmarks"][0]["trials_done"] = 5
    with app.app_context(), patch("db.record_benchmark_result",
                                  side_effect=RuntimeError("db down")):
        runner._persist_cell(0, 0, "done")  # must not raise


def test_the_write_does_not_hold_the_state_lock(runner):
    """get_state() is polled once a second on this lock; a DB write inside it
    stalls every page on the box."""
    runner._state["targets"][0]["benchmarks"][0]["trials_done"] = 5
    seen = {}

    def check(**kwargs):
        seen["locked"] = runner._lock.locked()

    with app.app_context(), patch("db.record_benchmark_result", side_effect=check):
        runner._persist_cell(0, 0, "done")

    assert seen["locked"] is False


def test_setting_a_terminal_status_records_the_cell(runner):
    """The hook is wired into _set_benchmark_status, not only callable."""
    runner._state["targets"][0]["benchmarks"][0]["trials_done"] = 5
    with app.app_context(), patch("db.record_benchmark_result") as rec:
        runner._set_benchmark_status(0, 0, "done")

    assert rec.call_count == 1


def test_setting_a_non_terminal_status_records_nothing(runner):
    with app.app_context(), patch("db.record_benchmark_result") as rec:
        runner._set_benchmark_status(0, 0, "running")

    rec.assert_not_called()
```

- [x] **Step 2: Run to verify it fails**

Run: `./venv/bin/python -m pytest benchmarks/test_runner_history.py -q`
Expected: FAIL — `AttributeError: 'BenchmarkRunner' object has no attribute '_persist_cell'`.

- [x] **Step 3: Track when a cell started**

In `benchmarks/runner.py`, add a `started_at` key to `_empty_benchmark_entry`'s
returned dict, after `"status"`:

```python
        # Wall-clock start of this cell, stamped when it goes running. The
        # stored result carries it so a history entry says when it was taken.
        "started_at": None,
```

- [x] **Step 4: Add the persistence method**

Add to `BenchmarkRunner`, after `_set_benchmark_status`:

```python
    def _persist_cell(
        self, target_index: int, bench_index: int, status: str
    ) -> None:
        """Store one cell's terminal result, so it outlives this process.

        Snapshots under the lock and writes outside it: the page polls
        get_state() on that same lock once a second, and a DB round-trip
        inside it would stall every benchmark page on the box.

        A stop before the first trial is not recorded — it measured nothing,
        and a row of zeroes would put a fake result into the baseline. An
        error IS recorded whatever the trial count, because "this target
        cannot do this benchmark" is the finding.

        Logs and swallows on failure. A run must never die because its
        telemetry did (the posture llm/activity.py takes for the same reason).
        """
        try:
            with self._lock:
                target = self._state["targets"][target_index]
                entry = dict(target["benchmarks"][bench_index])
                target_uuid = target["uuid"]
                target_label = target.get("display_name") or ""
                model_name = target.get("model_name") or ""
                provider = target.get("provider") or ""
            if status != "error" and entry.get("trials_done", 0) <= 0:
                return

            name, _cls, params = self.specs[bench_index]
            try:
                resolved = db.resolved_model_kwargs(UUID(target_uuid))
            except Exception:
                # A row deleted mid-run still deserves its result stored; it
                # just cannot report what it was configured with.
                resolved = None

            db.record_benchmark_result(
                spec_set=self.spec_set,
                benchmark_name=name,
                target_uuid=UUID(target_uuid),
                target_label=target_label,
                model_name=model_name,
                provider=provider,
                status=status,
                trials_done=entry.get("trials_done", 0),
                trials_total=entry.get("trials_total", 0),
                correct=entry.get("correct", 0),
                mistakes=entry.get("mistakes", 0),
                failures=entry.get("failures", 0),
                total_elapsed=entry.get("total_elapsed", 0.0),
                reasoning_chars=entry.get("reasoning_chars"),
                content_chars=entry.get("content_chars"),
                error=entry.get("error"),
                config_fingerprint=(
                    db.benchmark_fingerprint(resolved) if resolved else ""
                ),
                spec_fingerprint=db.benchmark_fingerprint(params),
                started_at=(
                    datetime.fromtimestamp(entry["started_at"], UTC)
                    if entry.get("started_at") else None
                ),
                ended_at=datetime.now(UTC),
            )
        except Exception:
            logger.warning(
                "benchmark: could not store result for target %d bench %d",
                target_index, bench_index, exc_info=True,
            )
```

Add the imports this needs at the top of `benchmarks/runner.py`:

```python
from datetime import UTC, datetime
from uuid import UUID
```

- [x] **Step 5: Wire it into the status setter**

In `_set_benchmark_status`, stamp the start and call the hook. Replace the
method body's `with self._lock:` block so it reads:

```python
        with self._lock:
            entry = self._state["targets"][target_index]["benchmarks"][bench_index]
            entry["status"] = status
            if status == "running" and not entry.get("started_at"):
                entry["started_at"] = time.time()
            if error is not None:
                entry["error"] = error
            if reasoning_chars is not None:
                entry["reasoning_chars"] = reasoning_chars
            if content_chars is not None:
                entry["content_chars"] = content_chars
        # Outside the lock: _persist_cell takes it again for its snapshot.
        if status in ("done", "error"):
            self._persist_cell(target_index, bench_index, status)
```

- [x] **Step 6: Run the tests**

Run: `./venv/bin/python -m pytest benchmarks/test_runner_history.py -q`
Expected: PASS, 8 passed.

- [x] **Step 7: Run the existing benchmark tests**

Run: `./venv/bin/python -m pytest benchmarks/ -q`
Expected: PASS, no regressions.

- [x] **Step 8: Commit**

```bash
git add benchmarks/runner.py benchmarks/test_runner_history.py
git commit -m "feat(benchmarks): store a cell's result when it reaches a terminal state"
```

---

### Task 4: Stopping a run stores what it managed to measure

**Files:**
- Modify: `benchmarks/runner.py` (`_finish`, and `_run`'s `finally`)
- Test: `benchmarks/test_runner_history.py`

**Interfaces:**
- Consumes: `_persist_cell` from Task 3.

- [x] **Step 1: Write the failing tests**

Append to `benchmarks/test_runner_history.py`:

```python
def test_aborting_stores_a_cell_that_had_trials(runner):
    """Two of five trials is a real, partial measurement — losing it because
    the operator hit Stop is losing data they paid model time for."""
    runner._state["targets"][0]["status"] = "running"
    b = runner._state["targets"][0]["benchmarks"][0]
    b.update({"status": "running", "trials_done": 2, "correct": 2})

    with app.app_context(), patch("db.record_benchmark_result") as rec:
        runner._finish(aborted=True)

    assert rec.call_count == 1
    assert rec.call_args.kwargs["status"] == "stopped"
    assert rec.call_args.kwargs["trials_done"] == 2


def test_aborting_stores_nothing_for_an_untouched_cell(runner):
    runner._state["targets"][0]["benchmarks"][0]["status"] = "running"

    with app.app_context(), patch("db.record_benchmark_result") as rec:
        runner._finish(aborted=True)

    rec.assert_not_called()


def test_aborting_still_resets_running_cells_to_pending(runner):
    """The existing reset must survive: without it the row stays yellow with a
    frozen progress bar and its Start button never re-enables."""
    runner._state["targets"][0]["status"] = "running"
    runner._state["targets"][0]["benchmarks"][0].update(
        {"status": "running", "trials_done": 2}
    )

    with app.app_context(), patch("db.record_benchmark_result"):
        runner._finish(aborted=True)

    assert runner._state["targets"][0]["status"] == "pending"
    assert runner._state["targets"][0]["benchmarks"][0]["status"] == "pending"


def test_a_clean_finish_stores_nothing_extra(runner):
    """Cells already stored themselves on reaching done/error; _finish must not
    write them a second time."""
    runner._state["targets"][0]["benchmarks"][0].update(
        {"status": "done", "trials_done": 5}
    )

    with app.app_context(), patch("db.record_benchmark_result") as rec:
        runner._finish(aborted=False)

    rec.assert_not_called()
```

- [x] **Step 2: Run to verify it fails**

Run: `./venv/bin/python -m pytest benchmarks/test_runner_history.py -q -k abort`
Expected: FAIL — nothing is recorded on abort.

- [x] **Step 3: Persist partials before the reset**

In `_finish`, the abort branch currently resets in-progress entries. Collect
the running cells under the lock, then persist them after releasing it — and
before the reset, since the reset erases the status that says they were
running. Replace the `if aborted:` block with:

```python
            if aborted:
                # A target SIGKILLed mid-warmup/mid-trial never emits its
                # terminal status event, so its status would stay stuck at
                # "warming_up"/"running" forever — a yellow row with "warming
                # up…" and a frozen progress bar that polling won't clear
                # (polling stops once running flips false). Reset any
                # in-progress target/benchmark back to pending so the row
                # clears and its Start button works again.
                for ti, t in enumerate(self._state["targets"]):
                    if t["status"] in ("warming_up", "running"):
                        t["status"] = "pending"
                    for bi, b in enumerate(t["benchmarks"]):
                        if b["status"] == "running":
                            # Note it before the reset erases the evidence
                            # that this cell was the one interrupted.
                            interrupted.append((ti, bi))
                            b["status"] = "pending"
```

Declare `interrupted` before the `with self._lock:` block:

```python
        # (target_index, bench_index) of cells the stop caught mid-flight.
        # Collected under the lock, written after it — the DB round-trip must
        # not block the once-a-second get_state() poll.
        interrupted: list[tuple[int, int]] = []
```

and after the `with self._lock:` block, at the end of `_finish`:

```python
        for ti, bi in interrupted:
            # _persist_cell drops the ones with no trials done, so a cell
            # killed before its first trial stores nothing.
            self._persist_cell(ti, bi, "stopped")
```

- [x] **Step 4: Give `_finish` an app context**

`_run` calls `_finish` from a `finally` that sits *outside* its
`with app.app_context()`, so `_finish` has no context and its DB write would
raise. In `_run`, change the `finally` block to:

```python
        finally:
            # Its own context: the one above closed when the try block exited,
            # and _finish now stores the results a stop interrupted.
            with app.app_context():
                self._finish(aborted=self._stop_event.is_set())
```

- [x] **Step 5: Run the tests**

Run: `./venv/bin/python -m pytest benchmarks/test_runner_history.py -q`
Expected: PASS, 12 passed.

- [x] **Step 6: Commit**

```bash
git add benchmarks/runner.py benchmarks/test_runner_history.py
git commit -m "feat(benchmarks): keep the partial result a stopped run measured"
```

---

### Task 5: A `/history` endpoint per page

**Files:**
- Modify: `webapp/benchmark_views.py`, `webapp/benchmark_story_views.py`, `webapp/benchmark_kanban_views.py`
- Test: `webapp/test_benchmark_history_views.py` (create)

**Interfaces:**
- Consumes: `db.benchmark_history` from Task 2.
- Produces: `GET /benchmark_basic/history`, `/benchmark_story/history`, `/benchmark_kanban/history`, each returning the `benchmark_history` dict as JSON. Task 6 fetches them.

- [x] **Step 1: Write the failing tests**

Create `webapp/test_benchmark_history_views.py`:

```python
"""The /history endpoints — and the guarantee that history stays off /state."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

import db
from db.models import BenchmarkResult, db as _db
from webapp.core import app

TARGET = uuid4()


@pytest.fixture(autouse=True)
def clean_rows():
    with app.app_context():
        _db.session.query(BenchmarkResult).delete()
        _db.session.commit()
        yield
        _db.session.query(BenchmarkResult).delete()
        _db.session.commit()


def _seed(spec_set="general", benchmark_name="base64_decode"):
    with app.app_context():
        db.record_benchmark_result(
            spec_set=spec_set, benchmark_name=benchmark_name, target_uuid=TARGET,
            target_label="t0.15 c8k struct", model_name="gemma4:e4b",
            provider="ollama", status="done", trials_done=5, trials_total=5,
            correct=5, mistakes=0, failures=0, total_elapsed=9.5,
            reasoning_chars=None, content_chars=None, error=None,
            config_fingerprint="cfg", spec_fingerprint="spec",
            started_at=datetime.now(UTC), ended_at=datetime.now(UTC),
        )


@pytest.mark.parametrize("page,spec_set,name", [
    ("benchmark_basic", "general", "base64_decode"),
    ("benchmark_kanban", "kanban", "kanban_md_struct"),
    ("benchmark_story", "story", "story_text"),
])
def test_history_endpoint_returns_the_stored_cell(page, spec_set, name):
    _seed(spec_set=spec_set, benchmark_name=name)
    resp = app.test_client().get(f"/{page}/history")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body[name][str(TARGET)]["complete"][0]["correct"] == 5


def test_history_endpoint_is_scoped_to_its_own_page():
    """The three suites share one table; /benchmark_basic must not serve the
    kanban page's rows."""
    _seed(spec_set="kanban", benchmark_name="kanban_md_struct")
    body = app.test_client().get("/benchmark_basic/history").get_json()

    assert body == {}


def test_history_endpoint_is_empty_with_no_rows():
    assert app.test_client().get("/benchmark_basic/history").get_json() == {}


def test_state_does_not_carry_history():
    """/state is polled once a second. History on it would put every stored
    result on the wire every second for as long as the page is open — the same
    reason story artifacts are kept off it."""
    _seed()
    body = app.test_client().get("/benchmark_basic/state").get_json()

    assert "history" not in body
    assert "complete" not in str(body)
```

- [x] **Step 2: Run to verify it fails**

Run: `./venv/bin/python -m pytest webapp/test_benchmark_history_views.py -q`
Expected: FAIL — 404 on every `/history` route.

- [x] **Step 3: Add the three routes**

In `webapp/benchmark_views.py`, after `benchmark_basic_state`:

```python
@app.route("/benchmark_basic/history")
def benchmark_basic_history() -> Response:
    return app.response_class(
        json.dumps(db.benchmark_history("general")),
        mimetype="application/json",
    )
```

Add `import db` at the top of the file if it is not already imported.

In `webapp/benchmark_kanban_views.py`, after `benchmark_kanban_state`:

```python
@app.route("/benchmark_kanban/history")
def benchmark_kanban_history() -> Response:
    return app.response_class(
        json.dumps(db.benchmark_history("kanban")),
        mimetype="application/json",
    )
```

In `webapp/benchmark_story_views.py`, beside its state route:

```python
@app.route("/benchmark_story/history")
def benchmark_story_history() -> Response:
    return app.response_class(
        json.dumps(db.benchmark_history("story")),
        mimetype="application/json",
    )
```

Add `import db` and `from flask import Response` to each file if missing.

- [x] **Step 4: Pass the history URL into the page**

In `render_benchmark_page`'s signature add a parameter after `stop_endpoint`:

```python
    history_endpoint: str | None = None,
```

and in the `render_template_string(...)` call add:

```python
        history_url=url_for(history_endpoint) if history_endpoint else '',
```

Then pass the endpoint name from each page. In `webapp/benchmark_views.py`:

```python
        "benchmark_basic_state", "benchmark_basic_start", "benchmark_basic_stop",
        history_endpoint="benchmark_basic_history",
```

In `webapp/benchmark_kanban_views.py`, add
`history_endpoint="benchmark_kanban_history"` to its `render_benchmark_page`
call; in `webapp/benchmark_story_views.py`, add
`history_endpoint="benchmark_story_history"`.

- [x] **Step 5: Run the tests**

Run: `./venv/bin/python -m pytest webapp/test_benchmark_history_views.py webapp/test_benchmark_story_views.py -q`
Expected: PASS.

- [x] **Step 6: Commit**

```bash
git add webapp/benchmark_views.py webapp/benchmark_kanban_views.py \
        webapp/benchmark_story_views.py webapp/test_benchmark_history_views.py
git commit -m "feat(benchmarks): serve stored per-cell history on its own endpoint"
```

---

### Task 6: The page shows history

**Files:**
- Modify: `webapp/benchmark_views.py` (`BENCHMARK_TEMPLATE` — CSS and JS)
- Test: `webapp/test_benchmark_history_views.py`

**Interfaces:**
- Consumes: `history_url` from Task 5.

- [x] **Step 1: Write the failing tests**

These assert on the rendered page source, matching how the existing page tests
in `webapp/test_benchmark_story_views.py` work. Append to
`webapp/test_benchmark_history_views.py`:

```python
@pytest.mark.parametrize("page", ["benchmark_basic", "benchmark_kanban",
                                  "benchmark_story"])
def test_page_wires_up_history(page):
    body = app.test_client().get(f"/{page}").get_data(as_text=True)

    # Fetched from its own endpoint, not read off the polled state.
    assert f"/{page}/history" in body
    assert "function loadHistory" in body
    # Merged into cells that have no live result.
    assert "function historicEntry" in body
    # Refetched when a run ends, so a finished cell's card is current.
    assert "wasRunning" in body


@pytest.mark.parametrize("page", ["benchmark_basic", "benchmark_kanban",
                                  "benchmark_story"])
def test_page_has_the_hover_card_styles(page):
    body = app.test_client().get(f"/{page}").get_data(as_text=True)

    assert "cell-history" in body
    assert "td.bench:hover .cell-history" in body
    assert "historic" in body
```

- [x] **Step 2: Run to verify it fails**

Run: `./venv/bin/python -m pytest webapp/test_benchmark_history_views.py -q -k page_`
Expected: FAIL — none of those strings are in the page.

- [x] **Step 3: Add the CSS**

In `BENCHMARK_TEMPLATE`'s `<style>` block, after the `button.cell-start`
rules:

```css
  td.bench .historic{color:#64748b;font-style:italic}
  td.bench .historic-mark{font-style:normal;opacity:0.7;margin-right:0.25em}
  .cell-history{display:none;position:absolute;z-index:20;left:0;top:100%;
        min-width:22em;padding:0.5em 0.6em;border:1px solid #cbd5e1;
        border-radius:5px;background:#fff;box-shadow:0 4px 14px rgba(0,0,0,0.14);
        font-style:normal;color:#1a1a2e;text-align:left;white-space:nowrap}
  td.bench:hover .cell-history{display:block}
  .cell-history h4{margin:0 0 0.35em;font-size:90%;color:#475569;font-weight:600}
  .cell-history .hrow{display:flex;gap:0.8em;justify-content:space-between}
  .cell-history .hwhen{color:#475569}
  .cell-history .hsep{margin:0.4em 0 0.25em;padding-top:0.3em;
        border-top:1px solid #e2e8f0;color:#94a3b8;font-size:85%}
  .cell-history .hwarn{margin-top:0.4em;color:#b45309;font-size:85%;
        white-space:normal}
```

- [x] **Step 4: Add the fetch and merge**

In the template's `<script>`, after the `benchmarkNames` declaration, add:

```javascript
// Stored results, {benchmark_name: {target_uuid: {complete:[], partial:[]}}}.
// Fetched from its own endpoint rather than read off the once-a-second /state
// poll: putting every cell's history on that poll would put the whole table's
// past on the wire every second for as long as the page is open.
let history = {};
const historyUrl = {{ history_url|tojson }};

async function loadHistory() {
  if (!historyUrl) return;
  try {
    const resp = await fetch(historyUrl);
    if (resp.ok) history = await resp.json();
  } catch (e) {
    // A missing history is a degraded page, never a broken one.
    history = {};
  }
}

function cellHistory(benchName, targetUuid) {
  const byTarget = history[benchName];
  return (byTarget && byTarget[targetUuid]) || null;
}

// The newest complete result, else the newest partial — what a cell shows
// when this session has not run it yet.
function historicEntry(benchName, targetUuid) {
  const h = cellHistory(benchName, targetUuid);
  if (!h) return null;
  return (h.complete && h.complete[0]) || (h.partial && h.partial[0]) || null;
}

function fmtWhen(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return isNaN(d) ? '' : d.toLocaleString();
}

function historyRow(e) {
  const counts = `✓${e.correct} ✗${e.mistakes} !${e.failures}`;
  const per = e.trials_done > 0
    ? (e.total_elapsed / e.trials_done).toFixed(1) + 's/tr'
    : (e.status === 'done' ? '' : escapeHtml(e.status));
  return `<div class="hrow"><span class="hwhen">${escapeHtml(fmtWhen(e.ended_at))}</span>` +
         `<span>${e.trials_done}/${e.trials_total}</span>` +
         `<span>${counts}</span><span>${per}</span></div>`;
}

// The card. Absent entirely when a cell has no stored history — an empty box
// on hover is worse than no box.
function historyCard(benchName, targetUuid) {
  const h = cellHistory(benchName, targetUuid);
  if (!h) return '';
  const complete = h.complete || [];
  const partial = h.partial || [];
  if (!complete.length && !partial.length) return '';
  let inner = `<h4>${escapeHtml(benchName)}</h4>`;
  inner += complete.map(historyRow).join('');
  if (partial.length) {
    inner += `<div class="hsep">partial</div>` + partial.map(historyRow).join('');
  }
  // Flagged, never hidden: seeing what changed when you retuned the model is
  // the reason to keep the earlier number at all.
  const newest = complete[0] || partial[0];
  const stale = [...complete, ...partial].find(
    e => newest && e.config_fingerprint !== newest.config_fingerprint);
  if (stale) {
    inner += `<div class="hwarn">⚠ model arguments changed since ` +
             `${escapeHtml(fmtWhen(stale.ended_at))}</div>`;
  }
  return `<div class="cell-history">${inner}</div>`;
}
```

- [x] **Step 5: Render history into the cell**

Replace `renderBench(b, ti, bi)` with a version that takes the target uuid and
falls back to stored results. Change its signature and its `pending` branch:

```javascript
function renderBench(b, ti, bi, targetUuid) {
  const bname = benchmarkNames[bi];
  const card = historyCard(bname, targetUuid);
  if (b.status === 'done') {
    return `<div>${fmtCounts(b)}</div>${benchDetails(b)}${benchStories(b, ti, bi)}${card}`;
  }
  if (b.status === 'error') {
    const errText = b.error ? `<div class="err" style="font-size:85%">${escapeHtml(b.error)}</div>` : '';
    return `<div>${fmtCounts(b)}<span class="pill error" style="margin-left:0.4em">error</span></div>${errText}${benchDetails(b)}${benchStories(b, ti, bi)}${card}`;
  }
  if (b.status === 'pending') {
    // Nothing ran this session, so the last stored result stands in — marked
    // historic so a stale number is never mistaken for a fresh one.
    const e = historicEntry(bname, targetUuid);
    if (e) {
      const counts = `✓${e.correct} ✗${e.mistakes} !${e.failures}`;
      return `<div class="historic"><span class="historic-mark">&#8987;</span>` +
             `${e.trials_done}/${e.trials_total} ${counts}</div>${card}`;
    }
    return `<div class="muted">pending</div>${card}`;
  }
  // status === 'running'
  const pct = b.trials_total > 0 ? (b.trials_done / b.trials_total) : 0;
  return `<progress max="1" value="${pct}"></progress>` +
         `<div>${b.trials_done}/${b.trials_total} ${fmtCounts(b)}</div>${card}`;
}
```

Update its one call site inside `render()`:

```javascript
      return `<td class="bench">${cellStart(b, t.uuid, i, busy)}${renderBench(b, t.index, i, t.uuid)}</td>`;
```

- [x] **Step 6: Count historic cells in the score**

In `render()`, the score multiplies `b.correct` and `b.trials_total` per cell.
A restored table would otherwise score every row 0.0000, which is worse than
showing nothing. Replace the scoring loop body:

```javascript
  const scored = state.targets.map(t => {
    let num = 1;
    let denom = 1;
    for (let i = 0; i < t.benchmarks.length; i++) {
      const b = t.benchmarks[i];
      // A cell with no live result contributes its stored one, so a table
      // restored after a restart still ranks. Cells with neither contribute
      // (0 + 1)/(total + 1) exactly as they do today.
      const e = b.status === 'pending'
        ? historicEntry(benchmarkNames[i], t.uuid) : null;
      const correct = e ? e.correct : b.correct;
      const total = e ? e.trials_total : b.trials_total;
      num *= (correct + 1);
      denom *= (total + 1);
    }
```

Leave the rest of the scoring block (the `- 1` on each side, the ranking)
untouched.

- [x] **Step 7: Load on start, refresh when a run ends**

Find the polling loop that calls `render(state)`. Add a module-level flag and
a refresh beside it:

```javascript
// History is refetched when a run finishes, so a cell that just completed
// shows its new entry without a page reload.
let wasRunning = false;
async function refreshHistoryIfRunEnded(state) {
  if (wasRunning && !state.running) {
    await loadHistory();
  }
  wasRunning = !!state.running;
}
```

In the poll handler, call `await refreshHistoryIfRunEnded(state);` before
`render(state);` (make the handler `async` if it is not already).

At the point the page first kicks off polling, precede it with:

```javascript
loadHistory().then(() => { /* first render picks the table up from storage */ });
```

- [x] **Step 8: Run the tests**

Run: `./venv/bin/python -m pytest webapp/test_benchmark_history_views.py -q`
Expected: PASS.

- [x] **Step 9: Verify the page in a browser**

The operator runs the server (`python -m tools.serve_ui`); do not add a
launch config. With it running, load `/benchmark_basic` and confirm: cells
with stored results render greyed and italic with the hourglass marker,
hovering one shows the card, and the score column is non-zero.

If the server is not running, say so and leave this step unchecked rather than
claiming it passed.

- [x] **Step 10: Commit**

```bash
git add webapp/benchmark_views.py webapp/test_benchmark_history_views.py
git commit -m "feat(benchmarks): show stored results in the table and on hover"
```

---

### Task 7: Full suites and documentation

**Files:**
- Modify: `notes/` or the spec, only if a claim there is now wrong.

- [x] **Step 1: Run every affected suite**

Run: `./venv/bin/python -m pytest db/ benchmarks/ webapp/ -q`
Expected: no failures introduced by this branch.

- [x] **Step 2: Confirm editdocument was not touched**

Run: `git diff --stat main -- '*editdocument*'`
Expected: empty. That page was explicitly out of scope.

- [x] **Step 3: Confirm no index-keyed history crept in**

Run: `grep -rn "bench_index" db/benchmark.py db/models.py`
Expected: no output. History is keyed on the benchmark's name.

- [x] **Step 4: Commit any doc correction**

```bash
git add -A
git commit -m "docs(benchmarks): describe the stored per-cell history"
```

---

## Notes for the implementer

**The lock rule is not stylistic.** `get_state()` is polled once a second by
every open benchmark page. A DB round-trip inside `BenchmarkRunner._lock`
stalls all of them. Snapshot under the lock, write outside it — Task 3 has a
test that fails if this is got wrong.

**Persistence must not be able to break a run.** Every write path catches and
logs. A benchmark run costs real model time; losing one to a telemetry bug is
a much worse outcome than losing the telemetry.

**Complete vs partial is derived in one place** — `record_benchmark_result`.
Do not let callers pass `completed`; the rule (finished *and* every trial ran)
is easy to get subtly wrong at each call site.

**Never hide a stored result because its fingerprint moved.** Flag it. The
page's purpose is comparison, and the entry from before you changed the
temperature is the most interesting one there.
