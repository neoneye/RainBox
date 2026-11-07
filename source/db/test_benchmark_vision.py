"""benchmark_vision_trial: storing trials, reading runs back, retention."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

import db
from db.models import BenchmarkVisionTrial
from db.models import db as _db
from webapp.core import app

_TARGET = uuid4()
_T0 = datetime(1999, 3, 31, 12, 0, tzinfo=UTC)


def _trial(case="center", index=0, passed=True, **extra):
    return {"case": case, "trial_index": index, "passed": passed, "missing": [],
            "also_named": [], "error": None, "content": "Testing", "reasoning": "look closely",
            "elapsed": 1.5, "first_token": 0.2, "thinking": 1.0, "load_s": None,
            "prefill_s": 0.3, "decode_s": 1.0, "prompt_tokens": 290, "cached_tokens": 0,
            "output_tokens": 40, "reasoning_tokens": 30, "done_reason": "stop", **extra}


def _record(run_uuid, started_at, trial, target=_TARGET, trials_per_case=3):
    return db.record_vision_trial(
        run_uuid=run_uuid, run_started_at=started_at, target_uuid=target,
        target_label="lfm3", model_name="lfm3-vl:2b", provider="ollama",
        config_fingerprint="cfg", trials_per_case=trials_per_case, trial=trial)


@pytest.fixture(autouse=True)
def clean_rows():
    with app.app_context():
        _db.session.query(BenchmarkVisionTrial).delete()
        _db.session.commit()
    yield
    with app.app_context():
        _db.session.query(BenchmarkVisionTrial).delete()
        _db.session.commit()


def test_runs_read_back_newest_first_with_trials_in_order():
    old, new = uuid4(), uuid4()
    with app.app_context():
        _record(old, _T0, _trial())
        _record(new, _T0 + timedelta(hours=1), _trial("center", 0))
        _record(new, _T0 + timedelta(hours=1), _trial("top_left", 0, passed=False,
                                                     missing=["cat"], content="bug"))
        runs = db.vision_runs()[str(_TARGET)]
    assert [r["run_uuid"] for r in runs] == [str(new), str(old)]
    # The planned size travels with the run, so two answers of a 3-per-case
    # run read against 18, not 2.
    assert runs[0]["trials_per_case"] == 3
    assert [t["case"] for t in runs[0]["trials"]] == ["center", "top_left"]
    second = runs[0]["trials"][1]
    assert second["passed"] is False and second["missing"] == ["cat"]
    assert second["decode_tps"] == 40.0


def test_runs_leave_the_reasoning_text_for_its_own_request():
    with app.app_context():
        row = _record(uuid4(), _T0, _trial())
        trial = db.vision_runs()[str(_TARGET)][0]["trials"][0]
        assert "reasoning" not in trial and trial["reasoning_chars"] == len("look closely")
        assert db.vision_trial_reasoning(row.uuid) == "look closely"
        assert db.vision_trial_reasoning(uuid4()) is None


def test_prune_keeps_each_targets_newest_runs():
    other = uuid4()
    runs = [uuid4() for _ in range(4)]
    with app.app_context():
        for i, run in enumerate(runs):
            for case in ("center", "top_left"):
                _record(run, _T0 + timedelta(minutes=i), _trial(case))
        _record(uuid4(), _T0, _trial(), target=other)
        removed = db.prune_vision_runs(_TARGET, keep=2)
        kept = [r["run_uuid"] for r in db.vision_runs()[str(_TARGET)]]
        assert removed == 4
        assert kept == [str(runs[3]), str(runs[2])]
        assert len(db.vision_runs()[str(other)]) == 1
