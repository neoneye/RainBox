"""benchmark_result: per-cell retention, the fingerprint helper, and reading
history back."""

import json as _json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

import db
from db.models import BenchmarkResult
from db.models import db as _db
from webapp.core import app

_TARGET = uuid4()


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


@pytest.fixture(autouse=True)
def clean_rows():
    with app.app_context():
        _db.session.query(BenchmarkResult).delete()
        _db.session.commit()
    yield
    with app.app_context():
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
            _record(benchmark_name="base64_decode", target_uuid=other_target,
                    correct=i)

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
            rows = (_db.session.query(BenchmarkResult)
                    .filter_by(spec_set=spec_set).all())
            assert len(rows) == db.COMPLETE_RETENTION


def test_fingerprint_is_stable_and_order_independent():
    """Kwargs come out of JSONB in arbitrary order; a fingerprint that moved
    with key order would flag every entry as changed."""
    a = db.benchmark_fingerprint({"temperature": 0.15, "context_window": 8192})
    b = db.benchmark_fingerprint({"context_window": 8192, "temperature": 0.15})
    assert a == b
    assert a != db.benchmark_fingerprint(
        {"temperature": 0.2, "context_window": 8192})


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
