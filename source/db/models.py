import json
import logging
import os
import re
from datetime import UTC, datetime
from collections.abc import Callable
from typing import Any, Literal
from uuid import UUID, uuid4

import sqlalchemy as sa
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from pgvector.sqlalchemy import Vector

logger = logging.getLogger(__name__)

db = SQLAlchemy()

State = Literal["processing", "completed", "failed", "stopped"]
VALID_STATES: tuple[State, ...] = ("processing", "completed", "failed", "stopped")

# The operator's real database. Tests never use this — conftest.py forces the
# session onto rainbox_claude so a test can't touch production data. Override
# either with the DATABASE_URL env var.
DEFAULT_DATABASE_URL: str = "postgresql+psycopg://localhost/rainbox_production"

# Postgres LISTEN/NOTIFY channel the chat SSE stream subscribes to. A new
# chat message NOTIFYs this channel so connected browsers get pushed an event
# (no polling). Payload is a small JSON blob: {"room_uuid", "message_id", ...}.
CHAT_NOTIFY_CHANNEL: str = "chat_events"
# Postgres NOTIFY payloads are capped at ~8000 bytes. For streaming updates we
# inline the row's current text when it fits under this budget; past it, the
# notify omits the text and flags the browser to refetch that one row by id.
CHAT_NOTIFY_MAX_TEXT: int = 7000


class Inbox(db.Model):
    """Pending work for an agent: the input side of the queue.

    An inbox row is ephemeral — `take_item` (db.queue) pops the oldest row for
    an agent, deletes it, and opens a `Journal` row in 'processing'. So the
    `Inbox`/`Journal` pair tells the lifecycle at the operator level: Inbox is
    work *waiting to start*, Journal is *what happened to it* once taken.

    NAMING — no `Queue` prefix, on purpose. The package (`db.queue`) and its
    helpers (`enqueue`, `take_item`) already mark the subsystem; a `Queue*`
    prefix would only stutter ("an inbox is already a queue"). The bare,
    lifecycle-oriented pair `Inbox`/`Journal` reads better than a taxonomy
    prefix would, so these two skip the `Cron*`/`Kanban*`/`Memory*` convention
    that groups larger multi-table subsystems.
    """

    __tablename__ = "inbox"
    id: Mapped[int] = mapped_column(primary_key=True)
    agent_uuid: Mapped[UUID] = mapped_column()
    enqueued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    payload: Mapped[str] = mapped_column(Text)
    __table_args__ = (Index("inbox_by_agent", "agent_uuid", "id"),)


class ModelConfig(db.Model):
    __tablename__ = "model_config"
    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[UUID] = mapped_column(unique=True, default=uuid4)
    provider: Mapped[str] = mapped_column(Text, nullable=False, default="lm_studio")
    model_name: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(Text, default="")
    arguments: Mapped[dict] = mapped_column(JSONB, default=dict)
    available: Mapped[bool] = mapped_column(default=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    __table_args__ = (
        UniqueConstraint(
            "provider", "model_name",
            name="model_config_provider_model_name_key",
        ),
    )

    @property
    def effective_display_name(self) -> str:
        """The user-set display_name if any, otherwise the model_name. Mirrors
        ModelConfigOverride.effective_display_name so config rows can carry a
        friendly label without losing the raw model id as the fallback."""
        return self.display_name or self.model_name


class ModelConfigOverride(db.Model):
    __tablename__ = "model_config_override"
    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[UUID] = mapped_column(unique=True, default=uuid4)
    display_name: Mapped[str] = mapped_column(Text, default="")
    model_config_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("model_config.uuid", ondelete="RESTRICT"), index=True
    )
    overrides: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    model_config_ref: Mapped["ModelConfig"] = relationship(
        "ModelConfig",
        foreign_keys=[model_config_uuid],
        primaryjoin="ModelConfigOverride.model_config_uuid == ModelConfig.uuid",
        lazy="joined",
    )

    def _resolved_args(self) -> dict[str, Any]:
        """Base ModelConfig.arguments shallow-merged with self.overrides
        (override wins). Used by the label properties so the synthesized
        summary reflects the EFFECTIVE configuration, not just whichever
        keys this override happens to set explicitly."""
        base = (self.model_config_ref.arguments
                if self.model_config_ref is not None else {})
        return {**(base or {}), **(self.overrides or {})}

    @property
    def effective_display_name(self) -> str:
        """display_name if the user set one; otherwise a short summary derived
        from the resolved arguments (base ∪ override, e.g. "t0.5 c32k struct
        tool reasoning"). Used everywhere we render an override label, so
        unnamed overrides stay informative."""
        if self.display_name:
            return self.display_name
        return synthesize_override_label(self._resolved_args())

    @property
    def synthesized_label(self) -> str:
        """The auto-derived summary from the resolved arguments, always —
        regardless of whether display_name is set. Use this for placeholder
        hints (e.g. the rename input) so the user sees what the auto label
        would be even when they've set a custom display_name."""
        return synthesize_override_label(self._resolved_args())


def synthesize_override_label(arguments: dict[str, Any]) -> str:
    """Build a compact summary from a resolved ModelConfig.arguments-shaped
    dict (typically base ∪ override):
      - temperature → "t<value>"   (e.g. "t0.5")
      - context_window → "c<N>k"   ("c32k" for round multiples of 1024,
                                    "c3.8k" for non-round, "c512" for < 1024)
      - should_use_structured_outputs=True → "struct"
      - is_function_calling_model=True → "tool"
      - reasoning on → "reasoning", where "on" means either the OpenAI-compat
        additional_kwargs.extra_body.reasoning.effort ≠ "none" (LM Studio/Jan)
        or Ollama's native top-level `thinking` flag is truthy
    Empty dict → empty string (the caller decides on a placeholder)."""
    parts: list[str] = []
    temp = arguments.get("temperature")
    if temp is not None:
        parts.append(f"t{temp:g}")
    cw = arguments.get("context_window")
    if isinstance(cw, int):
        if cw >= 1024 and cw % 1024 == 0:
            parts.append(f"c{cw // 1024}k")
        elif cw >= 1024:
            parts.append(f"c{cw / 1024:.1f}k")
        else:
            parts.append(f"c{cw}")
    if arguments.get("should_use_structured_outputs"):
        parts.append("struct")
    if arguments.get("is_function_calling_model"):
        parts.append("tool")
    if args_reasoning_on(arguments):
        parts.append("reasoning")
    return " ".join(parts)


