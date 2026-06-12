"""Production-quality monitor for the chat agent.

Reads recent `kind="message"` rows from ChatMessage, scores each with
lightweight validators, and stores the result as an EvalRun whose
`config.source == "production_sample"`. Per-message rows attach to a
shared synthetic EvalCase ("production_sample_message") because the
existing `case_type` / `split` CHECK constraints don't include a
production tier — this keeps FK semantics clean without DDL changes.

This is monitoring, not a runtime guardrail; it never blocks chat.
"""

import argparse
import logging
import sys
from typing import Any

import db
from db import ChatMessage, EvalCase, EvalRun

logger = logging.getLogger(__name__)

PRODUCTION_SAMPLE_CASE_NAME = "production_sample_message"
MAX_REASONABLE_LENGTH = 8000


def _get_or_create_production_sample_case() -> EvalCase:
    """Idempotent: fetch the shared synthetic case, or create it.

    Uses `case_type='chat_reply'` and `split='holdout'` because the
    existing CHECK constraints don't allow a 'production' value. The
    EvalRun.config.source field is the canonical production marker."""
    rows = db.list_eval_cases()  # no per-name lookup helper exists
    for row in rows:
        if row.name == PRODUCTION_SAMPLE_CASE_NAME:
            return row
    return db.create_eval_case(
        name=PRODUCTION_SAMPLE_CASE_NAME,
        case_type="chat_reply",
        split="holdout",
        status="active",
        input={"source": "production_sample"},
        expected={},
        rubric={"validators": ["non_empty", "length_bounded"]},
    )


def _fetch_recent_chat_messages(limit: int) -> list[ChatMessage]:
    """Recent agent `kind='message'` rows, newest first.

    The spec is explicit (WP05 line 87): only AGENT outputs count as a
    production-quality signal. We join ChatUser to filter out human
    inputs — both have `kind='message'` by default in `post_chat_message`."""
    return (
        db.db.session.query(ChatMessage)
        .join(db.ChatUser, db.ChatUser.uuid == ChatMessage.sender_uuid)
        .filter(ChatMessage.kind == "message")
        .filter(db.ChatUser.user_type == "agent")
        .order_by(ChatMessage.created_at.desc())
        .limit(limit)
        .all()
    )


def _score_message(msg: ChatMessage) -> tuple[float, bool, dict[str, Any]]:
    """Lightweight validators: non-empty stripped text, length 1..MAX.

    Returns (score, passed, details). Score is 1.0 on full pass,
    partial otherwise; this is monitoring, not a runtime block."""
    text = (msg.text or "").strip()
    details: dict[str, Any] = {
        "text": msg.text or "",
        "chat_message_uuid": str(msg.uuid),
        "room_uuid": str(msg.room_uuid),
        "sender_uuid": str(msg.sender_uuid),
    }
    if not text:
        details["validator"] = "non_empty"
        return 0.0, False, details
    if len(text) > MAX_REASONABLE_LENGTH:
        details["validator"] = "length_bounded"
        return 0.5, False, details
    return 1.0, True, details


def run_production_sample(
    *,
    limit: int = 50,
    name_prefix: str | None = None,
) -> EvalRun:
    """Sample recent chat outputs and store as an EvalRun.

    `name_prefix` is for test cleanup; production callers leave it None
    and a default prefix is used."""
    case = _get_or_create_production_sample_case()
    messages = _fetch_recent_chat_messages(limit)

    run_name = f"{name_prefix or 'production-sample'}: production_sample"
    run = db.create_eval_run(
        name=run_name,
        agent_role="chat",
        config={"source": "production_sample", "limit": limit},
    )

    passed_count = 0
    total = 0
    score_sum = 0.0
    for msg in messages:
        score, passed, details = _score_message(msg)
        db.create_eval_result(
            eval_run_uuid=run.uuid,
            eval_case_uuid=case.uuid,
            score=score,
            passed=passed,
            details=details,
        )
        total += 1
        score_sum += score
        if passed:
            passed_count += 1

    mean = (score_sum / total) if total else 0.0
    return db.finish_eval_run(
        run.uuid,
        summary={
            "cases": total,
            "passed": passed_count,
            "failed": total - passed_count,
            "mean_score": mean,
            "source": "production_sample",
        },
    )


# --- CLI --------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="eval_monitor",
        description="Sample recent chat outputs and store a quality signal "
                    "as an EvalRun(config.source='production_sample').",
    )
    parser.add_argument(
        "--recent-chat", action="store_true",
        help="Source: recent chat agent messages (currently the only "
             "source supported).",
    )
    parser.add_argument(
        "--limit", type=int, default=50,
        help="Number of recent messages to sample (default: 50).",
    )
    args = parser.parse_args(argv)

    if not args.recent_chat:
        parser.error("--recent-chat is required (no other source today)")

    # Skip db.init_db here for the same lock-conflict reason eval_compare's
    # CLI avoids it: ALTER TABLE migrations need AccessExclusiveLock and can
    # deadlock against any caller holding an open SQLAlchemy session.
    app = db.make_app()
    with app.app_context():
        run = run_production_sample(limit=args.limit)
        summary = run.summary or {}
        print(
            f"production_sample EvalRun {run.uuid}: "
            f"{summary.get('cases', 0)} cases, "
            f"{summary.get('passed', 0)} passed, "
            f"mean {summary.get('mean_score', 0.0):.2f}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(_main())
