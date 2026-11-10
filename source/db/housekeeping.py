"""Housekeeping persistence: the jobs that run after a turn, and their attempts.

The `housekeeping` role (agents/housekeeping.py) does the work. This module is
its queue and its record: which jobs exist, which is next, what each attempt
did, and whether a turn is in progress. The Housekeeping page reads the same
rows. Design: notes/housekeeping-design.md.
"""
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from db.models import (
    AssistantRun,
    ChatMessage,
    ChatUser,
    HousekeepingAttempt,
    HousekeepingJob,
    Inbox,
    Journal,
    LlmCall,
    RoomChunk,
    db,
)

logger = logging.getLogger(__name__)

__all__ = [
    "HOUSEKEEPING_JOB_TYPES",
    "JOB_CALLERS",
    "ROOM_CHUNK_JOB",
    "RUN_SUMMARY_JOB",
    "attach_reply_signals",
    "chunk_subject",
    "counted_room_messages",
    "due_room_chunks",
    "ensure_room_chunk_jobs",
    "list_room_chunk_series",
    "list_room_chunks",
    "parse_chunk_subject",
    "store_room_chunk",
    "bump_run_housekeeping",
    "checkpoint_housekeeping_attempt",
    "ensure_housekeeping_job",
    "finish_housekeeping_attempt",
    "get_housekeeping_job",
    "housekeeping_attempt_call",
    "housekeeping_now",
    "housekeeping_revision",
    "housekeeping_room_names",
    "list_housekeeping_attempts",
    "list_housekeeping_jobs",
    "list_run_housekeeping_jobs",
    "next_housekeeping_job",
    "recover_running_housekeeping_jobs",
    "request_run_housekeeping",
    "requeue_housekeeping_job",
    "run_housekeeping_flags",
    "settle_housekeeping_job",
    "start_housekeeping_attempt",
    "sweep_due_housekeeping_jobs",
    "turn_activity",
]

RUN_SUMMARY_JOB = "run_summary"
ROOM_CHUNK_JOB = "room_chunk_summary"

#: job type -> (tier, label). Tier 1 is read by the next turn; tier 2 is
#: generative work that is read eventually. The label is what the page prints.
HOUSEKEEPING_JOB_TYPES: dict[str, tuple[int, str]] = {
    RUN_SUMMARY_JOB: (2, "run summary"),
    ROOM_CHUNK_JOB: (2, "room chunk summary"),
}

#: The /activity caller tag of each job type's model call. It is how an
#: attempt finds the stored call it made.
JOB_CALLERS: dict[str, str] = {
    RUN_SUMMARY_JOB: "assistant.run_summarizer",
    ROOM_CHUNK_JOB: "assistant.room_summarizer",
}

#: How far back a `running` run or a `processing` journal row still counts as
#: a turn in progress. Both are settled when their worker ends, so an older
#: one is a row a dead process left behind, and it must not hold housekeeping
#: back for ever.
ACTIVITY_HORIZON = timedelta(minutes=30)

#: How far back the sweep looks for finished runs with no summary, and how
#: many jobs one pass may add. The sweep exists to find an enqueue that was
#: lost, which is recent and rare, so both bounds are small.
SWEEP_WINDOW = timedelta(hours=24)
SWEEP_LIMIT = 10

_TERMINAL_RUN_STATUSES = ("finished", "stopped", "failed", "killed")
_PREVIEW_LIMIT = 20_000


def _now() -> datetime:
    return datetime.now(UTC)


# --- is a turn in progress ---------------------------------------------------

