"""Durable assistant trace: assistant_run / assistant_step persistence.

The trace tables are the source of truth for an assistant turn. The loop calls
exactly three helpers — `start_assistant_run`, `append_assistant_step`,
`finish_run` — plus `list_assistant_steps` for readers. Re-exported from `db`.

Steps write only to these tables. A turn used to mirror each step into the room
as a `debug-assistant` row and each reasoning channel as a `thinking` row, which
buried the conversation under a dozen bubbles per run; the room now carries one
`kind="progress"` row instead (see the agent's `_publish_progress`), linking to
/assistant where the full trace already lives.
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

import sqlalchemy as sa

from db.chat import post_chat_message
from db.models import (
    CHAT_NOTIFY_CHANNEL,
    AssistantControl,
    AssistantRun,
    AssistantStep,
    AssistantWriteIntent,
    ChatMessage,
    ChatUser,
    SecondOpinionAssessment,
    SecondOpinionReview,
    db,
)
from db.queue import fail_journal_if_processing


_MODEL_PROGRESS_TEXT_LIMIT = 100_000


def _bounded_model_progress_text(value: str | None) -> str | None:
    """Keep checkpoints useful without growing the run's JSON metadata forever."""
    if value is None or len(value) <= _MODEL_PROGRESS_TEXT_LIMIT:
        return value
    marker = "\n\n...[checkpoint truncated]...\n\n"
    remaining = _MODEL_PROGRESS_TEXT_LIMIT - len(marker)
    head = remaining // 2
    return value[:head] + marker + value[-(remaining - head):]


StepPhase = Literal["planned", "running", "observed", "failed", "final",
                    "control", "skipped"]


def _assistant_notify(run_uuid: UUID, event: str) -> None:
    """Emit one chat_events NOTIFY keyed by `assistant_run_uuid`, so the
    /assistant page can live-refresh the run it is showing. The payload has no
    `room_uuid`, which is exactly why chat clients ignore it (their onmessage
    returns early without one). `event` classifies the source ('run' lifecycle,
    'step' open/settle, 'model' streaming checkpoint) for debuggability; the
    page just refreshes on any of them. Must run inside the writing
    transaction so listeners see committed rows on delivery."""
    db.session.execute(
        sa.text("SELECT pg_notify(:channel, :payload)"),
        {
            "channel": CHAT_NOTIFY_CHANNEL,
            "payload": json.dumps(
                {"assistant_run_uuid": str(run_uuid), "event": event}
            ),
        },
    )


def start_assistant_run(
    journal_id: UUID,
    room_uuid: UUID,
    agent_uuid: UUID,
    step_limit: int = 6,
) -> AssistantRun:
    """Open a run row (status 'running') and return it."""
    run = AssistantRun(
        journal_id=journal_id,
        room_uuid=room_uuid,
        agent_uuid=agent_uuid,
        status="running",
        step_limit=step_limit,
    )
    db.session.add(run)
    db.session.flush()
    _assistant_notify(run.uuid, "run")
    db.session.commit()
    return run


def assistant_step_path(run_uuid: UUID, step_uuid: UUID) -> str:
    """The /assistant deep link to one step of one run: the run page scrolled to
    (and :target-highlighting) the element with id="step-<step_uuid>"."""
    return f"/assistant?id={run_uuid}#step-{step_uuid}"


def open_assistant_step(
    *,
    run_uuid: UUID,
    step_index: int,
    action: str | None,
    reason: str | None = None,
    args: dict[str, Any] | None = None,
    log: list | None = None,
    system_prompt: str | None = None,
    user_prompt: str | None = None,
    reasoning: str | None = None,
    model_response: str | None = None,
    rejected_attempts: list | None = None,
    requested_at: datetime | None = None,
    model_group_uuid: UUID | None = None,
    model_uuid: UUID | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    duration_ms: int | None = None,
) -> AssistantStep:
    """Insert a step's single row at phase `running` and commit it before the
    action runs (trace-before-action durability: a kill mid-action leaves this
    row). Returns the row so the caller has its stable `uuid` to bind a
    write-intent to. Posts no chat row — that lands at settle, when the
    observation exists."""
    step = AssistantStep(
        run_uuid=run_uuid,
        step_index=step_index,
        phase="running",
        action=action,
        reason=reason,
        args=args or {},
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        log=log,
        reasoning=reasoning,
        model_response=model_response,
        rejected_attempts=rejected_attempts or None,
        requested_at=requested_at,
        model_group_uuid=model_group_uuid,
        model_uuid=model_uuid,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        duration_ms=duration_ms,
    )
    db.session.add(step)
    db.session.flush()
    _assistant_notify(run_uuid, "step")
    db.session.commit()
    return step


def settle_assistant_step(
    step: AssistantStep,
    *,
    phase: StepPhase,
    observation_preview: str | None = None,
    observation: dict[str, Any] | None = None,
    error: str | None = None,
) -> AssistantStep:
    """Settle an open step in place: UPDATE its `running` row to a terminal
    `phase` (observed/failed) with the outcome. One row per step — no append."""
    step.phase = phase
    step.observation_preview = observation_preview
    step.observation = observation
    step.settled_at = datetime.now(UTC)
    step.error = error
    db.session.add(step)
    db.session.flush()
    _assistant_notify(step.run_uuid, "step")
    db.session.commit()
    return step


def append_assistant_step(
    *,
    run_uuid: UUID,
    step_index: int,
    phase: StepPhase,
    action: str | None,
    reason: str | None = None,
    args: dict[str, Any] | None = None,
    log: list | None = None,
    system_prompt: str | None = None,
    user_prompt: str | None = None,
    reasoning: str | None = None,
    model_response: str | None = None,
    rejected_attempts: list | None = None,
    code_driven: bool = False,
    requested_at: datetime | None = None,
    observation_preview: str | None = None,
    error: str | None = None,
    model_group_uuid: UUID | None = None,
    model_uuid: UUID | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    duration_ms: int | None = None,
) -> AssistantStep:
    """Record a **single-insert** step row — the terminal-only path for a step
    with no `running`→settle lifecycle: a `failed` validation, the `final` reply,
    and `control` (stop/redirect) events. Normal action steps use open/settle
    instead.

    `code_driven` marks a row the loop produced on its own initiative (see the
    column): its action and reason are labels, not a model decision."""
    step = AssistantStep(
        run_uuid=run_uuid,
        step_index=step_index,
        phase=phase,
        action=action,
        reason=reason,
        args=args or {},
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        log=log,
        reasoning=reasoning,
        model_response=model_response,
        rejected_attempts=rejected_attempts or None,
        code_driven=code_driven,
        requested_at=requested_at,
        observation_preview=observation_preview,
        error=error,
        model_group_uuid=model_group_uuid,
        model_uuid=model_uuid,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        duration_ms=duration_ms,
    )
    db.session.add(step)
    db.session.flush()  # commit the step row before anything else this txn
    _assistant_notify(run_uuid, "step")
    db.session.commit()
    return step


