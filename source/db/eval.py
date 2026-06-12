"""Eval persistence: eval cases, runs, and results.

Split out of db.py. Holds eval case/run/result CRUD plus the feedback->eval
promotion helper (promote_feedback_to_eval_case, which reads a stored
FeedbackEvent via db_feedback). Re-exported from db for import compatibility.
"""
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from db.models import EvalCase, EvalResult, EvalRun, db
from db.feedback import get_feedback_event


def create_eval_case(
    *,
    name: str,
    case_type: str,
    split: str = "train",
    status: str = "candidate",
    input: dict[str, Any] | None = None,
    expected: dict[str, Any] | None = None,
    rubric: dict[str, Any] | None = None,
    source_feedback_uuid: UUID | None = None,
) -> EvalCase:
    """Insert an eval_case row. JSONB fields default to `{}` when None."""
    ec = EvalCase(
        name=name,
        case_type=case_type,
        split=split,
        status=status,
        input=input if input is not None else {},
        expected=expected if expected is not None else {},
        rubric=rubric if rubric is not None else {},
        source_feedback_uuid=source_feedback_uuid,
    )
    db.session.add(ec)
    db.session.commit()
    return ec


def get_eval_case(eval_case_uuid: UUID) -> "EvalCase | None":
    """Fetch an eval case by uuid, or None if not present."""
    return db.session.query(EvalCase).filter_by(uuid=eval_case_uuid).first()


def list_eval_cases(
    *,
    status: str | None = None,
    split: str | None = None,
    case_type: str | None = None,
    source_feedback_uuid: UUID | None = None,
) -> list[EvalCase]:
    """Return eval cases matching every supplied filter, oldest-first."""
    q = db.session.query(EvalCase)
    if status is not None:
        q = q.filter(EvalCase.status == status)
    if split is not None:
        q = q.filter(EvalCase.split == split)
    if case_type is not None:
        q = q.filter(EvalCase.case_type == case_type)
    if source_feedback_uuid is not None:
        q = q.filter(EvalCase.source_feedback_uuid == source_feedback_uuid)
    return q.order_by(EvalCase.id.asc()).all()


def promote_feedback_to_eval_case(
    feedback_uuid: UUID,
    *,
    split: str | None = None,
    name: str | None = None,
    status: str = "candidate",
) -> EvalCase:
    """Build an EvalCase from a stored FeedbackEvent. Downvotes default to
    `split="regression"`; upvotes default to `split="train"`. The case
    starts as `candidate` so a human can edit it in Flask-Admin before
    flipping to `active`.

    The promoted case carries the feedback's metadata snapshot into a
    deterministic input shape, leaves expected mostly empty (just the
    feedback comment in `notes`), and stamps a default rubric. The
    feedback uuid is preserved on `source_feedback_uuid` for traceability.

    Raises `ValueError` if `feedback_uuid` doesn't exist."""
    fb = get_feedback_event(feedback_uuid)
    if fb is None:
        raise ValueError(f"feedback not found: {feedback_uuid}")

    if split is None:
        split = "regression" if fb.rating == "downvote" else "train"

    meta = fb.metadata_ or {}

    room_history: list[dict[str, Any]] = []
    prev_human_text = meta.get("prev_human_message_text")
    if prev_human_text:
        room_history.append({
            "sender_type": "human",
            "text": prev_human_text,
        })

    input_data: dict[str, Any] = {
        "room_history": room_history,
        "current_message": prev_human_text or "",
        "agent_role": "chat",
        "rated_message_text": meta.get("rated_message_text"),
        "rated_message_content_type": meta.get("rated_message_content_type"),
        "debug_memory": meta.get("debug_memory"),
        "debug_query": meta.get("debug_query"),
    }
    expected_data: dict[str, Any] = {
        "must_include": [],
        "must_not_include": [],
        "expected_memories": [],
        "forbidden_memories": [],
        "notes": fb.comment or "",
    }
    rubric_data: dict[str, Any] = {
        "criteria": [
            {"name": "answers_current_message", "weight": 0.4},
            {"name": "uses_relevant_memory", "weight": 0.3},
            {"name": "avoids_irrelevant_private_memory", "weight": 0.3},
        ],
        "threshold": 0.7,
    }

    if name is None:
        rated_snippet = (meta.get("rated_message_text") or "(no text)")[:60]
        name = f"Feedback {fb.rating}: {rated_snippet}"

    return create_eval_case(
        name=name,
        case_type="chat_reply",
        split=split,
        status=status,
        input=input_data,
        expected=expected_data,
        rubric=rubric_data,
        source_feedback_uuid=fb.uuid,
    )


def create_eval_run(
    *,
    name: str,
    agent_role: str,
    config: dict[str, Any] | None = None,
) -> EvalRun:
    """Insert an EvalRun row. `started_at` is auto-stamped; `finished_at`
    stays NULL until `finish_eval_run` is called."""
    run = EvalRun(
        name=name,
        agent_role=agent_role,
        config=config if config is not None else {},
        summary={},
    )
    db.session.add(run)
    db.session.commit()
    return run


def finish_eval_run(run_uuid: UUID, *, summary: dict[str, Any]) -> EvalRun:
    """Mark an EvalRun complete: stamp `finished_at` and store the
    summary blob. Raises ValueError if the run isn't found."""
    run = db.session.query(EvalRun).filter_by(uuid=run_uuid).first()
    if run is None:
        raise ValueError(f"eval run not found: {run_uuid}")
    run.finished_at = datetime.now(UTC)
    run.summary = summary
    db.session.commit()
    return run


def set_baseline_eval_run(run_uuid: UUID, *, is_baseline: bool) -> EvalRun:
    """Flip the is_baseline flag on an EvalRun. Used by the regression
    gate to mark a known-good run that future candidates are compared
    against. Raises ValueError if the run doesn't exist."""
    run = db.session.query(EvalRun).filter_by(uuid=run_uuid).first()
    if run is None:
        raise ValueError(f"eval run not found: {run_uuid}")
    run.is_baseline = is_baseline
    db.session.commit()
    return run


def get_eval_run(run_uuid: UUID) -> "EvalRun | None":
    return db.session.query(EvalRun).filter_by(uuid=run_uuid).first()


def list_eval_runs(*, limit: int | None = None) -> list[EvalRun]:
    """Return EvalRuns newest-first (so the latest run is at index 0)."""
    q = db.session.query(EvalRun).order_by(EvalRun.id.desc())
    if limit is not None:
        q = q.limit(limit)
    return q.all()


def create_eval_result(
    *,
    eval_run_uuid: UUID,
    eval_case_uuid: UUID,
    score: float,
    passed: bool,
    details: dict[str, Any] | None = None,
) -> EvalResult:
    """Insert an EvalResult row. `score` is clamped to [0.0, 1.0] by the
    DB CheckConstraint."""
    result = EvalResult(
        eval_run_uuid=eval_run_uuid,
        eval_case_uuid=eval_case_uuid,
        score=score,
        passed=passed,
        details=details if details is not None else {},
    )
    db.session.add(result)
    db.session.commit()
    return result


def list_eval_results_for_run(run_uuid: UUID) -> list[EvalResult]:
    """All results for one run, ordered by insertion id ascending."""
    return (
        db.session.query(EvalResult)
        .filter(EvalResult.eval_run_uuid == run_uuid)
        .order_by(EvalResult.id.asc())
        .all()
    )
