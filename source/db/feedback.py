"""Feedback and retrieval-telemetry persistence.

Split out of db.py. Holds chat-reply feedback events (and their metadata
snapshot), the downvote->retrieval linkage, and retrieval-telemetry events.
These two concerns are kept together because link_downvote_to_retrieval_targets
ties feedback to retrieval. Re-exported from db for import compatibility.
"""
import json
import logging
import re
from typing import Any
from uuid import UUID

import sqlalchemy as sa

from db.models import ChatMessage, ChatUser, FeedbackEvent, RetrievalEvent, db

logger = logging.getLogger(__name__)


def _build_feedback_metadata(
    room_uuid: UUID, message_uuid: UUID,
) -> dict[str, Any]:
    """Snapshot the chat context around a rated message: the rated row's
    text/content_type, the latest prior human message, and the latest
    prior `debug-memory` / `debug-query` payloads (or None when absent).
    Called by `create_feedback_event` at the moment of capture."""
    rated = (
        db.session.query(ChatMessage)
        .filter(ChatMessage.uuid == message_uuid)
        .first()
    )
    if rated is None:
        raise ValueError(f"chat message not found: {message_uuid}")

    prev_human = (
        db.session.query(ChatMessage)
        .filter(
            ChatMessage.room_uuid == room_uuid,
            ChatMessage.id < rated.id,
        )
        .join(
            ChatUser,
            ChatUser.uuid == ChatMessage.sender_uuid,
        )
        .filter(ChatUser.user_type == "human")
        .order_by(ChatMessage.id.desc())
        .limit(1)
        .first()
    )
    prev_human_id = prev_human.id if prev_human is not None else None

    def _latest_kind(kind: str) -> tuple[ChatMessage | None, dict[str, Any] | None]:
        """Scope the diagnostic snapshot to the rated turn:
          - same room as the rated reply
          - same agent (sender_uuid) as the rated reply
          - id < rated.id (still strictly prior)
          - id > prev_human.id when a prior human message exists (this
            is what closes WP07 Finding 3 — a downvote on turn N must
            not pick up turn N-1's debug-memory).
        """
        q = (
            db.session.query(ChatMessage)
            .filter(
                ChatMessage.room_uuid == room_uuid,
                ChatMessage.kind == kind,
                ChatMessage.id < rated.id,
                ChatMessage.sender_uuid == rated.sender_uuid,
            )
        )
        if prev_human_id is not None:
            q = q.filter(ChatMessage.id > prev_human_id)
        row = q.order_by(ChatMessage.id.desc()).limit(1).first()
        if row is None:
            return None, None
        try:
            payload = json.loads(row.text)
        except (ValueError, TypeError):
            payload = {"_raw": row.text}
        return row, payload

    debug_memory_row, debug_memory_payload = _latest_kind("debug-memory")
    debug_query_row, debug_query_payload = _latest_kind("debug-query")

    return {
        "rated_message_text": rated.text,
        "rated_message_content_type": rated.content_type,
        "prev_human_message_uuid": str(prev_human.uuid) if prev_human else None,
        "prev_human_message_text": prev_human.text if prev_human else None,
        "debug_memory": debug_memory_payload,
        "debug_memory_message_uuid": (
            str(debug_memory_row.uuid) if debug_memory_row else None
        ),
        "debug_query": debug_query_payload,
        "debug_query_message_uuid": (
            str(debug_query_row.uuid) if debug_query_row else None
        ),
    }


def create_feedback_event(
    *,
    room_uuid: UUID,
    message_uuid: UUID,
    agent_uuid: UUID,
    rating: str,
    comment: str | None = None,
    created_by_uuid: UUID | None = None,
) -> FeedbackEvent:
    """Persist a feedback row, building the metadata snapshot from the
    surrounding chat context. Raises sa.exc.IntegrityError if `rating`
    isn't one of {upvote, downvote}."""
    metadata = _build_feedback_metadata(room_uuid, message_uuid)
    fb = FeedbackEvent(
        room_uuid=room_uuid,
        message_uuid=message_uuid,
        agent_uuid=agent_uuid,
        rating=rating,
        comment=comment,
        created_by_uuid=created_by_uuid,
        metadata_=metadata,
    )
    db.session.add(fb)
    db.session.commit()
    return fb