def finish_run(
    run: AssistantRun,
    status: str,
    final_summary: str | None = None,
) -> AssistantRun:
    """Close a run with a terminal status and optional short summary."""
    run.status = status
    run.finished_at = datetime.now(UTC)
    if final_summary is not None:
        run.final_summary = final_summary
    db.session.add(run)
    _assistant_notify(run.uuid, "run")
    db.session.commit()
    return run


def set_run_summary(run: AssistantRun, summary: dict[str, Any]) -> AssistantRun:
    """Store the assistant_run_summarizer agent's post-completion digest on a run, stamping
    `summarized_at`. Overwrites any prior summary (the latest summarization wins)."""
    run.summary = {**summary, "summarized_at": datetime.now(UTC).isoformat()}
    db.session.add(run)
    db.session.commit()
    return run


def checkpoint_assistant_call(
    run: AssistantRun,
    *,
    step_index: int,
    system_prompt: str,
    user_prompt: str,
    requested_at: datetime,
    model_group_uuid: UUID | None,
) -> AssistantRun:
    """Persist a model request before dispatch so a killed worker leaves evidence."""
    metadata = dict(run.metadata_ or {})
    metadata["active_call"] = {
        "step_index": step_index,
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "requested_at": requested_at.isoformat(),
        "model_group_uuid": str(model_group_uuid) if model_group_uuid else None,
        "attempts": [],
    }
    run.metadata_ = metadata
    db.session.add(run)
    _assistant_notify(run.uuid, "model")
    db.session.commit()
    return run


def checkpoint_assistant_model_attempt(
    run: AssistantRun,
    *,
    model_uuid: UUID,
    model_name: str,
    timeout_seconds: float,
) -> AssistantRun:
    """Append the model and configured timeout to the active call checkpoint."""
    metadata = dict(run.metadata_ or {})
    active = dict(metadata.get("active_call") or {})
    attempts = list(active.get("attempts") or [])
    attempts.append({
        "model_uuid": str(model_uuid),
        "model_name": model_name,
        "timeout_seconds": timeout_seconds,
        "started_at": datetime.now(UTC).isoformat(),
    })
    active["attempts"] = attempts
    metadata["active_call"] = active
    run.metadata_ = metadata
    db.session.add(run)
    db.session.commit()
    return run


def checkpoint_assistant_model_failure(
    run: AssistantRun, *, model_uuid: UUID, error: str
) -> AssistantRun:
    """Attach an attempt error while retaining the prompts and timeout context."""
    metadata = dict(run.metadata_ or {})
    active = dict(metadata.get("active_call") or {})
    attempts = list(active.get("attempts") or [])
    for index in range(len(attempts) - 1, -1, -1):
        attempt = attempts[index]
        if attempt.get("model_uuid") == str(model_uuid) and not attempt.get("error"):
            attempt = dict(attempt)
            attempt["error"] = error
            attempt["finished_at"] = datetime.now(UTC).isoformat()
            attempts[index] = attempt
            break
    active["attempts"] = attempts
    metadata["active_call"] = active
    run.metadata_ = metadata
    db.session.add(run)
    _assistant_notify(run.uuid, "model")
    db.session.commit()
    return run


def checkpoint_assistant_model_progress(
    run: AssistantRun,
    *,
    model_uuid: UUID,
    reasoning: str | None,
    response_text: str | None,
) -> AssistantRun:
    """Persist the latest streamed reasoning/content for interruption recovery."""
    metadata = dict(run.metadata_ or {})
    active = dict(metadata.get("active_call") or {})
    attempts = list(active.get("attempts") or [])
    for index in range(len(attempts) - 1, -1, -1):
        attempt = attempts[index]
        if attempt.get("model_uuid") == str(model_uuid) and not attempt.get("error"):
            updated = dict(attempt)
            updated["partial_reasoning"] = _bounded_model_progress_text(reasoning)
            updated["partial_response"] = _bounded_model_progress_text(response_text)
            updated["last_progress_at"] = datetime.now(UTC).isoformat()
            attempts[index] = updated
            break
    active["attempts"] = attempts
    metadata["active_call"] = active
    run.metadata_ = metadata
    db.session.add(run)
    _assistant_notify(run.uuid, "model")
    db.session.commit()
    return run


def clear_assistant_call_checkpoint(run: AssistantRun) -> AssistantRun:
    metadata = dict(run.metadata_ or {})
    if "active_call" not in metadata:
        return run
    metadata.pop("active_call", None)
    run.metadata_ = metadata
    db.session.add(run)
    db.session.commit()
    return run


def set_failure_run_summary(run: AssistantRun, error: str) -> AssistantRun:
    """Store a non-LLM fallback summary immediately for a failed run."""
    trigger = get_run_trigger_message(run)
    trigger_text = (trigger or {}).get("text") or "Assistant request"
    return set_run_summary(run, {
        "trigger": str(trigger_text)[:240],
        "obstacles": [error],
        "outcome": "failed",
        "source": "failure-fallback",
    })


def post_assistant_failure_notice(
    run: AssistantRun, error: str
) -> ChatMessage:
    """Post one operational chat notice for a failed run and clear progress."""
    run_uuid = str(run.uuid)
    existing = (
        db.session.query(ChatMessage)
        .filter(
            ChatMessage.room_uuid == run.room_uuid,
            ChatMessage.sender_uuid == run.agent_uuid,
            ChatMessage.kind == "notice",
            ChatMessage.meta.contains({"assistant_failure_run_uuid": run_uuid}),
        )
        .order_by(ChatMessage.id.desc())
        .first()
    )
    if existing is not None:
        return existing

    reason = error.strip()[:1000] or "The assistant worker stopped unexpectedly."
    run_link = f"/assistant?id={run.uuid}"
    text = (
        "I stopped before completing this request.\n\n"
        f"Reason: {reason}\n\n"
        f"Inspect the failed run: [{run_link}]({run_link})"
    )
    return post_chat_message(
        run.room_uuid,
        run.agent_uuid,
        text,
        kind="notice",
        # `assistant_failure_run_uuid` keeps notice creation idempotent per
        # run; `assistant_run_uuid` is the pointer every terminal assistant
        # post carries, so one renderer gives them all a link to the trace.
        meta={"assistant_failure_run_uuid": run_uuid,
              "assistant_run_uuid": run_uuid},
    )