def args_reasoning_on(arguments: dict[str, Any]) -> bool:
    """Whether a resolved ModelConfig.arguments-shaped dict has reasoning on:
    either the OpenAI-compat additional_kwargs.extra_body.reasoning.effort is
    set and ≠ "none" (LM Studio/Jan), or Ollama's top-level `thinking` flag is
    truthy."""
    effort = (
        ((arguments.get("additional_kwargs") or {}).get("extra_body") or {})
        .get("reasoning", {})
        .get("effort")
    )
    return bool((effort and effort != "none") or arguments.get("thinking"))


class Journal(db.Model):
    """One unit of agent work (state + payload + result), the internal audit log
    of the queue.

    A journal row is the durable counterpart to an `Inbox` row: created when
    `take_item` pops the inbox entry, then mutated through its lifecycle —
    'processing' → 'completed'/'failed'/'stopped' — with the original payload,
    the result, timestamps, and routing status all preserved as the work's
    history. (For richer agents the result only *points* at a fuller per-step
    record, e.g. the `assistant_run`/`assistant_step` trace; the journal row is
    the queue-level record beneath those, not a peer of them.)

    NAMING — "journal" here means "durable record of work after it leaves the
    inbox," not a diary. The append-only connotation is a mild mismatch (the row
    mutates in place rather than being only appended to), but the local meaning
    is settled and load-bearing: `journal_id` is threaded across the codebase.
    It is deliberately NOT named `AgentRun`: that would read as a peer of the
    higher-level domain runs (`AssistantRun`, `ConversationRun`, `CronRun`,
    `EvalRun`) when it is actually the lower-level substrate underneath them —
    and not every such run maps 1:1 to a journal row.

    IDENTITY — `journal.id` is a UUID (the `journal_id` threaded everywhere is a
    UUID), so the id is globally unique and self-describing: a single `journal_id`
    grep'd from a log file or backup points at exactly this row without first
    knowing which table it came from, and it travels across process/payload
    boundaries unambiguously (no int collisions with other tables' PKs).

    Consequence: a random uuid is NOT monotonic, so "oldest first" ordering must
    use a timestamp (`started_at` / `enqueued_at`), never `id`. The queue helpers
    in `db.queue` order by `started_at`.
    """

    __tablename__ = "journal"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    inbox_id: Mapped[int | None] = mapped_column()
    agent_uuid: Mapped[UUID] = mapped_column()
    enqueued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(Text)
    payload: Mapped[str] = mapped_column(Text)
    result: Mapped[str | None] = mapped_column(Text)
    routed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint(
            "state IN ('processing','completed','failed','stopped')",
            name="journal_state_check",
        ),
        Index("journal_by_agent", "agent_uuid", "id"),
        Index("journal_unrouted", "state", "routed_at"),
    )


class CronFolder(db.Model):
    __tablename__ = "cron_folder"
    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[UUID] = mapped_column(unique=True, default=uuid4)
    name: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")  # notes about the child nodes
    parent_uuid: Mapped[UUID | None] = mapped_column(default=None)  # null = root; plain col, no FK
    enabled: Mapped[bool] = mapped_column(default=True)
    position: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    __table_args__ = (Index("cron_folder_children", "parent_uuid", "position"),)


class CronJob(db.Model):
    __tablename__ = "cron_job"
    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[UUID] = mapped_column(unique=True, default=uuid4)
    name: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(default=True)
    folder_uuid: Mapped[UUID | None] = mapped_column(default=None)  # null = unfiled; plain col, no FK
    cron_expr: Mapped[str] = mapped_column(Text, default="")
    timezone: Mapped[str] = mapped_column(Text, default="localtime")  # 'localtime' | 'UTC'
    action_type: Mapped[str] = mapped_column(Text, default="message")
    target: Mapped[str] = mapped_column(Text, default="")
    message: Mapped[str] = mapped_column(Text, default="")
    command: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    # Auto-retry on failure: refire (trigger='retry') up to this many times
    # after an error outcome, then wait for the next scheduled slot. 0 = off.
    max_retries: Mapped[int] = mapped_column(default=0)
    # Provenance: the assistant run+step that created this job (e.g. a reminder via
    # set_reminder). Null for manually-created jobs. Surfaced read-only on /cron.
    origin_run_uuid: Mapped[UUID | None] = mapped_column(default=None)
    origin_step_uuid: Mapped[UUID | None] = mapped_column(default=None)
    # firing-phase columns (unused until the scheduler lands)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    position: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    __table_args__ = (
        CheckConstraint("action_type IN ('message','command','backup','memory_sync')", name="cron_job_action_type_check"),
        Index("cron_job_in_folder", "folder_uuid", "position"),
    )


