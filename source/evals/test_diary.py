"""diary_recall scoring: gold hits, separate candidate/injected measures, and
hard failures an averaged score cannot mask."""

from types import SimpleNamespace

import db
from diary.parsing import sha256_hex
from diary.test_retrieval import app_ctx, live, world  # noqa: F401 — fixtures
from evals.diary import score_diary_recall_case


def _case(live, args, gold=(), forbidden=()):
    return SimpleNamespace(
        input={"room_uuid": str(live.room), "agent_uuid": str(live.agent), "models_local": True,
               "args": args},
        expected={"gold": list(gold), "forbidden_sources": list(forbidden)})


def _gold(live, path, needle):
    raw = (live.root / path).read_bytes()
    start = raw.index(needle.encode())
    return {"path": path, "sha256": sha256_hex(raw), "byte_start": start,
            "byte_end": start + len(needle.encode())}


def test_gold_hit_scores_one(live):
    score, d = score_diary_recall_case(_case(
        live, {"mode": "literal", "query": "Edit::VSpace#move_left"},
        [_gold(live, "archive/ChangeLog.txt", "Edit::VSpace#move_left")]))
    assert score == 1.0 and d["candidate_recall"] == 1.0 and d["injected_coverage"] == 1.0
    assert d["hard_failures"] == []


def test_miss_scores_zero_and_negative_case(live):
    score, _ = score_diary_recall_case(_case(
        live, {"mode": "literal", "query": "Physiotherapy"},
        [_gold(live, "archive/ChangeLog.txt", "Edit::VSpace#move_left")]))
    assert score == 0.0
    neg, _ = score_diary_recall_case(_case(live, {"mode": "literal", "query": "zeppelin-xyz"}))
    assert neg == 1.0


def test_forbidden_source_is_a_hard_failure(live):
    score, d = score_diary_recall_case(_case(
        live, {"mode": "literal", "query": "Edit::VSpace#move_left"},
        [_gold(live, "archive/ChangeLog.txt", "Edit::VSpace#move_left")],
        forbidden=[str(live.src.uuid)]))
    assert score == 0.0 and d["hard_failures"] == ["forbidden_source_exposure"]


def test_budget_overflow_is_a_hard_failure(live):
    case = _case(live, {"mode": "timeline", "date_from": "2027-03-12", "date_to": "2027-03-12"})
    score, d = score_diary_recall_case(case, budget=200)
    assert "budget_overflow" in d["hard_failures"] and score == 0.0


def test_runner_dispatches_diary_recall(live):
    from evals.runner import run_eval_case
    case = db.EvalCase(name="diary gold", case_type="diary_recall", split="regression",
                       status="active",
                       input=_case(live, {"mode": "literal", "query": "Buffer#test_exception_xxx"}).input,
                       expected={"gold": [_gold(live, "archive/ChangeLog.txt", "Buffer#test_exception_xxx")]},
                       rubric={})
    db.session.add(case)
    db.session.commit()
    try:
        result = run_eval_case(case)
        assert result.passed and result.details["injected_coverage"] == 1.0
    finally:
        runs = [r.eval_run_uuid for r in db.session.query(db.EvalResult).filter_by(eval_case_uuid=case.uuid)]
        db.session.query(db.EvalResult).filter_by(eval_case_uuid=case.uuid).delete()
        db.session.query(db.EvalRun).filter(db.EvalRun.uuid.in_(runs)).delete()
        db.session.delete(case)
        db.session.commit()
