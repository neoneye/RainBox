"""VisionBenchmarkRunner: what a worker event does to the live state, and
starting under the shared one-suite slot."""

from unittest.mock import patch
from uuid import uuid4

import pytest
from flask import Flask

from benchmarks.exclusion import SLOT
from benchmarks.vision_ocr import CASES, Target
from benchmarks.vision_runner import LABEL, VisionBenchmarkRunner, _settings_summary


@pytest.fixture(autouse=True)
def clean_global_slot():
    before = SLOT.holder()
    yield
    if before is None and SLOT.holder() is not None:
        SLOT.release(SLOT.holder() or "")


def _target():
    return Target(str(uuid4()), "lfm3", "ollama", "lfm3-vl:2b", "ollama", "http://x", {})


def test_a_trial_event_is_stored_and_bumps_the_revision():
    runner = VisionBenchmarkRunner()
    target = _target()
    runner._state["current"] = {"uuid": target.uuid, "label": "lfm3", "phase": "starting",
                                "case": None, "trial": None, "since": 0}
    run = {"run_uuid": uuid4(), "started_at": None, "fingerprint": "f"}
    with patch("benchmarks.vision_runner.db.record_vision_trial") as record:
        runner._apply_event(target, run, {"t": "trial_start", "case": "center", "trial": 1})
        assert runner.get_state()["current"]["case"] == "center"
        runner._apply_event(target, run, {"t": "trial", "trial": {"case": "center"}})
    assert record.call_count == 1
    state = runner.get_state()
    assert state["trials_done"] == 1 and state["revision"] == 1


def test_a_failed_store_still_counts_the_trial():
    runner = VisionBenchmarkRunner()
    with patch("benchmarks.vision_runner.db.record_vision_trial", side_effect=RuntimeError("db")), \
         patch("benchmarks.vision_runner.db.session"):
        runner._store(_target(), {"run_uuid": uuid4(), "started_at": None, "fingerprint": ""},
                      {"case": "center"})
    assert runner.get_state()["trials_done"] == 1


def test_start_refuses_while_another_suite_holds_the_machine():
    runner = VisionBenchmarkRunner()
    assert SLOT.acquire("Story benchmark")
    try:
        assert runner.start(Flask(__name__), [str(uuid4())]) is False
        assert runner.get_state()["blocked_by"] == "Story benchmark"
    finally:
        SLOT.release("Story benchmark")


def test_start_validates_before_taking_the_slot():
    runner = VisionBenchmarkRunner()
    with pytest.raises(ValueError):
        runner.start(Flask(__name__), [])
    with pytest.raises(ValueError):
        runner.start(Flask(__name__), [str(uuid4())], trials=0)
    assert SLOT.holder() is None


def test_start_sizes_the_run_and_releases_the_slot_when_done():
    runner = VisionBenchmarkRunner()
    with patch.object(VisionBenchmarkRunner, "_run", lambda self, *a: self._finish()):
        assert runner.start(Flask(__name__), [str(uuid4()), str(uuid4())], trials=2)
        runner._thread.join(5)
    state = runner.get_state()
    assert state["running"] is False
    assert state["trials_total"] == 2 * len(CASES) * 2
    assert SLOT.holder() != LABEL


def test_settings_summary_names_thinking():
    assert _settings_summary({"thinking": False, "temperature": 0.2}) == "thinking off · t0.2"
    assert _settings_summary({}) == "thinking: model default"