class CronRun(db.Model):
    """One row per firing — the "logs". The outcome lands on the same row:
    in-process actions (message/backup) set status synchronously in
    fire_cron_job; async command fires stay 'pending' until the workspace-shell
    agent writes back via cron_record_run_outcome (cron_tick sweeps runs whose
    completion never arrived, e.g. a killed agent, to 'error')."""
    __tablename__ = "cron_run"
    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[UUID] = mapped_column(unique=True, default=uuid4)
    cron_uuid: Mapped[UUID] = mapped_column()  # plain col, no FK
    trigger: Mapped[str] = mapped_column(Text, default="scheduled")
    debug: Mapped[bool] = mapped_column(default=False)
    fired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    status: Mapped[str] = mapped_column(Text, default="pending")
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    error: Mapped[str] = mapped_column(Text, default="")
    journal_id: Mapped[UUID | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    __table_args__ = (
        CheckConstraint("status IN ('pending','ok','error')", name="cron_run_status_check"),
        Index("cron_run_by_job", "cron_uuid", "id"),
    )


class KanbanBoard(db.Model):
    """A kanban board: the database-backed coordination surface agents and
    humans share (docs/plan.md "Kanban board" — chosen over markdown todo
    lists precisely because reliable mutation needs uuid-addressed rows, not
    document editing). Columns/tasks reference boards by plain uuid columns
    (no FKs, app-side validation — the cron-tables pattern)."""

    __tablename__ = "kanban_board"
    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[UUID] = mapped_column(unique=True, default=uuid4)
    name: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    folder_uuid: Mapped[UUID | None] = mapped_column(default=None)  # null = unfiled/root; plain col, no FK
    position: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    # No index on folder_uuid: the column is ALTER-added to the pre-existing
    # kanban_board table, and create_all() never indexes an existing table, so
    # an __table_args__ index would exist only on fresh DBs (dev/prod drift).
    # Boards are few; the scan is cheap. Mirrors the index-less Chatroom.folder_uuid.


class KanbanBoardFolder(db.Model):
    """An organizational folder in the /kanban left-panel tree (folders →
    boards). Purely organizational: boards reference a folder by a plain
    `folder_uuid` column and folders nest via `parent_uuid` — both plain uuid
    columns with no FK (the cron/chat folder pattern; app-side validation in
    validate_kanban_tree catches dangling/cyclic refs)."""

    __tablename__ = "kanban_board_folder"
    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[UUID] = mapped_column(unique=True, default=uuid4)
    name: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    parent_uuid: Mapped[UUID | None] = mapped_column(default=None)  # null = root; plain col, no FK
    position: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    __table_args__ = (Index("kanban_board_folder_children", "parent_uuid", "position"),)


class KanbanColumn(db.Model):
    __tablename__ = "kanban_column"
    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[UUID] = mapped_column(unique=True, default=uuid4)
    board_uuid: Mapped[UUID] = mapped_column()  # plain col, no FK
    name: Mapped[str] = mapped_column(Text, default="")
    position: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    __table_args__ = (Index("kanban_column_by_board", "board_uuid", "position"),)


class KanbanTask(db.Model):
    """A task on a board. `agent_uuid` is the agent RESPONSIBLE for the action
    (the assignee; agent_config uuid — stable across role renames; NULL =
    unassigned). The claim/lease trio is the CURRENT WORKER: `claimed_by`
    holds the lease until `claim_expires_at`; an expired lease makes the task
    claimable again, so a crashed agent can't own a task forever. Assignment
    is set by humans (bulk save); the lease only by the claim operations."""

    __tablename__ = "kanban_task"
    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[UUID] = mapped_column(unique=True, default=uuid4)
    board_uuid: Mapped[UUID] = mapped_column()   # plain col, no FK
    column_uuid: Mapped[UUID] = mapped_column()  # plain col, no FK
    title: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    agent_uuid: Mapped[UUID | None] = mapped_column(default=None)
    claimed_by: Mapped[UUID | None] = mapped_column(default=None)
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    claim_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    position: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    __table_args__ = (Index("kanban_task_by_column", "column_uuid", "position"),)


class KanbanTaskEvent(db.Model):
    """Append-only audit trail per task: who did what, when (claimed, moved,
    done, failed, note, …). Agent operations and the bulk UI save both append
    here, so an LLM (or the operator) can reconstruct a task's history."""

    __tablename__ = "kanban_task_event"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_uuid: Mapped[UUID] = mapped_column()  # plain col, no FK
    kind: Mapped[str] = mapped_column(Text, default="note")
    # Who acted: an agent uuid, "human", or another short label.
    actor: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    __table_args__ = (Index("kanban_event_by_task", "task_uuid", "id"),)


# Dedicated chat surface for cron events (firings, errors). Both uuids are fixed
# so the room/sender are stable across restarts. The sender is a plain agent-type
# chat_user that is NOT in agent_config, so the supervisor never processes it —
# it only exists as the author of event lines. See seed_chat_defaults /
# post_cron_event.
# Random-looking (not near-zero) so their short forms are distinguishable in
# uuid columns across the UI.
CRON_ROOM_UUID = UUID("404210fa-9c95-4f62-960e-db57faa37203")
CRON_SYSTEM_UUID = UUID("b91c42e4-8689-4b1c-afab-2d10bcede332")
CRON_SYSTEM_NAME = "cron"


# The three states a per-capability membership constraint can take. "dont_care"
# imposes no constraint; "must_have"/"must_not_have" admit only members that
# resolve to the capability being True/False respectively.
CAPABILITY_CONSTRAINTS: tuple[str, ...] = ("dont_care", "must_have", "must_not_have")


class ModelGroup(db.Model):
    __tablename__ = "model_group"
    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[UUID] = mapped_column(unique=True, default=uuid4)
    name: Mapped[str] = mapped_column(Text)
    # Per-capability membership constraints, each a tri-state (see
    # CAPABILITY_CONSTRAINTS): "dont_care" imposes no constraint, "must_have"
    # admits only members that resolve to the capability=True, "must_not_have"
    # admits only members that resolve to the capability=False.
    function_calling_constraint: Mapped[str] = mapped_column(
        Text, default="dont_care"
    )
    structured_output_constraint: Mapped[str] = mapped_column(
        Text, default="dont_care"
    )
    reasoning_constraint: Mapped[str] = mapped_column(Text, default="dont_care")

    # Back-compat shims: callers that only ask "is this group guaranteed to
    # support capability X?" (agents binding to a group, the agent↔group
    # compatibility check) keep reading these booleans. Only "must_have" counts
    # as guaranteed; "must_not_have" and "dont_care" both read as not-required.
    @property
    def requires_function_calling(self) -> bool:
        return self.function_calling_constraint == "must_have"

    @property
    def requires_structured_output(self) -> bool:
        return self.structured_output_constraint == "must_have"

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class ModelGroupMember(db.Model):
    """One entry in a ModelGroup's ordered list. member_uuid references either
    a ModelConfig.uuid or a ModelConfigOverride.uuid (resolved override-first,
    same as the /models?id= lookup). No hard FK on member_uuid because it's a
    polymorphic reference across two tables."""

    __tablename__ = "model_group_member"
    id: Mapped[int] = mapped_column(primary_key=True)
    group_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("model_group.uuid", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column()
    member_uuid: Mapped[UUID] = mapped_column()
    __table_args__ = (Index("model_group_member_order", "group_uuid", "position"),)


class AgentModelBinding(db.Model):
    """Binds a code-defined agent (from agent_config.py) to a ModelGroup.

    Agents and their topology stay in code; this table only records which model
    group each agent runs. A group is a prioritized fallback list (try the first
    model, fall back to the next on failure) — e.g. a fast/low-quality group vs a
    slow/high-quality one. model_group_uuid is nullable until a group is
    assigned; ON DELETE SET NULL unassigns the agent if its group is deleted."""

    __tablename__ = "agent_model_binding"
    id: Mapped[int] = mapped_column(primary_key=True)
    agent_uuid: Mapped[UUID] = mapped_column(unique=True)
    model_group_uuid: Mapped[UUID | None] = mapped_column(
        ForeignKey("model_group.uuid", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class ChatUser(db.Model):
    """A participant in the group chat. Exactly two kinds exist: a single
    `human` operator (this demo has no sign-up/auth — there is always one
    human), and one `agent` per code-defined agent in agent_config.py (the
    agent rows reuse those agents' uuids/names so "agent uuid" is consistent
    across the app)."""

    __tablename__ = "chat_user"
    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[UUID] = mapped_column(unique=True, default=uuid4)
    name: Mapped[str] = mapped_column(Text)
    user_type: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    __table_args__ = (
        CheckConstraint("user_type IN ('human','agent')", name="chat_user_type_check"),
    )


class Chatroom(db.Model):
    __tablename__ = "chatroom"
    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[UUID] = mapped_column(unique=True, default=uuid4)
    name: Mapped[str] = mapped_column(Text)
    created_by: Mapped[UUID] = mapped_column()  # chat_user.uuid (the human)
    # Left-panel folder placement (mirrors cron's folder tree). null = top level;
    # plain col, no FK (house style — app-side validation). `position` orders
    # rooms within their folder (or among top-level rooms).
    folder_uuid: Mapped[UUID | None] = mapped_column(default=None)
    position: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class ChatroomFolder(db.Model):
    """A left-panel folder grouping chatrooms (and other folders). Mirrors
    CronFolder minus the scheduling-only fields (description/enabled): chat
    folders are purely organizational. parent_uuid is a plain uuid column (no
    FK, app-side validation — the cron/kanban house style); null = root.
    position orders folders within their parent."""

    __tablename__ = "chatroom_folder"
    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[UUID] = mapped_column(unique=True, default=uuid4)
    name: Mapped[str] = mapped_column(Text, default="")
    parent_uuid: Mapped[UUID | None] = mapped_column(default=None)  # null = root; plain col, no FK
    position: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    __table_args__ = (Index("chatroom_folder_children", "parent_uuid", "position"),)


class ChatroomMember(db.Model):
    """Explicit room membership. A room is "between" its members: the human
    plus whichever agents were chosen at creation."""

    __tablename__ = "chatroom_member"
    id: Mapped[int] = mapped_column(primary_key=True)
    room_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("chatroom.uuid", ondelete="CASCADE"), index=True
    )
    user_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("chat_user.uuid", ondelete="CASCADE")
    )
    __table_args__ = (
        Index("chatroom_member_unique", "room_uuid", "user_uuid", unique=True),
    )


class ChatMessage(db.Model):
    """A message in a room. `id` (autoincrement) doubles as the ordering /
    incremental-fetch cursor: clients ask for messages `after` their last id.
    sender_uuid references chat_user.uuid (kept FK-less, matching the
    Inbox/Journal agent_uuid style)."""

    __tablename__ = "chat_message"
    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[UUID] = mapped_column(unique=True, default=uuid4)
    room_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("chatroom.uuid", ondelete="CASCADE"), index=True
    )
    sender_uuid: Mapped[UUID] = mapped_column()
    text: Mapped[str] = mapped_column(Text)
    # How `text` should be interpreted: "markdown" or "json". Agent messages
    # carry their declared reply_format; human messages are auto-classified.
    content_type: Mapped[str] = mapped_column(Text, default="markdown")
    # What role this message plays in the room (orthogonal to content_type):
    #   "message"      — a real, user-facing chat message (default)
    #   "thinking"     — an agent's thought process / intermediate reasoning
    #   "debug-router" — the router agent's {subject, action} triage output
    # The UI can fold away non-"message" rows so they don't clutter the chat.
    kind: Mapped[str] = mapped_column(Text, default="message")
    # Structured attachment for interactive messages (default {}). A confirm-tier
    # write proposal stores {write_intent, capability, step_link} so chat can render
    # confirm/reject controls; list_room_messages splices in the intent's live state.
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)
    # True while a row is being streamed token-by-token (its `text` grows in
    # place via update_chat_message). Flipped to False on the final flush. The UI
    # shows a live cursor and withholds feedback buttons while True.
    streaming: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    __table_args__ = (Index("chat_message_by_room", "room_uuid", "id"),)