def turn_activity(*, session: Session | None = None) -> bool:
    """Whether anything other than housekeeping is working or about to.

    True when an agent role has work in its inbox, when an assistant run is
    active, or when a worker has a journal row in `processing`. Housekeeping
    yields to all of it: the operator is waiting on that work, and nobody is
    waiting on this.

    `session` lets the watcher thread ask on a session of its own; the scoped
    `db.session` must never be shared across threads.
    """
    from agents.config import HOUSEKEEPING_UUID, agent_config

    s = session or db.session
    others = [p["uuid"] for p in agent_config.values()
              if p["uuid"] != HOUSEKEEPING_UUID]
    if s.query(Inbox.id).filter(Inbox.agent_uuid.in_(others)).first():
        return True
    cutoff = _now() - ACTIVITY_HORIZON
    if (s.query(AssistantRun.uuid)
            .filter(AssistantRun.status.in_(("running", "stopping")),
                    AssistantRun.started_at >= cutoff).first()):
        return True
    return (s.query(Journal.id)
            .filter(Journal.state == "processing",
                    Journal.agent_uuid.in_(others),
                    Journal.started_at >= cutoff).first()) is not None


# --- creating and finding jobs ----------------------------------------------

def ensure_housekeeping_job(
    job_type: str, *, run_uuid: UUID | None = None,
    room_uuid: UUID | None = None, subject: str = "", job_version: int = 1,
) -> tuple[HousekeepingJob, bool]:
    """The job for this work, created if it does not exist. Returns
    `(job, created)`. Asking twice for the same work gives the same row, so a
    duplicate enqueue never runs anything twice."""
    key = dict(job_type=job_type, run_uuid=run_uuid, subject=subject,
               job_version=job_version)
    existing = db.session.query(HousekeepingJob).filter_by(**key).first()
    if existing is not None:
        return existing, False
    tier = HOUSEKEEPING_JOB_TYPES.get(job_type, (2, job_type))[0]
    job = HousekeepingJob(tier=tier, room_uuid=room_uuid, **key)
    db.session.add(job)
    try:
        db.session.commit()
    except IntegrityError:
        # Another process created it between the read and the insert.
        db.session.rollback()
        return db.session.query(HousekeepingJob).filter_by(**key).one(), False
    return job, True


def request_run_housekeeping(
    run_uuid: UUID, *, room_uuid: UUID | None = None,
    request_message: str | None = None, force: bool = False,
) -> None:
    """Tell the housekeeping role that a run has ended. One inbox item per
    run; the worker expands it into that run's jobs. `force` runs the jobs
    again even when they already have a result (the Refresh summary menu
    item)."""
    from agents.config import HOUSEKEEPING_UUID

    payload: dict[str, Any] = {"run_uuid": str(run_uuid)}
    if room_uuid is not None:
        payload["room_uuid"] = str(room_uuid)
    if request_message:
        payload["request_message"] = str(request_message)
    if force:
        payload["force"] = True
    # Through the facade, as every other producer enqueues.
    import db as facade

    facade.enqueue(HOUSEKEEPING_UUID, payload)


def requeue_housekeeping_job(job: HousekeepingJob) -> HousekeepingJob:
    """Put a job back in the queue with its failure count cleared. Its earlier
    attempts stay; the next one is appended."""
    job.state = "waiting"
    job.outcome = None
    job.deferred_reason = None
    job.retries = 0
    job.finished_at = None
    db.session.add(job)
    db.session.commit()
    return job


def next_housekeeping_job() -> HousekeepingJob | None:
    """The job to run next: every tier 1 job before any tier 2 job; within a
    tier, a job whose run's page was opened first, then the oldest."""
    return (db.session.query(HousekeepingJob)
            .filter(HousekeepingJob.state.in_(("waiting", "deferred")))
            .order_by(HousekeepingJob.tier.asc(),
                      HousekeepingJob.bumped_at.desc().nulls_last(),
                      HousekeepingJob.created_at.asc())
            .first())