def list_active_assistant_runs() -> list[AssistantRun]:
    """Runs that a newly started supervisor cannot have a live worker for."""
    return (
        db.session.query(AssistantRun)
        .filter(AssistantRun.status.in_(("running", "stopping")))
        .order_by(AssistantRun.started_at)
        .all()
    )


def recover_interrupted_assistant_run(
    journal_id: UUID, reason: str
) -> AssistantRun | None:
    """Close an assistant run whose worker exited outside normal exception handling."""
    run = (
        db.session.query(AssistantRun)
        .filter(AssistantRun.journal_id == journal_id)
        .one_or_none()
    )
    if run is None or run.status not in ("running", "stopping"):
        return None

    now = datetime.now(UTC)
    active = dict((run.metadata_ or {}).get("active_call") or {})
    attempts = list(active.get("attempts") or [])
    last_attempt = attempts[-1] if attempts else {}
    timeout = last_attempt.get("timeout_seconds")
    model_name = last_attempt.get("model_name")
    detail = reason
    if model_name:
        detail += f" Active model: {model_name}."
    if timeout is not None:
        detail += f" Configured model timeout: {timeout:g}s."

    steps = list_assistant_steps(run.uuid)
    running_step = next((step for step in reversed(steps) if step.phase == "running"), None)
    if running_step is not None:
        settle_assistant_step(running_step, phase="failed", error=detail)
    else:
        default_index = max((s.step_index for s in steps), default=-1) + 1
        step_index = int(active.get("step_index", default_index))
        if any(step.step_index == step_index for step in steps):
            step_index = default_index
        requested_at = None
        try:
            requested_at = datetime.fromisoformat(active["requested_at"])
        except (KeyError, TypeError, ValueError):
            pass
        duration_ms = None
        if requested_at is not None:
            duration_ms = max(0, int((now - requested_at).total_seconds() * 1000))
        model_uuid = None
        model_group_uuid = None
        try:
            if last_attempt.get("model_uuid"):
                model_uuid = UUID(last_attempt["model_uuid"])
            if active.get("model_group_uuid"):
                model_group_uuid = UUID(active["model_group_uuid"])
        except (TypeError, ValueError):
            pass
        append_assistant_step(
            run_uuid=run.uuid,
            step_index=step_index,
            phase="failed",
            action=None,
            error=detail,
            system_prompt=active.get("system_prompt"),
            user_prompt=active.get("user_prompt"),
            reasoning=last_attempt.get("partial_reasoning"),
            model_response=last_attempt.get("partial_response"),
            requested_at=requested_at,
            duration_ms=duration_ms,
            model_group_uuid=model_group_uuid,
            model_uuid=model_uuid,
        )

    finish_run(run, "killed", final_summary=detail)
    set_failure_run_summary(run, detail)
    fail_journal_if_processing(journal_id, {
        "ok": False,
        "status": "killed",
        "assistant_run_uuid": str(run.uuid),
        "error": detail,
    })
    clear_assistant_call_checkpoint(run)
    post_assistant_failure_notice(run, detail)
    return run


def get_assistant_run(run_uuid: UUID) -> AssistantRun | None:
    """One run row by uuid (the primary key / log-greppable identifier), or None."""
    return db.session.get(AssistantRun, run_uuid)


def list_assistant_runs(limit: int = 50) -> list[AssistantRun]:
    """The most recent runs, newest first — the left pane of the /assistant
    inspector. (uuid is a stable tiebreaker for same-instant rows.)"""
    return (
        db.session.query(AssistantRun)
        .order_by(AssistantRun.started_at.desc(), AssistantRun.uuid.desc())
        .limit(limit)
        .all()
    )


def assistant_step_counts(run_uuids: list[UUID]) -> dict[UUID, int]:
    """Number of step rows per run, for a batch of runs (one GROUP BY — no
    N+1 over a result page). Runs with no steps are absent from the result; the
    caller treats a missing key as 0.

    Every row counts, matching how /assistant numbers its timeline: the
    warm-up and follow-up calls are steps there, so a run must not report a
    different number of steps on the two pages."""
    if not run_uuids:
        return {}
    rows = (
        db.session.query(AssistantStep.run_uuid, sa.func.count())
        .filter(AssistantStep.run_uuid.in_(run_uuids))
        .group_by(AssistantStep.run_uuid)
        .all()
    )
    return {run_uuid: n for run_uuid, n in rows}


def _overview_q_filter(query, q: str):
    """Narrow a run query to the case-insensitive substring `q` over the
    human-facing text: the summary digest's trigger line, the truncated
    final_summary, and the uuid (so a grepped id still finds its run)."""
    needle = f"%{q.strip()}%"
    return query.filter(
        sa.or_(
            AssistantRun.summary["trigger"].astext.ilike(needle),
            AssistantRun.final_summary.ilike(needle),
            sa.cast(AssistantRun.uuid, sa.Text).ilike(needle),
        )
    )


# Status facets for the overview, mirroring the left tree's buckets
# (_bucket_runs) and the inspector's _dash_status outcome reading.
_OVERVIEW_STATUS_PREDICATES = {
    "running": lambda: AssistantRun.status.in_(("running", "stopping")),
    "stopped": lambda: AssistantRun.status == "stopped",
    "resolved": lambda: AssistantRun.summary["outcome"].astext == "resolved",
    "unresolved": lambda: sa.or_(
        AssistantRun.summary["outcome"].astext.in_(("partial", "failed")),
        AssistantRun.status.in_(("failed", "killed")),
    ),
}