class ConversationRun(db.Model):
    """Live, bounded state for one persona-to-persona conversation, driven by the
    ConversationManagerAgent. The transcript stays in `chat_message`; this row is
    the only mutable runtime state the feature adds. The two compare-and-set
    guards (`tick_count` for manual ticks, `last_speaker_journal_id`/`turn`/
    `active_turn` for routed completions) keep the turn loop idempotent under
    double-delivery and restarts."""

    __tablename__ = "conversation_run"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    room_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("chatroom.uuid", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(Text, default="running")
    turn: Mapped[int] = mapped_column(default=0)
    tick_count: Mapped[int] = mapped_column(default=0)
    participants: Mapped[list] = mapped_column(JSONB, default=list)
    turn_policy: Mapped[dict] = mapped_column(JSONB, default=dict)
    last_speaker_journal_id: Mapped[UUID | None] = mapped_column(nullable=True)
    active_turn: Mapped[int | None] = mapped_column(nullable=True)
    active_speaker_uuid: Mapped[UUID | None] = mapped_column(nullable=True)
    active_turn_enqueued_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_human_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    retry_count: Mapped[int] = mapped_column(default=0)
    stop_requested: Mapped[bool] = mapped_column(default=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    budget: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    __table_args__ = (
        CheckConstraint(
            "status IN ('running','paused','finished','failed','stopped')",
            name="conversation_run_status_check",
        ),
    )


class WorkspaceShellState(db.Model):
    """Per-room persisted state for the workspace_shell agent: the working
    directory (and an env column, kept at the fixed baseline), so `cd` survives
    between messages (and restarts) even though the agent process is spawned
    fresh per message."""

    __tablename__ = "workspace_shell_state"
    room_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("chatroom.uuid", ondelete="CASCADE"), primary_key=True
    )
    cwd: Mapped[str] = mapped_column(Text)
    env: Mapped[dict] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class MemoryClaim(db.Model):
    """A first-class memory entry: what the system believes, optionally
    structured as (subject, predicate, object), with explicit scope,
    kind, lifecycle status, and sensitivity. Evidence rows
    (memory_evidence) carry provenance — see add_memory_evidence."""

    __tablename__ = "memory_claim"
    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[UUID] = mapped_column(unique=True, default=uuid4)
    agent_uuid: Mapped[UUID | None] = mapped_column()
    scope: Mapped[str] = mapped_column(Text)
    room_uuid: Mapped[UUID | None] = mapped_column()
    kind: Mapped[str] = mapped_column(Text)
    subject: Mapped[str | None] = mapped_column(Text)
    predicate: Mapped[str | None] = mapped_column(Text)
    object: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column()
    status: Mapped[str] = mapped_column(Text)
    sensitivity: Mapped[str] = mapped_column(Text)
    supersedes_uuid: Mapped[UUID | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    conflicts_with_uuid: Mapped[UUID | None] = mapped_column()
    epistemic_confidence: Mapped[float | None] = mapped_column()
    retrieval_strength: Mapped[float | None] = mapped_column()
    support_count: Mapped[int | None] = mapped_column()
    subj_pred_key: Mapped[str | None] = mapped_column(Text)
    value_key: Mapped[str | None] = mapped_column(Text)
    key_version: Mapped[int | None] = mapped_column()
    __table_args__ = (
        CheckConstraint(
            "scope IN ('global','agent','room','project')",
            name="memory_claim_scope_check",
        ),
        CheckConstraint(
            "kind IN ('fact','preference','project_decision','procedure','episode_summary')",
            name="memory_claim_kind_check",
        ),
        CheckConstraint(
            "status IN ('candidate','active','superseded','rejected','expired')",
            name="memory_claim_status_check",
        ),
        CheckConstraint(
            "sensitivity IN ('public','private','secret')",
            name="memory_claim_sensitivity_check",
        ),
        CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="memory_claim_confidence_range",
        ),
        Index("memory_claim_by_scope", "scope", "status"),
    )


class MemoryEvidence(db.Model):
    """Provenance row for a MemoryClaim. Multiple evidence rows can attach
    to the same claim (e.g. an inferred claim that later gains a
    confirmed_by_user row). FK CASCADEs on claim deletion."""

    __tablename__ = "memory_evidence"
    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[UUID] = mapped_column(unique=True, default=uuid4)
    memory_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("memory_claim.uuid", ondelete="CASCADE"), index=True
    )
    provenance: Mapped[str] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(Text)
    source_id: Mapped[str | None] = mapped_column(Text)
    excerpt: Mapped[str | None] = mapped_column(Text)
    created_by_uuid: Mapped[UUID | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    __table_args__ = (
        CheckConstraint(
            "provenance IN ('observed_from_source','inferred_by_model',"
            "'confirmed_by_user','imported_from_transcript')",
            name="memory_evidence_provenance_check",
        ),
        CheckConstraint(
            "source_type IN ('chat_message','journal','file','api',"
            "'manual','transcript')",
            name="memory_evidence_source_type_check",
        ),
    )


class MemoryRejectedValue(db.Model):
    """A tombstone: a (scope, subject/predicate, value) that was rejected or
    superseded and must not silently return. Snapshots the rejected claim's text
    and evidence metadata so a later suppression is explainable even if the
    original claim/evidence rows change."""

    __tablename__ = "memory_rejected_value"
    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[UUID] = mapped_column(unique=True, default=uuid4)
    scope: Mapped[str] = mapped_column(Text)
    agent_uuid: Mapped[UUID | None] = mapped_column()
    room_uuid: Mapped[UUID | None] = mapped_column()
    subj_pred_key: Mapped[str] = mapped_column(Text)
    value_key: Mapped[str] = mapped_column(Text)
    claim_text: Mapped[str] = mapped_column(Text)
    evidence_summary: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    created_from_uuid: Mapped[UUID | None] = mapped_column()
    created_by_uuid: Mapped[UUID | None] = mapped_column()
    hit_count: Mapped[int] = mapped_column(default=0)
    last_hit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC))
    __table_args__ = (
        CheckConstraint("scope IN ('global','agent','room','project')",
                        name="memory_rejected_value_scope_check"),
    )