def bump_run_housekeeping(run_uuid: UUID) -> int:
    """Move a run's unfinished jobs to the front of their tier. Called when
    the run's page is opened. Returns how many jobs moved."""
    rows = (db.session.query(HousekeepingJob)
            .filter(HousekeepingJob.run_uuid == run_uuid,
                    HousekeepingJob.state.in_(("waiting", "deferred")),
                    HousekeepingJob.bumped_at.is_(None))
            .all())
    for job in rows:
        job.bumped_at = _now()
    if rows:
        db.session.commit()
    return len(rows)


def sweep_due_housekeeping_jobs() -> int:
    """Create the jobs that are due and have no row: a run that ended in the
    last day with no summary and no summary job. The inbox item is a hint and
    the records say what is due, so an enqueue lost in a crash delays a
    summary and never loses it. Returns how many jobs were created."""
    has_job = (sa.select(HousekeepingJob.uuid)
               .where(HousekeepingJob.run_uuid == AssistantRun.uuid,
                      HousekeepingJob.job_type == RUN_SUMMARY_JOB)
               .exists())
    runs = (db.session.query(AssistantRun)
            .filter(AssistantRun.status.in_(_TERMINAL_RUN_STATUSES),
                    AssistantRun.finished_at >= _now() - SWEEP_WINDOW,
                    AssistantRun.summary.is_(None),
                    ~has_job)
            .order_by(AssistantRun.finished_at.desc())
            .limit(SWEEP_LIMIT)
            .all())
    created = 0
    for run in runs:
        _job, new = ensure_housekeeping_job(
            RUN_SUMMARY_JOB, run_uuid=run.uuid, room_uuid=run.room_uuid)
        created += int(new)
    return created


def recover_running_housekeeping_jobs(reason: str) -> int:
    """Settle what a dead worker left behind: an attempt with no result is
    closed as failed, and its job goes back to waiting. There is one worker,
    so when it starts nothing can truly be running."""
    jobs = (db.session.query(HousekeepingJob)
            .filter(HousekeepingJob.state == "running").all())
    for job in jobs:
        for attempt in (db.session.query(HousekeepingAttempt)
                        .filter(HousekeepingAttempt.job_uuid == job.uuid,
                                HousekeepingAttempt.result.is_(None)).all()):
            attempt.result = "failed"
            attempt.error = reason
            attempt.ended_at = _now()
        job.state = "waiting"
    if jobs:
        db.session.commit()
    return len(jobs)


# --- attempts ----------------------------------------------------------------

def start_housekeeping_attempt(job: HousekeepingJob) -> HousekeepingAttempt:
    job.state = "running"
    job.deferred_reason = None
    attempt = HousekeepingAttempt(job_uuid=job.uuid)
    db.session.add_all([job, attempt])
    db.session.commit()
    return attempt


def checkpoint_housekeeping_attempt(
    attempt_uuid: UUID, *, model: str | None = None,
    response_preview: str | None = None,
) -> None:
    """Record what a running attempt is doing: its model, and the reply so
    far. The job's page shows both while the call streams."""
    values: dict[str, Any] = {}
    if model is not None:
        values["model"] = model
    if response_preview is not None:
        values["response_preview"] = response_preview[-_PREVIEW_LIMIT:]
    if not values:
        return
    db.session.execute(
        sa.update(HousekeepingAttempt)
        .where(HousekeepingAttempt.uuid == attempt_uuid).values(**values))
    db.session.commit()


def housekeeping_attempt_call(
    job: HousekeepingJob, attempt: HousekeepingAttempt,
) -> LlmCall | None:
    """The stored model call an attempt made: the one its job type's caller
    recorded while the attempt ran. None for an attempt that made no call."""
    if attempt.llm_call_uuid is not None:
        return db.session.get(LlmCall, attempt.llm_call_uuid)
    caller = JOB_CALLERS.get(job.job_type)
    if caller is None:
        return None
    # One worker runs one job at a time, so a call by this job type's caller
    # inside the attempt's own seconds is the attempt's call.
    slack = timedelta(seconds=2)
    q = (db.session.query(LlmCall)
         .filter(LlmCall.caller == caller,
                 LlmCall.started_at >= attempt.started_at - slack))
    if job.run_uuid is not None:
        q = q.filter(LlmCall.run_uuid == job.run_uuid)
    if attempt.ended_at is not None:
        q = q.filter(LlmCall.started_at <= attempt.ended_at + slack)
    return q.order_by(LlmCall.started_at.desc()).first()