def list_assistant_runs_page(
    *, q: str = "", status: str = "all", since: datetime | None = None,
    sort: str = "started", direction: str = "desc", offset: int = 0,
    limit: int = 25,
) -> tuple[list[AssistantRun], int, dict[str, int]]:
    """A filtered/sorted/paginated page of runs for /assistant-overview.

    Returns `(page_runs, total, counts)`:
      - `page_runs` — the requested slice, with running runs pinned ahead of
        the rest and the chosen `sort`/`direction` ordering within each group.
      - `total` — rows matching `q` AND `since` AND `status` (drives the pager).
      - `counts` — per-facet counts over the `q`+`since`-filtered set only (so
        the status tabs show their numbers independent of the active tab).

    `since` keeps only runs started at or after that instant (the time-range
    picker); None means any time. Sort keys: started (started_at), summary
    (summary->>'trigger'), steps (assistant_step count), duration (finished_at
    - started_at).
    """
    base = (_overview_q_filter(db.session.query(AssistantRun), q)
            if q.strip() else db.session.query(AssistantRun))
    if since is not None:
        base = base.filter(AssistantRun.started_at >= since)

    counts = {"all": base.count()}
    for key, pred in _OVERVIEW_STATUS_PREDICATES.items():
        counts[key] = base.filter(pred()).count()

    page_q = base
    if status in _OVERVIEW_STATUS_PREDICATES:
        page_q = page_q.filter(_OVERVIEW_STATUS_PREDICATES[status]())
    total = page_q.count()

    # Same definition as assistant_step_counts, so the column sorts by the
    # number the column shows.
    step_count = (
        sa.select(sa.func.count(AssistantStep.id))
        .where(AssistantStep.run_uuid == AssistantRun.uuid)
        .correlate(AssistantRun)
        .scalar_subquery()
    )
    sort_col = {
        "started": AssistantRun.started_at,
        "summary": AssistantRun.summary["trigger"].astext,
        "duration": (AssistantRun.finished_at - AssistantRun.started_at),
        "steps": step_count,
    }.get(sort, AssistantRun.started_at)
    ordering = sort_col.asc() if direction == "asc" else sort_col.desc()

    # Running runs always lead, regardless of the chosen sort.
    running_first = sa.case(
        (AssistantRun.status.in_(("running", "stopping")), 0), else_=1)
    page_runs = (
        page_q.order_by(running_first, ordering, AssistantRun.uuid.desc())
        .offset(max(0, offset)).limit(max(1, limit)).all()
    )
    return page_runs, total, counts


def get_run_trigger_message(run: AssistantRun) -> dict[str, Any] | None:
    """The chat message that initiated a run: the latest human `message` in the
    run's room at or before it started. Best-effort — returns None when none is
    found (e.g. a run seeded outside the chat flow). Returns a small dict
    (uuid/sender_uuid/sender_name/text/timestamp) for the /assistant inspector's
    trigger block; sender_uuid links to that participant's /user page."""
    row = (
        db.session.query(ChatMessage, ChatUser.name)
        .join(ChatUser, ChatUser.uuid == ChatMessage.sender_uuid)
        .filter(
            ChatMessage.room_uuid == run.room_uuid,
            ChatMessage.kind == "message",
            ChatUser.user_type == "human",
            ChatMessage.created_at <= run.started_at,
        )
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
        .first()
    )
    if row is None:
        return None
    msg, sender_name = row
    return {
        "id": msg.id,            # the int id the chat DOM anchors on (data-message-id)
        "uuid": str(msg.uuid),
        "sender_uuid": str(msg.sender_uuid),
        "sender_name": sender_name,
        "text": msg.text,
        "timestamp": msg.created_at.strftime("%Y-%m-%d %H:%M") if msg.created_at else "",
    }


def get_run_final_reply(run: AssistantRun) -> dict[str, Any] | None:
    """The agent's final reply for a run: the latest agent `message` in the room
    within the run's lifetime (the run stores only a truncated `final_summary`).
    None for a still-running run or one that crashed before replying. Bounded to
    `[started_at, finished_at]` so it can't borrow a sibling run's reply: without
    the lower bound, a later run that fails before replying would pick up the
    *previous* run's reply (any agent message before its finished_at). Returns a
    small dict (id/uuid/text) for the /assistant inspector's verdict block; `id`
    is the int the chat DOM anchors on, letting the verdict link jump to it."""
    if run.finished_at is None:
        return None
    msg = (
        db.session.query(ChatMessage)
        .filter(
            ChatMessage.room_uuid == run.room_uuid,
            ChatMessage.sender_uuid == run.agent_uuid,
            ChatMessage.kind == "message",
            ChatMessage.created_at >= run.started_at,
            ChatMessage.created_at <= run.finished_at,
        )
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
        .first()
    )
    if msg is None:
        return None
    return {"id": msg.id, "uuid": str(msg.uuid), "text": msg.text}


def list_assistant_steps(run_uuid: UUID) -> list[AssistantStep]:
    """All step rows for a run, in commit order (id ascending).

    What the loop's own readers want — the row written last IS the newest, and
    a running step is found by walking back from the end. A reader that wants
    the order the calls actually RAN wants `assistant_trace_steps`."""
    return (
        db.session.query(AssistantStep)
        .filter(AssistantStep.run_uuid == run_uuid)
        .order_by(AssistantStep.id)
        .all()
    )


def step_started_at(step):
    """When a step's model call BEGAN — which is not when its row was written.

    `requested_at` where it was recorded. Rows predating that capture are
    placed at their write time minus how long they took: the response landed
    when the row was written, so that is where the call ran. None when the row
    has neither, which is a legacy row the caller has to place itself."""
    if step.requested_at:
        return step.requested_at
    if step.created_at and step.duration_ms:
        return step.created_at - timedelta(milliseconds=step.duration_ms)
    return None


def assistant_trace_steps(run_uuid: UUID) -> list[AssistantStep]:
    """A run's steps in the order they ran, for the surfaces that read as a
    trace: the /assistant timeline and the markdown export.

    Commit order is not causal order, and the reply audit is where they come
    apart. The audit runs on a reply the decide call has already produced, but
    the decide row cannot be written until the audit's verdict is known (it
    settles `final` or `failed` on that verdict), so the audit commits first.
    Read by row id the audit appears BEFORE the call it audited — and since an
    audit prompt carries no action list, the run reads as one where the model
    was never offered its actions.

    The model-call waterfall on the same page has always been laid out on the
    clock, so commit order left one page disagreeing with itself about which of
    two calls came first. `_step_kinds` already had to reach for `requested_at`
    for the same reason; this puts the rows themselves in that order.

    A row with no start at all falls back to when it was written, and one
    without even that keeps its row position at the end: a legacy row belongs
    where it landed rather than at a guessed moment."""
    placed, unplaced = [], []
    for s in list_assistant_steps(run_uuid):
        at = step_started_at(s) or s.created_at
        (placed if at else unplaced).append((at, s))
    placed.sort(key=lambda p: (p[0], p[1].id))
    return [s for _, s in placed] + [s for _, s in unplaced]


# --- model calls --------------------------------------------------------------
#
# A run's model calls do not map one-to-one onto step rows. Most are a step's
# own decide/code-driven call, but two ride inside something else: the
# second-opinion review (its own table) and the acceptance-criteria revision's
# inner call (in a step's observation payload). Counting rows therefore
# under-reports the calls, and their time books as "action" time — exactly the
# time an operator is hunting for. This is the single enumeration: the
# inspector's dashboard and waterfall, the markdown export, and the in-chat
# progress row all read it, so no surface can quote a different number of calls
# than another.