class FeedbackEvent(db.Model):
    """Captured user feedback on a user-facing agent reply.

    Snapshot-style metadata: enough context (rated message text, prior
    human message, latest debug-memory/debug-query payload) to
    reconstruct an eval case later without depending on chat_message
    rows surviving."""

    __tablename__ = "feedback_event"
    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[UUID] = mapped_column(unique=True, default=uuid4)
    room_uuid: Mapped[UUID] = mapped_column(index=True)
    message_uuid: Mapped[UUID] = mapped_column(index=True)
    agent_uuid: Mapped[UUID] = mapped_column()
    rating: Mapped[str] = mapped_column(Text)
    comment: Mapped[str | None] = mapped_column(Text)
    created_by_uuid: Mapped[UUID | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    # `metadata` is reserved on DeclarativeBase; map a Python alias.
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    __table_args__ = (
        CheckConstraint(
            "rating IN ('upvote','downvote')",
            name="feedback_event_rating_check",
        ),
    )


class RetrievalEvent(db.Model):
    """Event-row record of one retrieval-pipeline decision affecting one
    candidate (qa entry or memory claim). Event-row source of truth per
    docs/relevance-telemetry.md — counters and rollups are derived, never
    primary."""

    __tablename__ = "retrieval_event"
    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[UUID] = mapped_column(unique=True, default=uuid4)
    target_type: Mapped[str] = mapped_column(Text)
    target_id: Mapped[str] = mapped_column(Text)
    stage: Mapped[str] = mapped_column(Text)
    query: Mapped[str | None] = mapped_column(Text)
    room_uuid: Mapped[UUID | None] = mapped_column()
    agent_uuid: Mapped[UUID | None] = mapped_column()
    journal_id: Mapped[UUID | None] = mapped_column()
    source: Mapped[str | None] = mapped_column(Text)
    retrieval_rank: Mapped[int | None] = mapped_column()
    retrieval_score: Mapped[float | None] = mapped_column()
    filter_label: Mapped[str | None] = mapped_column(Text)
    # `metadata` is reserved on DeclarativeBase; map a Python alias.
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    __table_args__ = (
        CheckConstraint(
            "target_type IN ('qa_entry','memory_claim','skill')",
            name="ck_retrieval_event_target_type",
        ),
        CheckConstraint(
            "stage IN ('retrieved','accepted','rejected','used','downvoted',"
            "'considered','injected')",
            name="ck_retrieval_event_stage",
        ),
        CheckConstraint(
            "filter_label IS NULL OR filter_label IN "
            "('relevant','irrelevant','unknown')",
            name="ck_retrieval_event_filter_label",
        ),
        Index(
            "ix_retrieval_event_target_stage",
            "target_type", "target_id", "stage",
        ),
        Index("ix_retrieval_event_created_at", "created_at"),
    )


class EvalCase(db.Model):
    """An eval case promoted from feedback (or hand-authored).

    `input` / `expected` / `rubric` are JSONB blobs editable in
    Flask-Admin. `source_feedback_uuid` carries provenance back to the
    FeedbackEvent if the case was promoted; null for hand-authored
    cases."""

    __tablename__ = "eval_case"
    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[UUID] = mapped_column(unique=True, default=uuid4)
    source_feedback_uuid: Mapped[UUID | None] = mapped_column(index=True)
    name: Mapped[str] = mapped_column(Text)
    case_type: Mapped[str] = mapped_column(Text)
    split: Mapped[str] = mapped_column(Text)
    input: Mapped[dict] = mapped_column(JSONB, default=dict)
    expected: Mapped[dict] = mapped_column(JSONB, default=dict)
    rubric: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    __table_args__ = (
        CheckConstraint(
            "case_type IN ('chat_reply','memory_retrieval',"
            "'query_answer','tool_output')",
            name="eval_case_case_type_check",
        ),
        CheckConstraint(
            "split IN ('train','holdout','regression')",
            name="eval_case_split_check",
        ),
        CheckConstraint(
            "status IN ('candidate','active','archived')",
            name="eval_case_status_check",
        ),
    )


class EvalRun(db.Model):
    """One execution of a set of eval cases. `config` records the filter
    used (split, status, explicit case_uuids…) so the run is reproducible.
    `summary` is filled in by `finish_eval_run` once all results have
    been collected."""

    __tablename__ = "eval_run"
    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[UUID] = mapped_column(unique=True, default=uuid4)
    name: Mapped[str] = mapped_column(Text)
    agent_role: Mapped[str] = mapped_column(Text)
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[dict] = mapped_column(JSONB, default=dict)
    is_baseline: Mapped[bool] = mapped_column(default=False, server_default=sa.false())


class EvalResult(db.Model):
    """One eval case's outcome inside a single eval run."""

    __tablename__ = "eval_result"
    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[UUID] = mapped_column(unique=True, default=uuid4)
    eval_run_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("eval_run.uuid", ondelete="CASCADE"), index=True
    )
    eval_case_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("eval_case.uuid", ondelete="CASCADE"), index=True
    )
    score: Mapped[float] = mapped_column()
    passed: Mapped[bool] = mapped_column()
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    __table_args__ = (
        CheckConstraint(
            "score >= 0.0 AND score <= 1.0",
            name="eval_result_score_range",
        ),
    )