def finish_housekeeping_attempt(
    job: HousekeepingJob, attempt: HousekeepingAttempt, result: str, *,
    error: str | None = None,
) -> HousekeepingAttempt:
    attempt.result = result
    attempt.error = error
    attempt.ended_at = _now()
    attempt.model_ms = int(
        (attempt.ended_at - attempt.started_at).total_seconds() * 1000)
    try:
        call = housekeeping_attempt_call(job, attempt)
    except Exception:
        # The link is a convenience for the page; never fail a job over it.
        logger.exception("housekeeping: could not link attempt to its call")
        db.session.rollback()
        call = None
    if call is not None:
        attempt.llm_call_uuid = call.uuid
        attempt.model = attempt.model or call.model
    db.session.add(attempt)
    db.session.commit()
    return attempt


def settle_housekeeping_job(
    job: HousekeepingJob, state: str, *, outcome: str | None = None,
    deferred_reason: str | None = None, retried: bool = False,
) -> HousekeepingJob:
    job.state = state
    job.outcome = outcome
    job.deferred_reason = deferred_reason
    if retried:
        job.retries += 1
    job.finished_at = _now() if state == "final" else None
    db.session.add(job)
    db.session.commit()
    return job


# --- reading, for the pages --------------------------------------------------

def get_housekeeping_job(job_uuid: UUID) -> HousekeepingJob | None:
    return db.session.get(HousekeepingJob, job_uuid)


def list_housekeeping_attempts(job_uuid: UUID) -> list[HousekeepingAttempt]:
    return (db.session.query(HousekeepingAttempt)
            .filter(HousekeepingAttempt.job_uuid == job_uuid)
            .order_by(HousekeepingAttempt.started_at.asc()).all())


def list_run_housekeeping_jobs(run_uuid: UUID) -> list[HousekeepingJob]:
    return (db.session.query(HousekeepingJob)
            .filter(HousekeepingJob.run_uuid == run_uuid)
            .order_by(HousekeepingJob.created_at.asc()).all())


def _outcome_filter(query, outcome: str):
    """`failed` and `done` are outcomes of a final job, so the page's one
    filter reads state and outcome together."""
    if outcome in ("waiting", "running", "deferred"):
        return query.filter(HousekeepingJob.state == outcome)
    if outcome == "failed":
        return query.filter(HousekeepingJob.state == "final",
                            HousekeepingJob.outcome.like("failed%"))
    if outcome == "done":
        return query.filter(HousekeepingJob.state == "final",
                            ~HousekeepingJob.outcome.like("failed%"))
    return query