def _parse_ts(value):
    """An ISO timestamp from a JSONB payload, or None. Payload-sourced values
    are never trusted to parse — a malformed one drops the call's placement,
    not the page."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    # Payloads store UTC; the rows come back from Postgres in the session's
    # zone. Convert so a payload-sourced call is not shown hours off the step
    # it ran inside.
    return parsed.astimezone() if parsed.tzinfo else parsed


def _call(label: str, kind: str, *, start, duration_ms, anchor: str = "",
          model_uuid=None, input_tokens=None, output_tokens=None,
          detail: str = "") -> dict:
    return {"label": label, "kind": kind, "start": start,
            "duration_ms": duration_ms, "anchor": anchor,
            "model_uuid": str(model_uuid) if model_uuid else None,
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "detail": detail}


def _rejected_calls(step) -> list[dict]:
    """The calls this step made and threw away: responses that arrived and
    were refused by the schema or a validator, each retried with the reason
    attached (see `ModelGroupAgent.REJECTED_RESPONSE_RETRIES`).

    Real calls to a real model, so they belong in the enumeration on the same
    footing as the one that succeeded — the step's own `duration_ms` covers
    only the attempt it kept, and without these rows their seconds read as a
    gap where nothing was running."""
    calls: list[dict] = []
    attempts = step.rejected_attempts or []
    for index, attempt in enumerate(attempts, start=1):
        # Numbered only when there were several, so the common single retry
        # reads as plain "(rejected)" and a model that failed twice in a row
        # is still tellable apart row by row.
        ordinal = f" {index}/{len(attempts)}" if len(attempts) > 1 else ""
        calls.append(_call(
            f"{step.action or '—'} (rejected{ordinal})", "rejected",
            start=_parse_ts(attempt.get("requested_at")),
            duration_ms=attempt.get("ms"), anchor=str(step.uuid),
            model_uuid=attempt.get("model_uuid"),
            input_tokens=attempt.get("input_tokens"),
            output_tokens=attempt.get("output_tokens")))
    return calls


#: How much of an embedded text the waterfall's name column carries. The
#: column is 14rem wide; past this the row ellipsises either way, and the
#: tooltip has the rest.
EMBED_LABEL_CHARS: int = 40


def embed_call_label(call: dict) -> tuple[str, str]:
    """An embed row's label and its tooltip detail: WHAT went to the embedder.

    The model is deliberately not in either — a run embeds on one model, so
    naming it per row is the same string repeated down the column; it is named
    once per step, in the timing block's embedder line. The text IS worth
    repeating, because it is the thing that differs: without it a column of
    identical `embed` rows could equally be one query embedded repeatedly,
    several different queries, or one call drawn more than once.

    A batched call — a first-run seed populate embeds the whole registry — is
    named by its size instead: its first chunk says nothing about the call."""
    preview = [str(t) for t in (call.get("preview") or [])]
    texts = call.get("texts") or 0
    if texts > 1:
        label = f"embed {texts} texts"
    elif preview:
        head = preview[0][:EMBED_LABEL_CHARS]
        label = f'embed "{head}{"…" if len(preview[0]) > EMBED_LABEL_CHARS else ""}"'
    else:
        # A payload written before the text was captured, or a call that
        # embedded nothing. Still a call, still worth its row.
        label = "embed"
    return label, " / ".join(preview)


def _embedding_calls(step, data: dict) -> list[dict]:
    """The embedder calls a step made, from its `timing` payload.

    A different model from the ones above — no tokens, no prompt, and not the
    assistant's own — but it runs on the same local runtime, so its calls are
    part of what the wall-clock between two LLM bars is made of, and enough of
    them can evict the model the next decide call needs warm. Kept out of the
    run's token/throughput totals (see `assistant_run_stats`) and counted on
    their own."""
    timing = data.get("timing") or {}
    embeddings = timing.get("embeddings") or {}
    calls: list[dict] = []
    for call in embeddings.get("calls") or []:
        label, detail = embed_call_label(call)
        calls.append(_call(
            label, "embedding",
            start=_parse_ts(call.get("requested_at")),
            duration_ms=call.get("ms"), anchor=str(step.uuid), detail=detail))
    return calls


#: A gap shorter than this is not drawn. Sub-second scheduling jitter between
#: two adjacent calls is not a finding, and a row per 0.1s gap would bury the
#: ones that are.
UNACCOUNTED_MIN_MS: int = 1000


def _phase_calls(step, data: dict) -> list[dict]:
    """The named phases a step's action recorded, from its `timing` payload.

    Not model calls — spans of the action's own wall-clock, which is exactly
    the part a per-call waterfall leaves as empty space. `_PhaseTimer` records
    them with start times for this purpose.
    """
    timing = data.get("timing") or {}
    calls: list[dict] = []
    for phase in timing.get("phases") or []:
        name = phase.get("name") or "phase"
        calls.append(_call(
            name, "phase",
            start=_parse_ts(phase.get("started_at")),
            duration_ms=phase.get("ms"), anchor=str(step.uuid)))
    return calls


def _end_of(call):
    """When a row stopped, or None if it cannot be placed."""
    if not call["start"]:
        return None
    return call["start"] + timedelta(milliseconds=call["duration_ms"] or 0)


def _assign_depth(calls: list[dict]) -> None:
    """Indent each row under what contains it.

    The only containment that exists is phase-inside-step and call-inside-
    phase, so this is a flat rule rather than a tree walk: a phase is a child
    of its step's own call row, and anything starting inside a phase's span is
    a child of that phase.
    """
    spans = [(c["start"], _end_of(c)) for c in calls if c["kind"] == "phase"]
    spans = [(a, b) for a, b in spans if a and b]
    for c in calls:
        if c["kind"] == "phase":
            c["depth"] = 1
            continue
        start = c["start"]
        inside = bool(start) and any(a <= start < b for a, b in spans)
        c["depth"] = 2 if inside else 0


def _gap_rows(covered: list[dict], window_start, window_end, depth: int,
              anchor: str) -> list[dict]:
    """`unaccounted` rows for the parts of a window no row in `covered` fills.

    Deliberately unlabelled beyond a duration: it is the absence of evidence,
    and naming it "model load" would be a guess printed as a fact.
    """
    if window_start is None or window_end is None:
        return []
    placed = sorted(
        ((c["start"], _end_of(c)) for c in covered if c["start"]),
        key=lambda pair: pair[0])
    rows: list[dict] = []
    cursor = window_start
    for start, end in placed:
        if start > cursor:
            ms = int((start - cursor).total_seconds() * 1000)
            if ms >= UNACCOUNTED_MIN_MS:
                rows.append(_call("unaccounted", "unaccounted", start=cursor,
                                  duration_ms=ms, anchor=anchor))
        cursor = max(cursor, end or start)
    if window_end > cursor:
        ms = int((window_end - cursor).total_seconds() * 1000)
        if ms >= UNACCOUNTED_MIN_MS:
            rows.append(_call("unaccounted", "unaccounted", start=cursor,
                              duration_ms=ms, anchor=anchor))
    for r in rows:
        r["depth"] = depth
    return rows


def _unaccounted_calls(calls: list[dict], run) -> list[dict]:
    """Every stretch of the run no row covers, measured per level.

    Per level, not globally: a phase covers its own internal holes, so a
    whole-run complement finds nothing inside one. The ten seconds a phase
    spends before its model call only appear when the phase's children are
    measured against the phase's own span.
    """
    rows: list[dict] = []
    # A phase is drawn indented but it is measured wall-clock all the same, so
    # it covers the top level too. Counting only depth-0 rows reported a gap
    # the length of every phase inside an action.
    covering = [c for c in calls
                if c["start"] and (c.get("depth") == 0 or c["kind"] == "phase")]
    if run is not None and covering:
        starts = [c["start"] for c in covering]
        first = min(starts + ([run.started_at] if run.started_at else []))
        ends = [e for e in (_end_of(c) for c in covering) if e]
        last = max(ends + ([run.finished_at] if run.finished_at else []))
        rows += _gap_rows(covering, first, last, 0, "")
    for phase in [c for c in calls if c["kind"] == "phase" and c["start"]]:
        end = _end_of(phase)
        children = [c for c in calls
                    if c.get("depth") == 2 and c["start"]
                    and phase["start"] <= c["start"] < (end or c["start"])]
        # A phase with nothing inside it already says what its time was spent
        # on. Reporting it as unaccounted as well double-counts it and buries
        # the gaps that genuinely have no explanation.
        if not children:
            continue
        rows += _gap_rows(children, phase["start"], end, 2, phase["anchor"])
    return rows


def _inner_calls(step, data: dict) -> list[dict]:
    """The model calls a step made from inside its action, which have no row of
    their own: the criteria revision's inner call. It records `requested_at` +
    `usage` in the observation payload; older payloads have the usage but no
    start time."""
    calls: list[dict] = []
    if "acceptance_criteria" in data or "usage" in data:
        usage = data.get("usage") or {}
        if usage.get("ms") is not None:
            calls.append(_call(
                "acceptance_criteria revision", "inner",
                start=_parse_ts(data.get("requested_at")),
                duration_ms=usage.get("ms"), anchor=str(step.uuid),
                model_uuid=data.get("model_uuid"),
                input_tokens=usage.get("input"),
                output_tokens=usage.get("output")))
    return calls


def retry_resumed_at(step):
    """When the attempt a step RECORDS began, on a call that retried.

    `requested_at` is when the call was sent, which on a retried call is when
    its first — rejected — attempt was sent. The row's `duration_ms` is the
    kept attempt's. Placed at `requested_at` the two bars start together and
    the kept one looks like it ran alongside the attempt it replaced, when in
    fact it began where that one stopped: the attempts are sequential, each
    sent only after the previous was refused.

    None when the call kept its first answer, which is nearly every call."""
    resumed = None
    for attempt in step.rejected_attempts or []:
        started = _parse_ts(attempt.get("requested_at"))
        if started is None:
            continue
        ended = started + timedelta(milliseconds=attempt.get("ms") or 0)
        if resumed is None or ended > resumed:
            resumed = ended
    return resumed


def assistant_llm_calls(steps: list, reviews: list | None = None,
                        run=None) -> list[dict]:
    """Every row of the run's timeline, oldest first.

    Model calls, the phases an action recorded inside itself, and — given
    `run`, which bounds the window — an `unaccounted` row for every stretch
    neither covers. A gap in a per-call waterfall is the one thing an operator
    cannot investigate: nothing to click, nothing named, no way to tell a model
    load from a slow query. Synthesizing the gaps makes the timeline sum to the
    run's wall-clock however little of it is instrumented, and the bars
    themselves say where instrumenting would pay.

    Each row carries `depth` for the renderer (see `_assign_depth`). `phase`
    and `unaccounted` rows are spans rather than calls and are excluded from
    the totals in `assistant_run_stats`.

    A call with no recorded start is placed at its row's end minus its
    duration — the response landed when the row was written, so that is where
    it ran. Calls with neither a start nor a duration (a crash before the model
    answered) still count: they happened, and hiding them would make the run
    look cheaper than it was."""
    calls: list[dict] = []
    for s in steps:
        if s.phase in ("control", "skipped"):
            # An operator event, or a call the loop could not make — both are
            # rows worth having in the trace, neither is a model call. Counting
            # a skip would price a call that never went out.
            continue
        data = (s.observation or {}).get("data") or {}
        # Before the step's own row: the attempts it threw away came first,
        # and enumerating them first keeps that order even where an attempt
        # recorded no duration to sort by.
        calls.extend(_rejected_calls(s))
        if s.requested_at or s.duration_ms is not None or s.system_prompt:
            start = step_started_at(s)
            resumed = retry_resumed_at(s)
            if resumed is not None and (start is None or resumed > start):
                start = resumed
            calls.append(_call(
                s.action or "—", "code-driven" if s.code_driven else "decide",
                start=start, duration_ms=s.duration_ms, anchor=str(s.uuid),
                model_uuid=s.model_uuid, input_tokens=s.input_tokens,
                output_tokens=s.output_tokens))
        calls.extend(_inner_calls(s, data))
        calls.extend(_embedding_calls(s, data))
        calls.extend(_phase_calls(s, data))
    by_uuid = {str(s.uuid): s for s in steps}
    for r in reviews or []:
        # A review runs between its step's decide call returning and the action
        # executing, so a row written before review start times were recorded
        # is placed at the moment its step row was opened.
        gated = by_uuid.get(str(r.step_uuid)) if r.step_uuid else None
        start = r.requested_at or (gated.created_at if gated else None)
        calls.append(_call(
            "second opinion", "review", start=start,
            duration_ms=r.duration_ms,
            anchor=str(r.step_uuid) if r.step_uuid else "",
            model_uuid=r.model_uuid, input_tokens=r.input_tokens,
            output_tokens=r.output_tokens))
    # Undated calls sort last rather than crashing the comparison — they are
    # legacy rows, and the waterfall renders them without a bar.
    calls.sort(key=lambda c: (c["start"] is None, c["start"] or datetime.min))
    # Depth first: the gap computation reads it to know which rows share a
    # level, and a gap row inherits the level it was measured on.
    _assign_depth(calls)
    calls.extend(_unaccounted_calls(calls, run))
    calls.sort(key=lambda c: (c["start"] is None, c["start"] or datetime.min,
                              c["depth"]))
    return calls


#: Timeline rows that describe a stretch of wall-clock rather than a call.
_SPAN_KINDS: frozenset[str] = frozenset({"embedding", "phase", "unaccounted"})


def assistant_run_stats(steps: list, reviews: list | None = None,
                        run=None) -> dict:
    """What a run has cost so far: `{calls, input_tokens, output_tokens,
    duration_ms, tps, embedding_calls, embedding_ms}`. Summed from the call
    enumeration rather than the step rows, so the inner calls are in the
    totals.

    The embedder is counted apart from the LLM totals rather than folded in:
    it produces no tokens, so its seconds in `duration_ms` would drag the
    throughput figure down against work it never did. It gets its own two
    numbers instead — visible, and not mixed into anything.

    Rejected attempts DO count. The run paid their tokens and their seconds,
    and leaving them out is what made a retried step look like a fast call
    followed by a gap where nothing ran."""
    calls = assistant_llm_calls(steps, reviews, run=run)
    embeddings = [c for c in calls if c["kind"] == "embedding"]
    # `phase` and `unaccounted` are spans, not calls: a phase overlaps the
    # calls inside it and a gap is the absence of one, so counting either
    # would inflate the call count and double-count seconds already inside a
    # model bar.
    llm = [c for c in calls if c["kind"] not in _SPAN_KINDS]
    in_tokens = sum((c["input_tokens"] or 0) for c in llm)
    out_tokens = sum((c["output_tokens"] or 0) for c in llm)
    llm_ms = sum((c["duration_ms"] or 0) for c in llm)
    return {
        "calls": len(llm),
        "input_tokens": in_tokens,
        "output_tokens": out_tokens,
        "duration_ms": llm_ms,
        "tps": round((in_tokens + out_tokens) / (llm_ms / 1000)) if llm_ms else None,
        "embedding_calls": len(embeddings),
        "embedding_ms": sum((c["duration_ms"] or 0) for c in embeddings),
    }


# --- confirm-tier write intents (Phase 5) ------------------------------------


def write_intent_payload_hash(capability_name: str, payload: dict[str, Any]) -> str:
    """Stable hash binding a capability to an exact payload. Confirming approves
    this hash; execution re-checks it so a confirmed write can't be mutated."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{capability_name}\n{canonical}".encode()).hexdigest()