class AssistantRun(db.Model):
    """One execution of the AssistantAgent's bounded ReAct loop.

    The durable, queryable source of truth for an assistant turn (journal.result
    holds only a short summary; chat rows hold only thin inline pointers). One
    row per handle() call; its assistant_step children are the step trace.

    Identity is the `uuid` (the primary key) — the same token shown in the
    `/assistant` inspector and grep'd from logs. Children reference it via
    `run_uuid`. There is no integer surrogate id.
    """

    __tablename__ = "assistant_run"
    uuid: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    journal_id: Mapped[UUID | None] = mapped_column(index=True)
    room_uuid: Mapped[UUID] = mapped_column()
    agent_uuid: Mapped[UUID] = mapped_column()
    status: Mapped[str] = mapped_column(Text, default="running")
    step_limit: Mapped[int] = mapped_column(default=6)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    final_summary: Mapped[str | None] = mapped_column(Text)
    # Run/model diagnostics; empty by default (NOT the step trace).
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    # A post-completion digest produced by the assistant_run_summarizer agent (off the
    # critical path): {trigger, obstacles[], outcome, summarized_at}. NULL until
    # summarized; never blocks the run.
    summary: Mapped[dict | None] = mapped_column(JSONB)
    __table_args__ = (
        CheckConstraint(
            "status IN ('running','stopping','finished','stopped','failed','killed')",
            name="assistant_run_status_check",
        ),
        Index("assistant_run_by_room", "room_uuid", "started_at"),
    )