def list_housekeeping_jobs(
    *, outcome: str = "all", job_type: str = "all",
    room_uuid: UUID | None = None, limit: int = 50, offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """One page of the log, newest first, with each job's attempts summed:
    `(rows, total)`. A row is `{job, attempts, model_ms, model}`."""
    q = _outcome_filter(db.session.query(HousekeepingJob), outcome)
    if job_type != "all":
        q = q.filter(HousekeepingJob.job_type == job_type)
    if room_uuid is not None:
        q = q.filter(HousekeepingJob.room_uuid == room_uuid)
    total = q.count()
    jobs = (q.order_by(HousekeepingJob.created_at.desc())
            .limit(limit).offset(offset).all())
    by_job: dict[UUID, list[HousekeepingAttempt]] = {}
    if jobs:
        for attempt in (db.session.query(HousekeepingAttempt)
                        .filter(HousekeepingAttempt.job_uuid.in_(
                            [j.uuid for j in jobs]))
                        .order_by(HousekeepingAttempt.started_at.asc()).all()):
            by_job.setdefault(attempt.job_uuid, []).append(attempt)
    rows = []
    for job in jobs:
        attempts = by_job.get(job.uuid, [])
        rows.append({
            "job": job,
            "attempts": len(attempts),
            "model_ms": sum(a.model_ms or 0 for a in attempts),
            "model": next((a.model for a in reversed(attempts) if a.model), None),
        })
    return rows, total


def housekeeping_room_names(room_uuids: list[UUID]) -> dict[UUID, str]:
    from db.models import Chatroom

    if not room_uuids:
        return {}
    rows = (db.session.query(Chatroom.uuid, Chatroom.name)
            .filter(Chatroom.uuid.in_(room_uuids)).all())
    return {r[0]: r[1] for r in rows}


def housekeeping_now() -> dict[str, Any]:
    """The state at this moment, for the top of the page: the running job and
    its attempt, how many jobs wait per tier, and whether a turn is holding
    housekeeping back."""
    running = (db.session.query(HousekeepingJob)
               .filter(HousekeepingJob.state == "running")
               .order_by(HousekeepingJob.created_at.asc()).first())
    attempt = None
    if running is not None:
        attempt = (db.session.query(HousekeepingAttempt)
                   .filter(HousekeepingAttempt.job_uuid == running.uuid,
                           HousekeepingAttempt.result.is_(None))
                   .order_by(HousekeepingAttempt.started_at.desc()).first())
    waiting = {
        tier: n for tier, n in
        db.session.query(HousekeepingJob.tier, sa.func.count())
        .filter(HousekeepingJob.state.in_(("waiting", "deferred")))
        .group_by(HousekeepingJob.tier).all()}
    failed = (db.session.query(sa.func.count())
              .select_from(HousekeepingJob)
              .filter(HousekeepingJob.state == "final",
                      HousekeepingJob.outcome.like("failed%")).scalar())
    return {
        "running": running,
        "attempt": attempt,
        "waiting": {1: waiting.get(1, 0), 2: waiting.get(2, 0)},
        "failed": failed or 0,
        "turn_active": turn_activity(),
    }


def housekeeping_revision() -> str:
    """A value that changes whenever the log would render differently. The
    pages poll it and reload their content when it moves."""
    jobs = db.session.query(
        sa.func.count(), sa.func.max(HousekeepingJob.finished_at),
        sa.func.max(HousekeepingJob.bumped_at),
    ).select_from(HousekeepingJob).one()
    states = (db.session.query(HousekeepingJob.state, sa.func.count())
              .group_by(HousekeepingJob.state).all())
    attempts = db.session.query(
        sa.func.count(), sa.func.max(HousekeepingAttempt.ended_at),
        sa.func.sum(sa.func.length(
            sa.func.coalesce(HousekeepingAttempt.response_preview, ""))),
    ).select_from(HousekeepingAttempt).one()
    chunks = db.session.query(sa.func.count()).select_from(RoomChunk).scalar()
    return "|".join(str(v) for v in (
        *jobs, sorted(states), *attempts, chunks, int(turn_activity())))


def run_housekeeping_flags(run_uuids: list[UUID]) -> dict[UUID, str]:
    """For the run list: `failed` for a run with a failed job, else `waiting`
    for one with unfinished jobs. A run whose jobs are all done is absent."""
    if not run_uuids:
        return {}
    rows = (db.session.query(HousekeepingJob.run_uuid, HousekeepingJob.state,
                             HousekeepingJob.outcome)
            .filter(HousekeepingJob.run_uuid.in_(run_uuids)).all())
    out: dict[UUID, str] = {}
    for run_uuid, state, outcome in rows:
        if state == "final" and (outcome or "").startswith("failed"):
            out[run_uuid] = "failed"
        elif state != "final" and out.get(run_uuid) != "failed":
            out[run_uuid] = "waiting"
    return out


# --- room chunks -------------------------------------------------------------

def counted_room_messages(room_uuid: UUID, *, after_id: int = 0,
                          first_id: int | None = None,
                          last_id: int | None = None) -> list[dict[str, Any]]:
    """The messages chunks count, in order: real messages that have finished
    streaming. Progress rows are reaped when the reply posts, and thinking
    rows are not part of the conversation, so neither counts. Each is
    `{id, uuid, sender, text, created_at, run_uuid}`, with `sender` the user
    type (`human` or `agent`) and `run_uuid` the assistant run a reply came
    from, when it came from one."""
    q = (db.session.query(ChatMessage, ChatUser.user_type)
         .join(ChatUser, ChatUser.uuid == ChatMessage.sender_uuid)
         .filter(ChatMessage.room_uuid == room_uuid,
                 ChatMessage.kind == "message",
                 ChatMessage.streaming.is_(False),
                 ChatMessage.id > after_id))
    if first_id is not None:
        q = q.filter(ChatMessage.id >= first_id)
    if last_id is not None:
        q = q.filter(ChatMessage.id <= last_id)
    return [{"id": m.id, "uuid": m.uuid, "sender": user_type, "text": m.text,
             "created_at": m.created_at,
             "run_uuid": (m.meta or {}).get("assistant_run_uuid")}
            for m, user_type in q.order_by(ChatMessage.id.asc()).all()]


def attach_reply_signals(room_uuid: UUID,
                         messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add `signals` to each assistant message: what the records say about
    that reply. Returns the same list.

    - `run`: how the reply's run ended. The run summary's outcome
      (`resolved`, `partial`, `failed`) when there is one, else the run's
      status.
    - `criteria`: `met` when the reply audit's last verdict was to send the
      reply, `not met` when it was still asking for a revision. `revisions`
      counts the times it sent the reply back. Absent when no audit ran.
    - `vote`: the human's latest vote on the message, `upvote` or
      `downvote`, with `vote_comment` when they wrote one.

    A message with no run and no vote gets `None`."""
    import json as _json

    from db.models import AssistantStep, FeedbackEvent

    run_uuids = {UUID(m["run_uuid"]) for m in messages if m.get("run_uuid")}
    runs = {}
    audits: dict[UUID, list[str]] = {}
    if run_uuids:
        runs = {r.uuid: r for r in db.session.query(AssistantRun)
                .filter(AssistantRun.uuid.in_(run_uuids)).all()}
        for run_uuid, preview in (
                db.session.query(AssistantStep.run_uuid, AssistantStep.observation_preview)
                .filter(AssistantStep.run_uuid.in_(run_uuids),
                        AssistantStep.action == "reply_audit")
                .order_by(AssistantStep.id.asc()).all()):
            try:
                verdict = (_json.loads(preview or "{}") or {}).get("verdict")
            except ValueError:
                verdict = None
            if verdict:
                audits.setdefault(run_uuid, []).append(verdict)
    votes = {v.message_uuid: v for v in db.session.query(FeedbackEvent)
             .filter(FeedbackEvent.room_uuid == room_uuid)
             .order_by(FeedbackEvent.id.asc()).all()}   # the latest vote wins
    for m in messages:
        signals: dict[str, Any] = {}
        run = runs.get(UUID(m["run_uuid"])) if m.get("run_uuid") else None
        if run is not None:
            signals["run"] = (run.summary or {}).get("outcome") or run.status
            verdicts = audits.get(run.uuid)
            if verdicts:
                signals["criteria"] = "met" if verdicts[-1] == "send" else "not met"
                signals["revisions"] = sum(1 for v in verdicts if v == "revise")
        vote = votes.get(m["uuid"])
        if vote is not None and m["sender"] != "human":
            signals["vote"] = vote.rating
            if vote.comment:
                signals["vote_comment"] = vote.comment
        m["signals"] = signals or None
    return messages


def chunk_subject(size: int, overlap: int, version: int, seq: int,
                  first_id: int, last_id: int) -> str:
    """A chunk job's subject: its series, its place in it, and its edges."""
    return f"{size}/{overlap}/v{version}/{seq}:{first_id}-{last_id}"


def parse_chunk_subject(subject: str) -> dict[str, int] | None:
    """The fields of a chunk job's subject. A subject with no version is from
    the first prompt."""
    try:
        series, edges = subject.split(":")
        fields = series.split("/")
        version = 1
        if len(fields) == 4:
            version = int(fields.pop(2).removeprefix("v"))
        size, overlap, seq = (int(v) for v in fields)
        first_id, last_id = (int(v) for v in edges.split("-"))
    except ValueError:
        return None
    return {"size": size, "overlap": overlap, "version": version, "seq": seq,
            "first_id": first_id, "last_id": last_id}


def due_room_chunks(room_uuid: UUID, size: int, overlap: int, version: int,
                    limit: int) -> list[dict[str, int]]:
    """The next chunks of a room's series that can be summarized now, oldest
    first, at most `limit`: `{seq, first_id, last_id}` each.

    The series continues after the newest stored chunk's last message. A
    chunk is due once `overlap` counted messages follow it, because the model
    reads those as context. Appending to the room never changes a chunk that
    was already cut."""
    newest = (db.session.query(RoomChunk)
              .filter_by(room_uuid=room_uuid, chunk_size=size, overlap=overlap,
                         prompt_version=version)
              .order_by(RoomChunk.seq.desc()).first())
    seq = newest.seq + 1 if newest else 0
    after = newest.last_message_id if newest else 0
    ids = [m["id"] for m in counted_room_messages(room_uuid, after_id=after)]
    out = []
    start = 0
    while len(out) < limit and start + size + overlap <= len(ids):
        out.append({"seq": seq, "first_id": ids[start],
                    "last_id": ids[start + size - 1]})
        seq += 1
        start += size
    return out


def ensure_room_chunk_jobs(room_uuid: UUID, size: int, overlap: int,
                           version: int, limit: int) -> int:
    """Queue a summary job for each due chunk, at most `limit`. A chunk that
    already has a job is not queued again. Returns how many were created."""
    created = 0
    for due in due_room_chunks(room_uuid, size, overlap, version, limit):
        _job, new = ensure_housekeeping_job(
            ROOM_CHUNK_JOB, room_uuid=room_uuid,
            subject=chunk_subject(size, overlap, version, **due))
        created += int(new)
    return created


def store_room_chunk(**values: Any) -> RoomChunk:
    chunk = RoomChunk(**values)
    db.session.add(chunk)
    db.session.commit()
    return chunk


def list_room_chunks(room_uuid: UUID, size: int, overlap: int,
                     version: int) -> list[RoomChunk]:
    return (db.session.query(RoomChunk)
            .filter_by(room_uuid=room_uuid, chunk_size=size, overlap=overlap,
                       prompt_version=version)
            .order_by(RoomChunk.seq.asc()).all())


def list_room_chunk_series(room_uuid: UUID) -> list[tuple[int, int, int, int]]:
    """The series a room has chunks in:
    `(chunk_size, overlap, prompt_version, chunks)`."""
    rows = (db.session.query(RoomChunk.chunk_size, RoomChunk.overlap,
                             RoomChunk.prompt_version, sa.func.count())
            .filter(RoomChunk.room_uuid == room_uuid)
            .group_by(RoomChunk.chunk_size, RoomChunk.overlap,
                      RoomChunk.prompt_version)
            .order_by(RoomChunk.chunk_size, RoomChunk.overlap,
                      RoomChunk.prompt_version).all())
    return [(r[0], r[1], r[2], r[3]) for r in rows]
