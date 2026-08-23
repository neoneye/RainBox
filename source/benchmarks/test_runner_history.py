"""BenchmarkRunner's persistence hook: which cell states get stored, and the
guarantee that storing one can never break a run."""

from unittest.mock import patch

import pytest

from benchmarks.runner import BenchmarkRunner, _empty_benchmark_entry
from webapp.core import app


@pytest.fixture
def runner():
    """A runner with one target and its benchmark entries, without touching
    the /models tree or starting a thread."""
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
    runner._state["targets"][0]["benchmarks"][2]["trials_done"] = 5
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
    """The hook is wired into _set_benchmark_status, not merely callable."""
    runner._state["targets"][0]["benchmarks"][0]["trials_done"] = 5
    with app.app_context(), patch("db.record_benchmark_result") as rec:
        runner._set_benchmark_status(0, 0, "done")

    assert rec.call_count == 1


def test_setting_a_non_terminal_status_records_nothing(runner):
    with app.app_context(), patch("db.record_benchmark_result") as rec:
        runner._set_benchmark_status(0, 0, "running")

    rec.assert_not_called()


def test_going_running_stamps_the_start_time(runner):
    """The stored result carries when it was taken; without this stamp every
    history entry would only know when it ended."""
    entry = runner._state["targets"][0]["benchmarks"][0]
    assert entry["started_at"] is None

    with app.app_context(), patch("db.record_benchmark_result"):
        runner._set_benchmark_status(0, 0, "running")

    assert entry["started_at"] is not None


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