class AssistantStep(db.Model):
    """One logical step in an assistant run, as **a single mutable row**. A normal
    action step is inserted at `phase="running"` (so a crash mid-action leaves a
    durable row) and then UPDATEd in place to its terminal `phase`
    (`observed`/`failed`). Terminal-only steps — a `failed` validation, the
    `final` reply, and `control` (stop/redirect) events — are a single insert.

    `phase` is therefore the step's *current state*, not a per-transition log.
    Its uuid is stable for the step's whole life, so `assistant_write_intent`
    references the producing step by `step_uuid`. `(run_id, step_index)` is
    one-to-one per non-control step but is not DB-unique (legacy rows from the
    former append-only design predate the single-row invariant, which is now
    code-enforced via the open/settle helpers in db.assistant).

    `legacy` note: older runs may still have multiple rows per (run_id,
    step_index); readers order by id / created_at and tolerate them.
    """

    __tablename__ = "assistant_step"
    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[UUID] = mapped_column(unique=True, default=uuid4)
    run_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("assistant_run.uuid", ondelete="CASCADE"), index=True
    )
    step_index: Mapped[int] = mapped_column()
    phase: Mapped[str] = mapped_column(Text)
    action: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    args: Mapped[dict] = mapped_column(JSONB, default=dict)
    # The exact prompt sent to the model for this step's decide call (the
    # "model request" half of the LLM interaction); NULL for control steps and
    # legacy rows that predate prompt capture.
    system_prompt: Mapped[str | None] = mapped_column(Text)
    user_prompt: Mapped[str | None] = mapped_column(Text)
    observation_preview: Mapped[str | None] = mapped_column(Text)
    # The full AssistantObservation the action returned, as {ok, text, data} —
    # the authoritative "function result" record, so `ok` need not be inferred
    # from `phase`. observation_preview stays the capped, model/trace-facing
    # text. NULL on legacy rows that predate capture.
    observation: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    model_group_uuid: Mapped[UUID | None] = mapped_column()
    model_uuid: Mapped[UUID | None] = mapped_column()
    # Token counts of this step's LLM (decide) call; NULL when not captured — a
    # `control` step, a crash before the call returned, or a provider that
    # reported no usage.
    input_tokens: Mapped[int | None] = mapped_column()
    output_tokens: Mapped[int | None] = mapped_column()
    # Wall-clock duration of this step's LLM (decide) call, in milliseconds.
    duration_ms: Mapped[int | None] = mapped_column()
    # When the decide LLM request was sent (the "model request" time); the
    # response arrived ~duration_ms later, at which point the row is created.
    # NULL on legacy rows that predate capture.
    requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When the row was created — right after the model response returned, so it
    # doubles as the "model response" / "function call" invocation time.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    # When the step settled — i.e. the action returned and the observation was
    # recorded (the "function result" time). NULL until settled, and on
    # single-insert terminal rows (final/failed-validation/control) that never
    # settle.
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint(
            "phase IN ('planned','running','observed','failed','final','control')",
            name="assistant_step_phase_check",
        ),
        Index("assistant_step_by_run", "run_uuid", "step_index", "id"),
        Index("assistant_step_by_action_phase", "action", "phase"),
        Index("assistant_step_by_created", "created_at"),
    )


class AssistantControl(db.Model):
    """An operator steering command for an in-flight assistant run. The loop
    polls pending controls at each step boundary: `stop` ends the run cleanly,
    `redirect` injects a new instruction before the next step. A new table (not a
    chat row) because controls are runtime state, not conversation.
    """

    __tablename__ = "assistant_control"
    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[UUID] = mapped_column(unique=True, default=uuid4)
    run_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("assistant_run.uuid", ondelete="CASCADE"), index=True
    )
    command: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    state: Mapped[str] = mapped_column(Text, default="pending")
    requested_by_uuid: Mapped[UUID | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        CheckConstraint(
            "command IN ('stop','redirect')", name="ck_assistant_control_command"
        ),
        CheckConstraint(
            "state IN ('pending','applied','ignored')",
            name="ck_assistant_control_state",
        ),
        Index("assistant_control_by_run_state", "run_uuid", "state", "id"),
    )