def create_write_intent(
    *,
    run_uuid: UUID,
    capability_name: str,
    payload: dict[str, Any],
    preview_text: str,
    room_uuid: UUID,
    agent_uuid: UUID,
    state: str = "proposed",
    result: dict[str, Any] | None = None,
    step_uuid: UUID | None = None,
) -> AssistantWriteIntent:
    """Open a write intent. Defaults to a `proposed` confirm-tier proposal; a
    log-and-undo recorder passes `state="completed"` with a `result` so the row
    is never confirmable as `proposed` (no double-execute window). `step_uuid`
    binds the intent to the step that produced it (the identity pointer)."""
    intent = AssistantWriteIntent(
        run_uuid=run_uuid,
        step_uuid=step_uuid,
        capability_name=capability_name,
        payload=payload,
        payload_hash=write_intent_payload_hash(capability_name, payload),
        preview_text=preview_text,
        state=state,
        room_uuid=room_uuid,
        agent_uuid=agent_uuid,
        result=result or {},
    )
    db.session.add(intent)
    db.session.commit()
    return intent


def get_write_intent(intent_uuid: UUID) -> AssistantWriteIntent | None:
    return (
        db.session.query(AssistantWriteIntent)
        .filter(AssistantWriteIntent.uuid == intent_uuid)
        .one_or_none()
    )