def link_downvote_to_retrieval_targets(feedback_event_uuid: UUID) -> None:
    """For a downvote FeedbackEvent, parse the snapshotted debug-memory
    and debug-query payloads out of its metadata and write `downvoted`
    RetrievalEvent rows for the referenced targets.

    Telemetry-only — best-effort and resilient: malformed payloads,
    missing keys, and bad target ids are skipped silently. Each row is
    committed individually so a later section's failure cannot discard
    rows already written by an earlier section.

    Non-idempotent: a duplicate POST writes duplicate downvoted rows.
    Caller is expected to invoke once per FeedbackEvent."""
    fe = db.session.query(FeedbackEvent).filter_by(
        uuid=feedback_event_uuid
    ).first()
    if fe is None or fe.rating != "downvote":
        return
    md = fe.metadata_ or {}
    common = {
        # cap snapshot text — keep retrieval_event rows small
        "query": (md.get("rated_message_text") or "")[:1000],
        "room_uuid": fe.room_uuid,
        "agent_uuid": fe.agent_uuid,
        # journal_id intentionally None — FeedbackEvent does not carry one
        "journal_id": None,
        "source": "chat_feedback",
        "filter_label": None,
        "metadata": {"feedback_event_uuid": str(fe.uuid)},
        "commit": True,
    }

    def _payload(snapshot: Any) -> dict | None:
        """`_build_feedback_metadata` stores the already-parsed JSON body
        directly (or `{"_raw": text}` when the body wasn't JSON). Anything
        that isn't a dict, or that's our `_raw` fallback, is unusable here."""
        if not isinstance(snapshot, dict):
            return None
        if "_raw" in snapshot:
            return None
        return snapshot

    # --- debug-memory: extract memory_uuids ---
    dbg_mem = _payload(md.get("debug_memory"))
    if dbg_mem:
        try:
            mems = dbg_mem.get("memories") or []
            for m in mems:
                if not isinstance(m, dict):
                    continue
                target_id = m.get("memory_uuid")
                if not target_id:
                    continue
                record_retrieval_event(
                    target_type="memory_claim",
                    target_id=str(target_id),
                    stage="downvoted",
                    **common,
                )
        except Exception:
            logger.exception(
                "downvote telemetry: failed to write debug-memory "
                "events for feedback_event=%s; skipping",
                fe.uuid,
            )
            db.session.rollback()

    # --- debug-query / debug-filter: extract qa_ids ---
    dbg_q = _payload(md.get("debug_query"))
    if dbg_q:
        try:
            qa_ids: list[str] = []
            filter_kept = dbg_q.get("filter_kept") or []
            if isinstance(filter_kept, list):
                qa_ids.extend(str(x) for x in filter_kept if x)
            match = dbg_q.get("match") or {}
            if isinstance(match, dict) and match.get("qa_id"):
                qa_ids.append(str(match["qa_id"]))
            for qa_id in set(qa_ids):
                record_retrieval_event(
                    target_type="qa_entry",
                    target_id=qa_id,
                    stage="downvoted",
                    **common,
                )
        except Exception:
            logger.exception(
                "downvote telemetry: failed to write debug-query "
                "events for feedback_event=%s; skipping",
                fe.uuid,
            )
            db.session.rollback()


def get_feedback_event(feedback_uuid: UUID) -> "FeedbackEvent | None":
    """Fetch a feedback event by uuid, or None if not present."""
    return db.session.query(FeedbackEvent).filter_by(uuid=feedback_uuid).first()


def list_feedback_events(
    *,
    room_uuid: UUID | None = None,
    agent_uuid: UUID | None = None,
    rating: str | None = None,
) -> list[FeedbackEvent]:
    """Return feedback events matching every supplied filter (AND-ed),
    ordered oldest-first by id."""
    q = db.session.query(FeedbackEvent)
    if room_uuid is not None:
        q = q.filter(FeedbackEvent.room_uuid == room_uuid)
    if agent_uuid is not None:
        q = q.filter(FeedbackEvent.agent_uuid == agent_uuid)
    if rating is not None:
        q = q.filter(FeedbackEvent.rating == rating)
    return q.order_by(FeedbackEvent.id.asc()).all()


def record_retrieval_event(
    *,
    target_type: str,
    target_id: str,
    stage: str,
    query: str | None = None,
    room_uuid: UUID | None = None,
    agent_uuid: UUID | None = None,
    journal_id: int | None = None,
    source: str | None = None,
    retrieval_rank: int | None = None,
    retrieval_score: float | None = None,
    filter_label: str | None = None,
    metadata: dict | None = None,
    commit: bool = True,
) -> RetrievalEvent:
    """Append one retrieval-pipeline event. Event-row source of truth —
    do not mutate existing rows.

    The `metadata` kwarg is stored as `event.metadata_` on the model
    (trailing underscore avoids the SQLAlchemy `MetaData` collision;
    the SQL column itself is named `metadata`).

    `commit=True` (default) commits immediately. Pass `commit=False`
    to batch many events in a single transaction — the row is flushed
    (so the event has a uuid/id and is visible in-session) but the
    caller is responsible for `db.session.commit()` at the end."""
    event = RetrievalEvent(
        target_type=target_type,
        target_id=target_id,
        stage=stage,
        query=query,
        room_uuid=room_uuid,
        agent_uuid=agent_uuid,
        journal_id=journal_id,
        source=source,
        retrieval_rank=retrieval_rank,
        retrieval_score=retrieval_score,
        filter_label=filter_label,
        metadata_=metadata or {},
    )
    db.session.add(event)
    if commit:
        db.session.commit()
    else:
        db.session.flush()
    return event


def list_retrieval_events(
    *,
    target_type: str | None = None,
    target_id: str | None = None,
    stage: str | None = None,
    source: str | None = None,
    limit: int | None = 1000,
) -> list[RetrievalEvent]:
    """Filter retrieval_event rows. Optional filters compose; ordering
    is newest first (descending created_at). Default `limit=1000`
    bounds the result set so a typo doesn't pull every row on a busy
    DB. Pass `limit=None` to opt into unbounded fetching."""
    q = db.session.query(RetrievalEvent)
    if target_type is not None:
        q = q.filter(RetrievalEvent.target_type == target_type)
    if target_id is not None:
        q = q.filter(RetrievalEvent.target_id == target_id)
    if stage is not None:
        q = q.filter(RetrievalEvent.stage == stage)
    if source is not None:
        q = q.filter(RetrievalEvent.source == source)
    q = q.order_by(RetrievalEvent.created_at.desc())
    if limit is not None:
        q = q.limit(limit)
    return q.all()