class AssistantWriteIntent(db.Model):
    """A confirm-tier write the assistant proposed but must not execute until the
    operator approves it. The payload is bound by `payload_hash` so a confirmed
    intent executes exactly what was previewed — the assistant cannot mutate it
    after confirmation. Log-and-undo writes do not use this table.

    State machine: proposed -> confirmed -> executing -> completed | failed, with
    rejected (operator declined) and undone (a completed write was reverted) as
    additional terminal states.
    """

    __tablename__ = "assistant_write_intent"
    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[UUID] = mapped_column(unique=True, default=uuid4)
    run_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("assistant_run.uuid", ondelete="CASCADE"), index=True
    )
    # The step that produced this intent — the sole pointer (assistant_step is
    # one mutable row per step, so its uuid is stable). Nullable only because
    # legacy intents predate the FK; every intent the loop writes sets it.
    step_uuid: Mapped[UUID | None] = mapped_column(
        ForeignKey("assistant_step.uuid", ondelete="SET NULL"), index=True
    )
    capability_name: Mapped[str] = mapped_column(Text)
    payload_hash: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    preview_text: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, default="proposed")
    room_uuid: Mapped[UUID] = mapped_column()
    agent_uuid: Mapped[UUID] = mapped_column()
    result: Mapped[dict] = mapped_column(JSONB, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_by_uuid: Mapped[UUID | None] = mapped_column()
    __table_args__ = (
        CheckConstraint(
            "state IN ('proposed','confirmed','executing','completed','failed',"
            "'rejected','undone')",
            name="ck_assistant_write_intent_state",
        ),
        Index("assistant_write_intent_by_run", "run_uuid", "id"),
    )


class MemoryEmbedding(db.Model):
    """A vector embedding of an active memory claim — the rainbox-owned half of
    hybrid retrieval (the Q&A table is owned by LlamaIndex's PGVectorStore).

    Kept separate from `memory_claim` so the claim stays readable in Flask-Admin,
    multiple embedding models/text-hashes can coexist during rebuilds, and a
    failed embedding never corrupts the source row. A claim with no row here
    falls back to lexical-only retrieval — never an error.
    """

    __tablename__ = "memory_embedding"
    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[UUID] = mapped_column(unique=True, default=uuid4)
    memory_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("memory_claim.uuid", ondelete="CASCADE"), index=True
    )
    model_name: Mapped[str] = mapped_column(Text)
    embed_dim: Mapped[int] = mapped_column()
    text_hash: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(768))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    __table_args__ = (
        UniqueConstraint(
            "memory_uuid", "model_name", "text_hash", name="uq_memory_embedding"
        ),
    )


class _ExternalTableBase(DeclarativeBase):
    """Declarative base for tables managed by *other* tools (e.g. LlamaIndex's
    PGVectorStore). Kept separate from `db.Model` so `db.create_all()` ignores
    these — the owning tool creates and migrates them."""


class SeedMemoryKb(_ExternalTableBase):
    """Maps the `data_seed_memory` table that LlamaIndex's PGVectorStore
    creates for curated seed-memory Q&A retrieval. Read-only via Flask-Admin;
    the `embedding` pgvector column is intentionally not declared because
    Flask-Admin can't render vectors and SQLAlchemy SELECTs only the columns
    listed here."""

    __tablename__ = "data_seed_memory"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    metadata_: Mapped[dict] = mapped_column(sa.JSON)
    node_id: Mapped[str] = mapped_column(Text)


class AppSetting(db.Model):
    """Operator-set configuration, addressed by `key` (e.g. "backup.repo").

    `value` is always stored as text (NULL = unset). `value_type`, `secret`, and
    `description` are a *seeded cache* of the code-side registry in db.settings
    (reconciled on startup) — never an independent source of truth. No `uuid`:
    rows are addressed by `key` and never FK-referenced or deep-linked. See
    docs/proposals/2026-06-07-user-configuration-in-postgres.md."""

    __tablename__ = "app_setting"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(Text, unique=True, index=True)
    value: Mapped[str | None] = mapped_column(Text, default=None)
    value_type: Mapped[str] = mapped_column(Text, default="string")  # string|bool|int|json
    description: Mapped[str] = mapped_column(Text, default="")
    secret: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class GitFolder(db.Model):
    __tablename__ = "git_folder"
    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[UUID] = mapped_column(unique=True, default=uuid4)
    name: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")  # notes about the child nodes
    parent_uuid: Mapped[UUID | None] = mapped_column(default=None)  # null = root; plain col, no FK
    position: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    __table_args__ = (Index("git_folder_children", "parent_uuid", "position"),)


class GitRepo(db.Model):
    __tablename__ = "git_repo"
    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[UUID] = mapped_column(unique=True, default=uuid4)
    name: Mapped[str] = mapped_column(Text, default="")  # RainBox display name; freely editable
    folder_uuid: Mapped[UUID | None] = mapped_column(default=None)  # null = unfiled at root; plain col, no FK
    path: Mapped[str] = mapped_column(Text, default="")  # absolute filesystem path; set at add, immutable for now
    description: Mapped[str] = mapped_column(Text, default="")
    position: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    __table_args__ = (Index("git_repo_in_folder", "folder_uuid", "position"),)


def psycopg_dsn() -> str:
    """The DATABASE_URL as a plain libpq DSN (no SQLAlchemy `+psycopg` driver
    tag), for opening a raw psycopg connection — used by the chat SSE stream to
    LISTEN on the notify channel."""
    url = os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    return url.replace("postgresql+psycopg://", "postgresql://", 1)