def list_write_intents_for_run(run_uuid: UUID) -> list[AssistantWriteIntent]:
    """All write intents a run produced, in creation order — the /assistant
    inspector buckets them by `step_uuid` to render each one under its step."""
    return (
        db.session.query(AssistantWriteIntent)
        .filter(AssistantWriteIntent.run_uuid == run_uuid)
        .order_by(AssistantWriteIntent.id)
        .all()
    )


def set_write_intent_state(
    intent: AssistantWriteIntent,
    state: str,
    *,
    confirmed_by_uuid: UUID | None = None,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> AssistantWriteIntent:
    """Transition an intent and stamp the matching timestamp."""
    now = datetime.now(UTC)
    intent.state = state
    if state == "confirmed":
        intent.confirmed_at = now
        if confirmed_by_uuid is not None:
            intent.confirmed_by_uuid = confirmed_by_uuid
    elif state == "executing":
        intent.executed_at = now
    elif state in ("completed", "failed", "rejected", "undone"):
        intent.completed_at = now
    if result is not None:
        intent.result = result
    if error is not None:
        intent.error = error
    db.session.add(intent)
    db.session.commit()
    return intent


# --- control channel (Phase 6) -----------------------------------------------


def create_assistant_control(
    *,
    run_uuid: UUID,
    command: str,
    payload: dict[str, Any] | None = None,
    requested_by_uuid: UUID | None = None,
    note: str | None = None,
) -> "AssistantControl":
    """Insert a pending steering command (stop/redirect) for a run."""
    control = AssistantControl(
        run_uuid=run_uuid, command=command, payload=payload or {},
        state="pending", requested_by_uuid=requested_by_uuid, note=note,
    )
    db.session.add(control)
    db.session.commit()
    return control


def list_pending_controls(run_uuid: UUID) -> list["AssistantControl"]:
    """Pending controls for a run, oldest first (the order the loop applies them)."""
    return (
        db.session.query(AssistantControl)
        .filter(AssistantControl.run_uuid == run_uuid, AssistantControl.state == "pending")
        .order_by(AssistantControl.id)
        .all()
    )


def mark_control_state(
    control: "AssistantControl", state: str, *, note: str | None = None
) -> "AssistantControl":
    """Transition a control to applied/ignored, stamping applied_at."""
    control.state = state
    if state in ("applied", "ignored"):
        control.applied_at = datetime.now(UTC)
    if note is not None:
        control.note = note
    db.session.add(control)
    db.session.commit()
    return control


def request_run_stop(run_uuid: UUID) -> bool:
    """Signal an intent to stop a still-running run (status -> 'stopping'). The
    loop performs the actual clean stop at its next step boundary. Returns False
    for an unknown run; a no-op for an already-terminal run."""
    run = db.session.get(AssistantRun, run_uuid)
    if run is None:
        return False
    if run.status == "running":
        run.status = "stopping"
        db.session.add(run)
        db.session.commit()
    return True


# --- second-opinion review records -------------------------------------------
# The pre-execution gate's judgment as first-class rows, so "why did this run go
# wrong?" and "why was this right for the wrong reasons?" are queries rather
# than a scan of observation blobs. See
# notes/proposals/2026-07-28-second-opinion-review-records.md.


def record_second_opinion_review(
    *,
    run_uuid: UUID,
    step_uuid: UUID | None,
    step_index: int,
    action: str,
    verdict: str,
    journal_id: UUID | None = None,
    room_uuid: UUID | None = None,
    agent_uuid: UUID | None = None,
    problems: list[dict[str, Any]] | None = None,
    skip_reason: str | None = None,
    error: str | None = None,
    group_from: str | None = None,
    model_uuid: UUID | None = None,
    system_prompt: str | None = None,
    user_prompt: str | None = None,
    reasoning: str | None = None,
    response: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    duration_ms: int | None = None,
    requested_at: datetime | None = None,
) -> SecondOpinionReview:
    """Persist one review. `categories` is derived from `problems` here rather
    than accepted from the caller, so the indexed column can never disagree with
    the findings it summarizes."""
    rows = list(problems or [])
    # dict.fromkeys: distinct, first-seen order — stable across equal inputs so
    # the column doesn't churn between otherwise-identical reviews.
    categories = list(dict.fromkeys(
        str(p["category"]) for p in rows if p.get("category")
    ))
    review = SecondOpinionReview(
        run_uuid=run_uuid, step_uuid=step_uuid, step_index=step_index,
        journal_id=journal_id, room_uuid=room_uuid, agent_uuid=agent_uuid,
        action=action, verdict=verdict, skip_reason=skip_reason, error=error,
        problems=rows, categories=categories, group_from=group_from,
        model_uuid=model_uuid, system_prompt=system_prompt,
        user_prompt=user_prompt, reasoning=reasoning, response=response,
        input_tokens=input_tokens, output_tokens=output_tokens,
        duration_ms=duration_ms, requested_at=requested_at,
    )
    db.session.add(review)
    db.session.commit()
    return review


def list_second_opinion_reviews(run_uuid: UUID) -> list[SecondOpinionReview]:
    """Every review for a run, oldest first. Retries reuse a step_index, so this
    order is the attempt chain."""
    return list(
        db.session.query(SecondOpinionReview)
        .filter(SecondOpinionReview.run_uuid == run_uuid)
        .order_by(SecondOpinionReview.step_index, SecondOpinionReview.id)
        .all()
    )


def record_second_opinion_assessment(
    review_uuid: UUID, assessment: str, note: str = "",
) -> SecondOpinionAssessment:
    """Append the operator's judgment of one review. Never edits an earlier
    assessment — a changed mind is a new row and the newest one wins."""
    row = SecondOpinionAssessment(
        review_uuid=review_uuid, assessment=assessment, note=note)
    db.session.add(row)
    db.session.commit()
    return row


def list_second_opinion_assessments(
    review_uuid: UUID,
) -> list[SecondOpinionAssessment]:
    """Every assessment of one review, oldest first — the operator's full
    trail of judgment, not just where they landed."""
    return list(
        db.session.query(SecondOpinionAssessment)
        .filter(SecondOpinionAssessment.review_uuid == review_uuid)
        .order_by(SecondOpinionAssessment.id)
        .all()
    )


def get_second_opinion_assessment(
    review_uuid: UUID,
) -> SecondOpinionAssessment | None:
    """The operator's current judgment of one review, or None if unassessed."""
    return (
        db.session.query(SecondOpinionAssessment)
        .filter(SecondOpinionAssessment.review_uuid == review_uuid)
        .order_by(SecondOpinionAssessment.id.desc())
        .first()
    )


_SECOND_OPINION_VERDICTS: tuple[str, ...] = (
    "approved", "rejected", "skipped", "error")


def list_second_opinion_reviews_page(
    *,
    verdict: str = "all",
    category: str = "all",
    assessed: str = "all",
    since: datetime | None = None,
    run_uuid: UUID | None = None,
    offset: int = 0,
    limit: int = 25,
) -> tuple[list[SecondOpinionReview], int, dict[str, int]]:
    """One page of reviews for the /second-opinion overview, newest first.

    Returns (rows, total, counts). `counts` is per-verdict over the same
    category/assessed/time window but ignoring the verdict filter itself, so
    the filter chips show what each choice would give rather than collapsing to
    the current selection.

    `assessed` is 'all' | 'yes' | 'no' — the operator works a backlog of
    unassessed reviews, so it has to be filterable, not just displayable.
    `run_uuid` scopes to one run (used by tests and a per-run drill-down).
    """
    def _base():
        q = db.session.query(SecondOpinionReview)
        if since is not None:
            q = q.filter(SecondOpinionReview.created_at >= since)
        if run_uuid is not None:
            q = q.filter(SecondOpinionReview.run_uuid == run_uuid)
        if category != "all":
            # ARRAY contains: the row lists this category among its findings.
            q = q.filter(SecondOpinionReview.categories.any(category))
        if assessed in ("yes", "no"):
            exists = (
                db.session.query(SecondOpinionAssessment)
                .filter(SecondOpinionAssessment.review_uuid
                        == SecondOpinionReview.uuid)
                .exists()
            )
            q = q.filter(exists if assessed == "yes" else ~exists)
        return q

    counts = {"all": _base().count()}
    for v in _SECOND_OPINION_VERDICTS:
        counts[v] = _base().filter(SecondOpinionReview.verdict == v).count()

    q = _base()
    if verdict != "all":
        q = q.filter(SecondOpinionReview.verdict == verdict)
    total = q.count()
    rows = (
        q.order_by(SecondOpinionReview.id.desc())
        .offset(max(0, offset)).limit(max(1, limit)).all()
    )
    return list(rows), total, counts


def second_opinion_assessments_for(
    review_uuids: list[UUID],
) -> dict[UUID, SecondOpinionAssessment]:
    """Each review's current (newest) assessment, for a page of rows in one
    query. Reviews with no assessment are absent from the mapping."""
    if not review_uuids:
        return {}
    rows = (
        db.session.query(SecondOpinionAssessment)
        .filter(SecondOpinionAssessment.review_uuid.in_(review_uuids))
        .order_by(SecondOpinionAssessment.id)
        .all()
    )
    # Ascending id, so a later row overwrites an earlier one: newest wins.
    return {r.review_uuid: r for r in rows}


def get_second_opinion_review(review_uuid: UUID) -> SecondOpinionReview | None:
    """One review by uuid, or None."""
    return (
        db.session.query(SecondOpinionReview)
        .filter(SecondOpinionReview.uuid == review_uuid)
        .first()
    )
