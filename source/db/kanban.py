"""Kanban persistence + agent operations.

The kanban board is a database-backed coordination primitive (docs/plan.md):
agents keep track of progress here because editing markdown todo lists is too
fragile for small models — instead of "rewrite the document correctly", an
agent calls narrow, uuid-addressed operations (claim / move / append event /
complete) that either succeed atomically or fail loudly. Those primitives are
deliberately mechanism-agnostic: they work equally as function-calling tools
or as fields of a structured-output reply (the undecided question in
docs/kanban-design.md), because the DB layer is the same either way.

Two write surfaces:
- **Bulk per-board save** (`kanban_save_board`) for the /kanban page — guarded
  exactly like the cron tree PUT: payload validation before any mutation, an
  optimistic-concurrency version token (stale → KanbanConflict → 409), and a
  declared-deletes tripwire so a truncated payload can't wipe a board. The UI
  save also appends 'created'/'moved' audit events for changes it detects.
- **Per-task agent operations** for LLM/agent callers — each one row-level,
  validated, and recorded in the kanban_task_event audit trail.

Reads: `kanban_load_board` (the wire format the page hydrates from) and
`kanban_board_markdown` — the canonical LLM-facing serialization, generated
server-side from DB state with spoof-resistant escaping (task text cannot
forge headings/bullets/uuids at the structural level).

Re-exported from db for import compatibility.
"""
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa

from db.models import (KanbanBoard, KanbanBoardFolder, KanbanColumn,
                       KanbanTask, KanbanTaskEvent, db)

KANBAN_DEFAULT_COLUMNS = ("To do", "In progress", "Done")

# How long a claim holds before it expires and the task becomes claimable
# again. Expiry is evaluated at read time (no sweep needed): a crashed or
# stalled agent simply stops renewing, and after this window any claimer —
# including another instance of the same agent — can take the task over.
# A working agent renews via kanban_renew_claim.
KANBAN_CLAIM_LEASE = timedelta(minutes=15)


class KanbanError(ValueError):
    """A kanban payload/operation failed validation (bad uuid, dangling
    column reference, wrong type). Callers map this to a 400."""


class KanbanConflict(Exception):
    """Optimistic-concurrency or claim conflict (stale board version; task
    already claimed by another agent). Callers map this to a 409."""


def _to_uuid(value: Any) -> UUID | None:
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


# ---- boards: create / delete / list / load / version ----

def kanban_create_board(name: str, description: str = "") -> dict[str, Any]:
    """Create a board with the default columns; returns the load payload."""
    if not isinstance(name, str) or not name.strip():
        raise KanbanError("board name is required")
    position = db.session.execute(
        sa.select(sa.func.coalesce(sa.func.max(KanbanBoard.position), -1))
    ).scalar_one() + 1
    board = KanbanBoard(uuid=uuid4(), name=name.strip(),
                        description=str(description or ""), position=position)
    db.session.add(board)
    for i, col_name in enumerate(KANBAN_DEFAULT_COLUMNS):
        db.session.add(KanbanColumn(uuid=uuid4(), board_uuid=board.uuid,
                                    name=col_name, position=i))
    db.session.commit()
    return kanban_load_board(board.uuid)  # type: ignore[return-value]


def kanban_delete_board(board_uuid: UUID) -> bool:
    """Delete a board with its columns, tasks, and task events. False if the
    board doesn't exist."""
    board = db.session.execute(
        sa.select(KanbanBoard).where(KanbanBoard.uuid == board_uuid)
    ).scalar_one_or_none()
    if board is None:
        return False
    task_uuids = db.session.execute(
        sa.select(KanbanTask.uuid).where(KanbanTask.board_uuid == board_uuid)
    ).scalars().all()
    if task_uuids:
        db.session.execute(sa.delete(KanbanTaskEvent)
                           .where(KanbanTaskEvent.task_uuid.in_(task_uuids)))
    db.session.execute(sa.delete(KanbanTask).where(KanbanTask.board_uuid == board_uuid))
    db.session.execute(sa.delete(KanbanColumn).where(KanbanColumn.board_uuid == board_uuid))
    db.session.delete(board)
    db.session.commit()
    return True


def kanban_duplicate_board(board_uuid: UUID) -> dict[str, Any] | None:
    """Deep-clone a board: fresh uuids for the board, every column, and every
    task (titles/descriptions/agents preserved, column mapping kept). The
    audit trail is NOT copied — it belongs to the original tasks; each clone
    task instead starts with a 'created' event naming the task it was
    duplicated from. Returns the new board's load payload, or None if the
    source board doesn't exist."""
    src = kanban_load_board(board_uuid)
    if src is None:
        return None
    position = db.session.execute(
        sa.select(sa.func.coalesce(sa.func.max(KanbanBoard.position), -1))
    ).scalar_one() + 1
    new_board = KanbanBoard(uuid=uuid4(), name=f"{src['name']} (copy)",
                            description=src["description"], position=position)
    db.session.add(new_board)
    col_map: dict[str, UUID] = {}
    for i, c in enumerate(src["columns"]):
        col_map[c["uuid"]] = uuid4()
        db.session.add(KanbanColumn(uuid=col_map[c["uuid"]],
                                    board_uuid=new_board.uuid,
                                    name=c["name"], position=i))
    for i, t in enumerate(src["tasks"]):
        tu = uuid4()
        db.session.add(KanbanTask(
            uuid=tu, board_uuid=new_board.uuid,
            column_uuid=col_map[t["columnUuid"]],
            title=t["title"], description=t["description"],
            agent_uuid=UUID(t["agentUuid"]) if t["agentUuid"] else None,
            position=i))
        db.session.add(KanbanTaskEvent(
            task_uuid=tu, kind="created", actor="human",
            detail=f"duplicated from `{t['uuid']}`"))
    db.session.commit()
    return kanban_load_board(new_board.uuid)


def kanban_list_boards() -> list[dict[str, Any]]:
    """Sidebar list: every board with its task count, in saved order."""
    boards = db.session.execute(
        sa.select(KanbanBoard).order_by(KanbanBoard.position, KanbanBoard.id)
    ).scalars().all()
    counts = {b: n for b, n in db.session.execute(
        sa.select(KanbanTask.board_uuid, sa.func.count()).group_by(KanbanTask.board_uuid)
    ).all()}
    return [{"uuid": str(b.uuid), "name": b.name,
             "taskCount": counts.get(b.uuid, 0)} for b in boards]


# ---- folder tree: create / delete / load / version / validate / save ----
# A separate concern from board CONTENTS (columns/tasks): this layer manages
# folders and which folder each board sits in. The save is placement-only (the
# /chat shape) — it never creates or deletes boards, and folder create/delete
# have their own endpoints (delete reparents, never cascades to boards).

def _folder_brief(f: "KanbanBoardFolder") -> dict[str, Any]:
    return {"uuid": str(f.uuid), "name": f.name, "description": f.description,
            "parentId": str(f.parent_uuid) if f.parent_uuid else None,
            "position": f.position}


def kanban_create_folder(
    name: str, parent_uuid: UUID | None = None, description: str = "",
) -> dict[str, Any]:
    """Create a folder (appended after its siblings); returns its brief dict."""
    if not isinstance(name, str) or not name.strip():
        raise KanbanError("folder name is required")
    position = db.session.execute(
        sa.select(sa.func.coalesce(sa.func.max(KanbanBoardFolder.position), -1))
        .where(KanbanBoardFolder.parent_uuid == parent_uuid)
    ).scalar_one() + 1
    folder = KanbanBoardFolder(uuid=uuid4(), name=name.strip(),
                               description=str(description or ""),
                               parent_uuid=parent_uuid, position=position)
    db.session.add(folder)
    db.session.commit()
    return _folder_brief(folder)


def kanban_delete_folder(folder_uuid: UUID) -> bool:
    """Delete a folder NON-DESTRUCTIVELY: its direct child folders and boards
    reparent up to the deleted folder's own parent (root if it had none), then
    the folder row is removed. Boards (and their columns/tasks) are never
    deleted by a folder delete. False if the folder doesn't exist."""
    folder = db.session.execute(
        sa.select(KanbanBoardFolder).where(KanbanBoardFolder.uuid == folder_uuid)
    ).scalar_one_or_none()
    if folder is None:
        return False
    grandparent = folder.parent_uuid
    db.session.execute(
        sa.update(KanbanBoardFolder)
        .where(KanbanBoardFolder.parent_uuid == folder_uuid)
        .values(parent_uuid=grandparent))
    db.session.execute(
        sa.update(KanbanBoard)
        .where(KanbanBoard.folder_uuid == folder_uuid)
        .values(folder_uuid=grandparent))
    db.session.delete(folder)
    db.session.commit()
    return True


def kanban_tree_version() -> str:
    """Opaque optimistic-concurrency token for the TREE (folders + board
    placement), over STRUCTURAL fields only — folder (uuid,name,parentId,
    position) and board (uuid,folderId,position). Board NAME and TASK COUNT are
    excluded on purpose: a board rename goes through the board PUT and agents
    add tasks in the background; including either would 409 the next tree save
    on every such event (the cron/chat 'exclude volatile fields' rule). The
    displayed name/count are kept in sync client-side instead."""
    folders = db.session.execute(
        sa.select(KanbanBoardFolder).order_by(KanbanBoardFolder.uuid)
    ).scalars().all()
    boards = db.session.execute(
        sa.select(KanbanBoard).order_by(KanbanBoard.uuid)
    ).scalars().all()
    payload = [
        [[str(f.uuid), f.name, f.description,
          str(f.parent_uuid) if f.parent_uuid else None, f.position]
         for f in folders],
        [[str(b.uuid), str(b.folder_uuid) if b.folder_uuid else None, b.position]
         for b in boards],
    ]
    blob = json.dumps(payload, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def kanban_load_tree() -> dict[str, Any]:
    """The /kanban left-panel tree: folders + boards (with task counts), each
    list in saved order. The page hydrates from this and PUTs it back. Board
    contents (columns/tasks) are NOT here — those load per-board."""
    folders = db.session.execute(
        sa.select(KanbanBoardFolder)
        .order_by(KanbanBoardFolder.position, KanbanBoardFolder.id)
    ).scalars().all()
    boards = db.session.execute(
        sa.select(KanbanBoard).order_by(KanbanBoard.position, KanbanBoard.id)
    ).scalars().all()
    counts = {b: n for b, n in db.session.execute(
        sa.select(KanbanTask.board_uuid, sa.func.count())
        .group_by(KanbanTask.board_uuid)
    ).all()}
    return {
        "folders": [_folder_brief(f) for f in folders],
        "boards": [
            {"uuid": str(b.uuid), "name": b.name,
             "folderId": str(b.folder_uuid) if b.folder_uuid else None,
             "position": b.position, "taskCount": counts.get(b.uuid, 0)}
            for b in boards
        ],
        "version": kanban_tree_version(),
    }


def validate_kanban_tree(
    folders: list[dict[str, Any]], boards: list[dict[str, Any]]
) -> None:
    """Structural integrity check for an incoming tree, run before any write.
    Raises KanbanError on the first problem; does not touch the DB. uuids are
    normalized so case/format-variant spellings of the same id collide here."""
    if not isinstance(folders, list):
        raise KanbanError(f"'folders' must be a list, got {type(folders).__name__}")
    if not isinstance(boards, list):
        raise KanbanError(f"'boards' must be a list, got {type(boards).__name__}")
    parent_of: dict[UUID, UUID | None] = {}
    for f in folders:
        if not isinstance(f, dict):
            raise KanbanError(f"folder entry must be an object, got {type(f).__name__}")
        fid = _to_uuid(f.get("uuid"))
        if fid is None:
            raise KanbanError(f"folder uuid is not a uuid: {f.get('uuid')!r}")
        if fid in parent_of:
            raise KanbanError(f"duplicate folder uuid: {fid}")
        if not isinstance(f.get("name", ""), str):
            raise KanbanError(f"folder {fid} name must be a string")
        if not isinstance(f.get("description", ""), str):
            raise KanbanError(f"folder {fid} description must be a string")
        pid_raw = f.get("parentId")
        if pid_raw is None:
            pid: UUID | None = None
        else:
            pid = _to_uuid(pid_raw)
            if pid is None:
                raise KanbanError(f"folder {fid} parentId is not a uuid: {pid_raw!r}")
        parent_of[fid] = pid
    for fid, pid in parent_of.items():
        if pid is not None and pid not in parent_of:
            raise KanbanError(f"folder {fid} references missing parent {pid}")
    # Acyclic: walking parents from any folder must terminate at a root.
    for start in parent_of:
        seen: set[UUID] = set()
        cur = parent_of[start]
        while cur is not None:
            if cur == start or cur in seen:
                raise KanbanError(f"folder cycle detected involving {start}")
            seen.add(cur)
            cur = parent_of.get(cur)
    board_uuids: set[UUID] = set()
    for b in boards:
        if not isinstance(b, dict):
            raise KanbanError(f"board entry must be an object, got {type(b).__name__}")
        bu = _to_uuid(b.get("uuid"))
        if bu is None:
            raise KanbanError(f"board uuid is not a uuid: {b.get('uuid')!r}")
        if bu in board_uuids:
            raise KanbanError(f"duplicate board uuid: {bu}")
        # uuids are globally unique across kinds: a node is deep-linked by uuid,
        # so a board sharing a folder's uuid would make the link ambiguous.
        if bu in parent_of:
            raise KanbanError(f"board uuid {bu} collides with a folder uuid")
        board_uuids.add(bu)
        fld_raw = b.get("folderId")
        if fld_raw is not None:
            fld = _to_uuid(fld_raw)
            if fld is None:
                raise KanbanError(f"board {bu} folderId is not a uuid: {fld_raw!r}")
            if fld not in parent_of:
                raise KanbanError(f"board {bu} references missing folder {fld}")


def kanban_save_tree(
    folders: list[dict[str, Any]], boards: list[dict[str, Any]],
    *, base_version: str | None = None,
) -> None:
    """Placement-only save of the tree: upsert folder name/description/parent/
    position and update each board's folder_uuid/position from list order.
    NEVER creates or deletes boards, and does not delete folders (deletion is
    kanban_delete_folder). A folder present in the DB but absent from the
    payload is left untouched. Validates first (KanbanError before any write);
    a stale base_version raises KanbanConflict."""
    validate_kanban_tree(folders, boards)
    if base_version is not None and base_version != kanban_tree_version():
        raise KanbanConflict("kanban tree changed since it was loaded")
    existing_f = {f.uuid: f for f in db.session.execute(
        sa.select(KanbanBoardFolder)).scalars().all()}
    existing_b = {b.uuid: b for b in db.session.execute(
        sa.select(KanbanBoard)).scalars().all()}
    for i, f in enumerate(folders):
        fu = _to_uuid(f["uuid"])
        row = existing_f.get(fu)
        if row is None:
            row = KanbanBoardFolder(uuid=fu)
            db.session.add(row)
        row.name = f.get("name", "")
        row.description = f.get("description", "")
        row.parent_uuid = _to_uuid(f["parentId"]) if f.get("parentId") else None
        row.position = i
    for i, b in enumerate(boards):
        bu = _to_uuid(b["uuid"])
        row = existing_b.get(bu)
        if row is None:
            continue  # placement-only: never create a board here
        row.folder_uuid = _to_uuid(b["folderId"]) if b.get("folderId") else None
        row.position = i
    db.session.commit()


def _board_rows(board_uuid: UUID):
    columns = db.session.execute(
        sa.select(KanbanColumn).where(KanbanColumn.board_uuid == board_uuid)
        .order_by(KanbanColumn.position, KanbanColumn.id)
    ).scalars().all()
    tasks = db.session.execute(
        sa.select(KanbanTask).where(KanbanTask.board_uuid == board_uuid)
        .order_by(KanbanTask.position, KanbanTask.id)
    ).scalars().all()
    return columns, tasks


def kanban_board_version(board_uuid: UUID) -> str:
    """Opaque optimistic-concurrency token over the user-managed fields of one
    board (same construction as cron_tree_version): any edit by another writer
    changes it; a PUT carrying a stale token is refused."""
    board = db.session.execute(
        sa.select(KanbanBoard).where(KanbanBoard.uuid == board_uuid)
    ).scalar_one_or_none()
    if board is None:
        return ""
    columns, tasks = _board_rows(board_uuid)
    payload = [
        [board.name, board.description],
        [[str(c.uuid), c.name, c.position] for c in columns],
        [[str(t.uuid), str(t.column_uuid), t.title, t.description,
          str(t.agent_uuid) if t.agent_uuid else None, t.position] for t in tasks],
    ]
    blob = json.dumps(payload, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def kanban_load_board(board_uuid: UUID) -> dict[str, Any] | None:
    """The wire format the /kanban page hydrates from (and PUTs back). List
    order is the saved order; the save derives `position` from it."""
    board = db.session.execute(
        sa.select(KanbanBoard).where(KanbanBoard.uuid == board_uuid)
    ).scalar_one_or_none()
    if board is None:
        return None
    columns, tasks = _board_rows(board_uuid)
    return {
        "uuid": str(board.uuid),
        "name": board.name,
        "description": board.description,
        "columns": [{"uuid": str(c.uuid), "name": c.name} for c in columns],
        "tasks": [
            {
                "uuid": str(t.uuid),
                "columnUuid": str(t.column_uuid),
                "title": t.title,
                "description": t.description,
                "agentUuid": str(t.agent_uuid) if t.agent_uuid else None,
                # Read-only lease state for display; the bulk save ignores it
                # (only the claim operations write the lease).
                "claimedBy": str(t.claimed_by) if t.claimed_by else None,
                "claimExpiresAt": t.claim_expires_at.isoformat()
                                  if t.claim_expires_at else None,
            }
            for t in tasks
        ],
        "version": kanban_board_version(board_uuid),
    }


# ---- bulk save (the page's debounced PUT) ----

def validate_kanban_payload(payload: dict[str, Any]) -> None:
    """Structural validation, run before any mutation: column/task uuids parse
    and are unique, every task references a column present in the payload,
    text fields are strings, agentUuid is null or a uuid."""
    if not isinstance(payload, dict):
        raise KanbanError("payload must be an object")
    if not isinstance(payload.get("name", ""), str):
        raise KanbanError("board name must be a string")
    if not isinstance(payload.get("description", ""), str):
        raise KanbanError("board description must be a string")
    columns = payload.get("columns", [])
    tasks = payload.get("tasks", [])
    if not isinstance(columns, list) or not columns:
        raise KanbanError("'columns' must be a non-empty list")
    if not isinstance(tasks, list):
        raise KanbanError("'tasks' must be a list")
    col_uuids: set[UUID] = set()
    for c in columns:
        if not isinstance(c, dict):
            raise KanbanError("column entry must be an object")
        cu = _to_uuid(c.get("uuid"))
        if cu is None:
            raise KanbanError(f"column uuid is not a uuid: {c.get('uuid')!r}")
        if cu in col_uuids:
            raise KanbanError(f"duplicate column uuid: {cu}")
        if not isinstance(c.get("name", ""), str):
            raise KanbanError(f"column {cu} name must be a string")
        col_uuids.add(cu)
    task_uuids: set[UUID] = set()
    for t in tasks:
        if not isinstance(t, dict):
            raise KanbanError("task entry must be an object")
        tu = _to_uuid(t.get("uuid"))
        if tu is None:
            raise KanbanError(f"task uuid is not a uuid: {t.get('uuid')!r}")
        if tu in task_uuids or tu in col_uuids:
            raise KanbanError(f"duplicate uuid: {tu}")
        task_uuids.add(tu)
        cu = _to_uuid(t.get("columnUuid"))
        if cu is None or cu not in col_uuids:
            raise KanbanError(f"task {tu} references missing column {t.get('columnUuid')!r}")
        if not isinstance(t.get("title", ""), str):
            raise KanbanError(f"task {tu} title must be a string")
        if not isinstance(t.get("description", ""), str):
            raise KanbanError(f"task {tu} description must be a string")
        agent_raw = t.get("agentUuid")
        if agent_raw is not None and _to_uuid(agent_raw) is None:
            raise KanbanError(f"task {tu} agentUuid must be a uuid or null: {agent_raw!r}")


def kanban_save_board(
    board_uuid: UUID, payload: dict[str, Any],
    *, base_version: str | None = None, expected_deletes: int | None = None,
    actor: str = "human",
) -> None:
    """Upsert one board's columns + tasks by uuid (list order → position);
    rows absent from the payload are deleted. Returns None; raises
    KanbanError (validation / unknown board), KanbanConflict (stale
    base_version). The two guards mirror cron_save_tree:

    - `base_version`: the version token the caller hydrated with; stale →
      KanbanConflict before any mutation (a second tab can't clobber).
    - `expected_deletes`: deletions the caller knowingly performed; a save
      deleting more than declared (truncated payload) is refused.

    Audit: a 'created' event is appended for each new task and a 'moved'
    event for each task whose column changed, attributed to `actor` — so UI
    edits land in the same kanban_task_event trail as agent operations."""
    board = db.session.execute(
        sa.select(KanbanBoard).where(KanbanBoard.uuid == board_uuid)
    ).scalar_one_or_none()
    if board is None:
        raise KanbanError(f"unknown board {board_uuid}")
    validate_kanban_payload(payload)
    if base_version is not None and base_version != kanban_board_version(board_uuid):
        raise KanbanConflict("board changed since it was loaded")
    existing_c = {c.uuid: c for c in db.session.execute(
        sa.select(KanbanColumn).where(KanbanColumn.board_uuid == board_uuid)
    ).scalars().all()}
    existing_t = {t.uuid: t for t in db.session.execute(
        sa.select(KanbanTask).where(KanbanTask.board_uuid == board_uuid)
    ).scalars().all()}
    incoming_c = {_to_uuid(c["uuid"]) for c in payload.get("columns", [])}
    incoming_t = {_to_uuid(t["uuid"]) for t in payload.get("tasks", [])}
    if expected_deletes is not None:
        would_delete = len((set(existing_c) | set(existing_t)) - (incoming_c | incoming_t))
        if would_delete > expected_deletes:
            raise KanbanError(
                f"save would delete {would_delete} row(s) but only "
                f"{expected_deletes} deletion(s) were declared — refusing"
            )
    board.name = payload.get("name", board.name)
    board.description = payload.get("description", board.description)
    for i, c in enumerate(payload.get("columns", [])):
        cu = _to_uuid(c["uuid"])
        row = existing_c.get(cu)
        if row is None:
            row = KanbanColumn(uuid=cu, board_uuid=board_uuid)
            db.session.add(row)
        row.name = c.get("name", "")
        row.position = i
    for cu, row in existing_c.items():
        if cu not in incoming_c:
            db.session.delete(row)
    deleted_task_uuids = [tu for tu in existing_t if tu not in incoming_t]
    for i, t in enumerate(payload.get("tasks", [])):
        tu = _to_uuid(t["uuid"])
        col = _to_uuid(t["columnUuid"])
        agent = _to_uuid(t["agentUuid"]) if t.get("agentUuid") else None
        row = existing_t.get(tu)
        if row is None:
            row = KanbanTask(uuid=tu, board_uuid=board_uuid, column_uuid=col)
            db.session.add(row)
            db.session.add(KanbanTaskEvent(task_uuid=tu, kind="created",
                                           actor=actor, detail=t.get("title", "")))
        elif row.column_uuid != col:
            old = existing_c.get(row.column_uuid)
            new_names = {_to_uuid(c["uuid"]): c.get("name", "") for c in payload["columns"]}
            db.session.add(KanbanTaskEvent(
                task_uuid=tu, kind="moved", actor=actor,
                detail=f"{old.name if old else '?'} → {new_names.get(col, '?')}"))
        row.column_uuid = col
        row.title = t.get("title", "")
        row.description = t.get("description", "")
        row.agent_uuid = agent
        row.position = i
    for tu in deleted_task_uuids:
        db.session.delete(existing_t[tu])
        db.session.execute(sa.delete(KanbanTaskEvent)
                           .where(KanbanTaskEvent.task_uuid == tu))
    db.session.commit()


# ---- markdown serialization (the canonical LLM-facing read view) ----

_MD_LINE_TOKEN = re.compile(r"^(\s{0,3})([#>*+`-]|\d+\.(?=\s))")


def _md_inline(text: str) -> str:
    """One-line value interpolated into markdown structure (titles, names):
    newlines collapse to spaces and the characters that could fake structure
    or close our own emphasis/code spans are backslash-escaped."""
    flat = " ".join(str(text or "").split())
    return re.sub(r"([\\`*_\[\]()])", r"\\\1", flat)


def _md_block(text: str) -> list[str]:
    """Description lines, escaped so task content cannot forge board
    structure: any line that *starts* like a heading/bullet/quote/fence gets
    its first token backslash-escaped. The contract stays parseable: only
    unindented `- **…**` lines are tasks, `##` lines are columns."""
    out = []
    for line in str(text or "").splitlines():
        if line.strip():
            out.append(_MD_LINE_TOKEN.sub(r"\1\\\2", line))
    return out


def _agent_display_names() -> dict[str, str]:
    """agent uuid (str) -> role name, for resolving @agent in serializations."""
    from agents.config import agent_config

    return {str(entry["uuid"]): name for name, entry in agent_config.items()}


# Focus values accepted by the serializers; None = the full symmetric document.
KANBAN_FOCUS_VALUES = frozenset({"in-progress"})
# How many recent task events the focus serialization inlines per task.
KANBAN_FOCUS_EVENT_LIMIT = 5


def _check_focus(focus: str | None) -> None:
    if focus is not None and focus not in KANBAN_FOCUS_VALUES:
        raise ValueError(f"unknown focus: {focus!r} (valid: {', '.join(sorted(KANBAN_FOCUS_VALUES))})")


def _focus_roles(columns: list[dict], focus: str | None) -> dict[str, str]:
    """column uuid -> 'brief' | 'full' | 'summary'. focus=in-progress on a
    board with >=3 columns: first column brief (title+id), last summarized
    (count + titles), middle full incl. lease + recent events. Fewer than 3
    columns (or no focus): everything 'full' — the plain document."""
    if focus is None or len(columns) < 3:
        return {c["uuid"]: "full" for c in columns}
    roles = {c["uuid"]: "full" for c in columns}
    roles[columns[0]["uuid"]] = "brief"
    roles[columns[-1]["uuid"]] = "summary"
    return roles


def _event_line(e: dict[str, Any]) -> str:
    """One escaped, single-line event for inlining under a task bullet."""
    flat = " ".join(str(e.get("detail") or "").split())[:200]
    # The literal `[ ]` around kind are intentional and safe: without a matching
    # link *definition* elsewhere in the document, `[text]` renders literally in
    # Markdown, and `(`/`)` are escaped by `_md_inline` so a detail can never
    # complete a `[text](url)` link.
    return f"[{_md_inline(e.get('kind'))}] {_md_inline(flat)}".rstrip()


def _focus_events(tasks: list[dict], roles: dict[str, str]) -> dict[str, list[dict]]:
    """Recent events for every task in a 'full' column under focus — the
    worker's resumable memory (docs/kanban-design.md 'Events are the working
    memory')."""
    # N+1 is acceptable: boards are bounded by the LLM context budget (docs/kanban-design.md).
    out: dict[str, list[dict]] = {}
    for t in tasks:
        if roles.get(t["columnUuid"]) == "full":
            events = kanban_task_events(UUID(t["uuid"]),
                                        limit=KANBAN_FOCUS_EVENT_LIMIT)
            if events:
                out[t["uuid"]] = events
    return out


def kanban_board_markdown(board_uuid: UUID, focus: str | None = None) -> str | None:
    """Serialize one board to the markdown contract (docs/kanban-design.md).
    Carries the SAME ids as the JSON twin, under the same role names: the
    board line is `Board id:`, each `##` column heading ends with its
    backticked columnId, and each task bullet has its taskId and — when
    assigned — the agent as @name (`agentId`). Columns are `##` headings in
    board order; a task is one bullet with bold title; description lines are
    indented beneath. Done-ness is the column, not checkboxes.

    focus='in-progress' (>=3 columns): first column shrinks to title+id
    bullets, the last to a count + titles, and middle columns gain lease
    state and recent events — the asymmetric context an executing agent
    needs. Unknown focus raises ValueError."""
    _check_focus(focus)
    data = kanban_load_board(board_uuid)
    if data is None:
        return None
    # <3 columns: focus collapses to the full symmetric document (byte-identical
    # to the default). Pass focus=None to the renderer so no lease/event lines
    # appear and events_by_task stays empty.
    active_focus = focus if (focus and len(data["columns"]) >= 3) else None  # mirrors _focus_roles' <3-column rule — keep in sync
    roles = _focus_roles(data["columns"], active_focus)
    events = _focus_events(data["tasks"], roles) if active_focus else {}
    return kanban_render_markdown(data, _agent_display_names(),
                                  focus=active_focus, events_by_task=events)


def kanban_render_markdown(
    data: dict[str, Any], agent_names: dict[str, str],
    *, focus: str | None = None,
    events_by_task: dict[str, list[dict]] | None = None,
) -> str:
    """Pure renderer for the markdown contract over a load_board-shaped
    payload. Split from kanban_board_markdown so callers with synthetic
    boards (the kanban benchmark) serialize with the production code — the
    benchmark must measure the real contract, not a copy of it."""
    _check_focus(focus)
    roles = _focus_roles(data["columns"], focus)
    events_by_task = events_by_task or {}
    lines = [f"# Kanban board: {_md_inline(data['name'])}", "",
             f"Board id: `{data['uuid']}`", ""]
    if data["description"].strip():
        lines.extend(_md_block(data["description"]))
        lines.append("")
    tasks_by_col: dict[str, list[dict]] = {}
    for t in data["tasks"]:
        tasks_by_col.setdefault(t["columnUuid"], []).append(t)
    for col in data["columns"]:
        lines.append(f"## {_md_inline(col['name'])} (`{col['uuid']}`)")
        lines.append("")
        tasks = tasks_by_col.get(col["uuid"], [])
        if not tasks:
            lines.extend(["_(empty)_", ""])
            continue
        role = roles[col["uuid"]]
        if role == "summary":
            lines.append(f"_{len(tasks)} task(s)_")
            lines.extend(f"- {_md_inline(t['title'])}" for t in tasks)
            lines.append("")
            continue
        for t in tasks:
            if role == "brief":
                lines.append(f"- **{_md_inline(t['title'])}** (`{t['uuid']}`)")
                continue
            agent = (f"@{_md_inline(agent_names.get(t['agentUuid'], t['agentUuid']))}"
                     f" (`{t['agentUuid']}`)"
                     if t["agentUuid"] else "_unassigned_")
            lines.append(f"- **{_md_inline(t['title'])}** (`{t['uuid']}`) — {agent}")
            lines.extend("  " + d for d in _md_block(t["description"]))
            if focus and t.get("claimedBy"):
                lines.append(f"  _claimed by `{t['claimedBy']}` "
                             f"until {t.get('claimExpiresAt') or '?'}_")
            for e in events_by_task.get(t["uuid"], []):
                lines.append(f"  - {_event_line(e)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def kanban_board_llm_json(
    board_uuid: UUID, focus: str | None = None,
) -> dict[str, Any] | None:
    """The JSON twin of the markdown serialization: one board as a nested
    columns→tasks structure for LLM context. Differs from the page's wire
    format on purpose — tasks sit inside their column (no cross-referencing
    needed), ids use role-named keys (boardId/columnId/taskId/agentId — what a
    model should call them when invoking operations), agents carry both id and
    resolved display name, and there is no `version` (this is a read snapshot,
    not a save payload). focus= applies the same asymmetric rule as the
    markdown twin — one knob set, both formats."""
    _check_focus(focus)
    data = kanban_load_board(board_uuid)
    if data is None:
        return None
    # <3 columns: focus collapses to the full symmetric document.
    active_focus = focus if (focus and len(data["columns"]) >= 3) else None  # mirrors _focus_roles' <3-column rule — keep in sync
    roles = _focus_roles(data["columns"], active_focus)
    events = _focus_events(data["tasks"], roles) if active_focus else {}
    return kanban_render_llm_json(data, _agent_display_names(),
                                  focus=active_focus, events_by_task=events)


def kanban_render_llm_json(
    data: dict[str, Any], agent_names: dict[str, str],
    *, focus: str | None = None,
    events_by_task: dict[str, list[dict]] | None = None,
) -> dict[str, Any]:
    """Pure renderer for the LLM JSON contract over a load_board-shaped
    payload (see kanban_render_markdown for why this is split out)."""
    _check_focus(focus)
    roles = _focus_roles(data["columns"], focus)
    events_by_task = events_by_task or {}
    tasks_by_col: dict[str, list[dict]] = {}
    for t in data["tasks"]:
        tasks_by_col.setdefault(t["columnUuid"], []).append(t)

    def _task_json(t: dict, role: str) -> dict[str, Any]:
        if role == "brief":
            return {"taskId": t["uuid"], "title": t["title"]}
        if role == "summary":
            return {"title": t["title"]}
        full = {
            "taskId": t["uuid"],
            "title": t["title"],
            "description": t["description"],
            "agentId": t["agentUuid"],
            "agentName": agent_names.get(t["agentUuid"])
                         if t["agentUuid"] else None,
        }
        if focus:
            full["claimedBy"] = t.get("claimedBy")
            full["claimExpiresAt"] = t.get("claimExpiresAt")
            full["events"] = [
                {"kind": e["kind"], "detail": e["detail"],
                 "createdAt": e["created_at"]}
                for e in events_by_task.get(t["uuid"], [])
            ]
        return full

    columns = []
    for col in data["columns"]:
        role = roles[col["uuid"]]
        tasks = tasks_by_col.get(col["uuid"], [])
        entry: dict[str, Any] = {
            "columnId": col["uuid"],
            "name": col["name"],
            "tasks": [_task_json(t, role) for t in tasks],
        }
        if role == "summary":
            entry["taskCount"] = len(tasks)
        columns.append(entry)
    return {
        "boardId": data["uuid"],
        "name": data["name"],
        "description": data["description"],
        "columns": columns,
    }


# ---- per-task agent operations (the LLM-facing write primitives) ----
# Each is row-level and uuid-addressed: it succeeds atomically (with an audit
# event) or raises — no document editing, nothing for a small model to get
# subtly wrong. They are equally usable as function-calling tools or as the
# interpretation of a structured-output reply.

def _task(task_uuid: UUID) -> "KanbanTask | None":
    return db.session.execute(
        sa.select(KanbanTask).where(KanbanTask.uuid == task_uuid)
    ).scalar_one_or_none()


def _task_brief(t: "KanbanTask") -> dict[str, Any]:
    return {"uuid": str(t.uuid), "boardUuid": str(t.board_uuid),
            "columnUuid": str(t.column_uuid), "title": t.title,
            "description": t.description,
            "agentUuid": str(t.agent_uuid) if t.agent_uuid else None,
            "claimedBy": str(t.claimed_by) if t.claimed_by else None,
            "claimExpiresAt": t.claim_expires_at.isoformat()
                              if t.claim_expires_at else None}


def _lease_live(t: "KanbanTask", now: datetime) -> bool:
    return t.claimed_by is not None and (t.claim_expires_at or now) > now


def kanban_append_event(
    task_uuid: UUID, kind: str, *, actor: str = "", detail: str = ""
) -> dict[str, Any] | None:
    """Append an audit/progress event to a task (kind: free short word —
    'note', 'progress', …). None if the task is gone."""
    t = _task(task_uuid)
    if t is None:
        return None
    if not isinstance(kind, str) or not kind.strip():
        raise KanbanError("event kind is required")
    db.session.add(KanbanTaskEvent(task_uuid=task_uuid, kind=kind.strip(),
                                   actor=str(actor or ""), detail=str(detail or "")))
    db.session.commit()
    return _task_brief(t)


def kanban_task_events(task_uuid: UUID, limit: int = 50) -> list[dict[str, Any]] | None:
    """A task's audit trail, newest first. None when the task doesn't exist —
    distinct from an empty history, so an agent quoting a stale/hallucinated
    taskId gets a loud 404 instead of a plausible-looking empty list."""
    if _task(task_uuid) is None:
        return None
    rows = db.session.execute(
        sa.select(KanbanTaskEvent).where(KanbanTaskEvent.task_uuid == task_uuid)
        .order_by(KanbanTaskEvent.id.desc()).limit(limit)
    ).scalars().all()
    return [{"kind": e.kind, "actor": e.actor, "detail": e.detail,
             "created_at": e.created_at.isoformat() if e.created_at else None}
            for e in rows]


def _locked_task(task_uuid: UUID) -> "KanbanTask | None":
    """The task row, locked FOR UPDATE — claim/release/renew decisions read
    and write under the row lock, so two concurrent operations on the same
    task serialize instead of both passing a stale check."""
    return db.session.execute(
        sa.select(KanbanTask).where(KanbanTask.uuid == task_uuid)
        .with_for_update()
    ).scalar_one_or_none()


def kanban_claim_task(
    task_uuid: UUID, agent_uuid: UUID, lease: timedelta = KANBAN_CLAIM_LEASE,
) -> dict[str, Any] | None:
    """An agent takes a LEASE on a specific task (claimed_by — the assignee
    `agent_uuid` is untouched; humans assign, agents claim). Claiming an
    unleased task (or one whose lease has EXPIRED — lease takeover, so a
    crashed agent can't hold a task forever) succeeds and is recorded;
    re-claiming one's own live lease renews it without a new event; a task
    another agent holds a LIVE lease on raises KanbanConflict. Row-locked, so
    concurrent claims serialize and exactly one wins."""
    now = datetime.now(UTC)
    t = _locked_task(task_uuid)
    if t is None:
        db.session.rollback()
        return None
    if _lease_live(t, now) and t.claimed_by != agent_uuid:
        db.session.rollback()
        raise KanbanConflict(
            f"task claimed by {t.claimed_by} until {t.claim_expires_at.isoformat()}")
    renewal = t.claimed_by == agent_uuid
    takeover = t.claimed_by is not None and not renewal  # expired lease of another
    previous = t.claimed_by
    t.claimed_by = agent_uuid
    t.claimed_at = now
    t.claim_expires_at = now + lease
    if not renewal:
        db.session.add(KanbanTaskEvent(
            task_uuid=task_uuid, kind="claimed", actor=str(agent_uuid),
            detail=f"lease takeover from {previous} (expired)" if takeover else ""))
    db.session.commit()
    return _task_brief(t)


def kanban_release_task(task_uuid: UUID, agent_uuid: UUID) -> dict[str, Any] | None:
    """Give a lease back (the agent is done or bowing out) so the task is
    immediately claimable by others. Releasing an unclaimed task is a no-op;
    anyone may clear an EXPIRED lease; a LIVE lease can only be released by
    its holder (KanbanConflict otherwise)."""
    now = datetime.now(UTC)
    t = _locked_task(task_uuid)
    if t is None:
        db.session.rollback()
        return None
    if t.claimed_by is None:
        db.session.commit()
        return _task_brief(t)  # nothing to release
    if _lease_live(t, now) and t.claimed_by != agent_uuid:
        db.session.rollback()
        raise KanbanConflict(
            f"task claimed by {t.claimed_by} until {t.claim_expires_at.isoformat()}")
    previous = t.claimed_by
    t.claimed_by = None
    t.claimed_at = None
    t.claim_expires_at = None
    db.session.add(KanbanTaskEvent(
        task_uuid=task_uuid, kind="released", actor=str(agent_uuid),
        detail=f"expired lease of {previous}" if previous != agent_uuid else ""))
    db.session.commit()
    return _task_brief(t)


def kanban_renew_claim(
    task_uuid: UUID, agent_uuid: UUID, lease: timedelta = KANBAN_CLAIM_LEASE,
) -> dict[str, Any] | None:
    """Extend one's own lease (a long-running agent heartbeats this). Only the
    current holder may renew — even of its own expired-but-not-taken lease;
    KanbanError when the task isn't claimed at all, KanbanConflict when
    another agent holds it. No event (renewals would drown the trail)."""
    now = datetime.now(UTC)
    t = _locked_task(task_uuid)
    if t is None:
        db.session.rollback()
        return None
    if t.claimed_by is None:
        db.session.rollback()
        raise KanbanError("task is not claimed — claim it first")
    if t.claimed_by != agent_uuid:
        db.session.rollback()
        raise KanbanConflict(f"task claimed by {t.claimed_by}")
    t.claim_expires_at = now + lease
    db.session.commit()
    return _task_brief(t)


def kanban_claim_next(
    agent_uuid: UUID, board_uuid: UUID | None = None,
    include_unassigned: bool = True, lease: timedelta = KANBAN_CLAIM_LEASE,
) -> dict[str, Any] | None:
    """Atomically find and LEASE one eligible task for an agent — the
    coordination primitive that keeps models from scanning a board and racing
    each other over the same pick. The DB decides:

    eligible = in a *runnable* column (any column except its board's last —
    the done-by-convention column), assigned to this agent or unassigned
    (when include_unassigned), and not held under another agent's LIVE lease
    (an expired lease is up for takeover). Preference order: the task this
    agent already holds a live lease on first ("what am I working on" — a
    worker restart resumes its work, renewing the lease without a duplicate
    event), then this agent's assigned tasks, then unassigned; then earlier
    boards (when no board filter is given), earlier columns, position.

    The claim goes into `claimed_by` (the lease) — the assignee `agent_uuid`
    is never touched: humans assign, agents claim. Tasks leave eligibility by
    completing/moving, not by claiming.

    Concurrency: the pick is `SELECT … FOR UPDATE SKIP LOCKED` (the classic
    job-queue pattern), so concurrent claim-next calls never select the same
    row. Returns the claimed task brief, or None when nothing is eligible."""
    now = datetime.now(UTC)
    col_pos = (sa.select(KanbanColumn.position)
               .where(KanbanColumn.uuid == KanbanTask.column_uuid)
               .scalar_subquery())
    last_col_pos = (sa.select(sa.func.max(KanbanColumn.position))
                    .where(KanbanColumn.board_uuid == KanbanTask.board_uuid)
                    .scalar_subquery())
    board_pos = (sa.select(KanbanBoard.position)
                 .where(KanbanBoard.uuid == KanbanTask.board_uuid)
                 .scalar_subquery())
    lease_live = sa.and_(KanbanTask.claimed_by.isnot(None),
                         KanbanTask.claim_expires_at > now)
    mine_live = sa.and_(KanbanTask.claimed_by == agent_uuid,
                        KanbanTask.claim_expires_at > now)
    conds = [col_pos < last_col_pos,
             sa.or_(sa.not_(lease_live), KanbanTask.claimed_by == agent_uuid)]
    if board_uuid is not None:
        conds.append(KanbanTask.board_uuid == board_uuid)
    if include_unassigned:
        conds.append(sa.or_(KanbanTask.agent_uuid == agent_uuid,
                            KanbanTask.agent_uuid.is_(None)))
    else:
        conds.append(KanbanTask.agent_uuid == agent_uuid)
    task = db.session.execute(
        sa.select(KanbanTask)
        .where(*conds)
        # NB: mine_live is NULL (not false) for unleased rows, and Postgres
        # sorts NULLS FIRST under DESC — nullslast keeps the live lease on top.
        .order_by(sa.desc(mine_live).nullslast(),    # my in-progress lease first
                  KanbanTask.agent_uuid.is_(None),   # my assigned before unassigned
                  board_pos, col_pos, KanbanTask.position, KanbanTask.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()
    if task is None:
        db.session.rollback()
        return None
    renewal = task.claimed_by == agent_uuid
    takeover = task.claimed_by is not None and not renewal
    previous = task.claimed_by
    task.claimed_by = agent_uuid
    task.claimed_at = task.claimed_at if renewal else now
    task.claim_expires_at = now + lease
    if not renewal:
        db.session.add(KanbanTaskEvent(
            task_uuid=task.uuid, kind="claimed", actor=str(agent_uuid),
            detail="claim-next" + (f" (lease takeover from {previous}, expired)"
                                   if takeover else "")))
    db.session.commit()
    return _task_brief(task)


def kanban_enqueue_task(task_uuid: UUID) -> dict[str, Any] | None:
    """Enqueue a task's ASSIGNED agent to execute it — the milestone-3
    enqueue-on-command trigger ("Run" on the task, not polling). The inbox
    payload is {task_uuid, board_uuid, source: "kanban"}; the supervisor
    spawns the agent, whose kanban adapter (tools/kanban_runner.py) does
    claim → progress events → work → complete/fail.

    Loud preconditions: KanbanError when the task has no assignee or the
    assignee isn't a runnable agent (not in agent_config); KanbanConflict
    while someone holds a LIVE lease (it is already being worked)."""
    from agents.config import agent_config
    from db.queue import enqueue

    now = datetime.now(UTC)
    t = _task(task_uuid)
    if t is None:
        return None
    if t.agent_uuid is None:
        raise KanbanError("task has no assigned agent — assign one first")
    if t.agent_uuid not in {entry["uuid"] for entry in agent_config.values()}:
        raise KanbanError(f"assigned agent {t.agent_uuid} is not a runnable agent")
    if _lease_live(t, now):
        raise KanbanConflict(
            f"task is being worked: claimed by {t.claimed_by} "
            f"until {t.claim_expires_at.isoformat()}")
    enqueue(t.agent_uuid, {
        "task_uuid": str(t.uuid),
        "board_uuid": str(t.board_uuid),
        "source": "kanban",
    })
    db.session.add(KanbanTaskEvent(task_uuid=task_uuid, kind="enqueued",
                                   actor="human", detail=""))
    db.session.commit()
    return _task_brief(t)


def kanban_move_task(
    task_uuid: UUID, column_uuid: UUID, *, actor: str = "", note: str = ""
) -> dict[str, Any] | None:
    """Move a task to another column of its board (appended at the end).
    Raises KanbanError if the column doesn't exist on the task's board."""
    t = _task(task_uuid)
    if t is None:
        return None
    col = db.session.execute(
        sa.select(KanbanColumn).where(KanbanColumn.uuid == column_uuid,
                                      KanbanColumn.board_uuid == t.board_uuid)
    ).scalar_one_or_none()
    if col is None:
        raise KanbanError(f"column {column_uuid} is not on task {task_uuid}'s board")
    old = db.session.execute(
        sa.select(KanbanColumn.name).where(KanbanColumn.uuid == t.column_uuid)
    ).scalar_one_or_none() or "?"
    t.column_uuid = column_uuid
    t.position = (db.session.execute(
        sa.select(sa.func.coalesce(sa.func.max(KanbanTask.position), -1))
        .where(KanbanTask.column_uuid == column_uuid, KanbanTask.uuid != task_uuid)
    ).scalar_one()) + 1
    detail = f"{old} → {col.name}" + (f" — {note}" if note else "")
    db.session.add(KanbanTaskEvent(task_uuid=task_uuid, kind="moved",
                                   actor=str(actor or ""), detail=detail))
    db.session.commit()
    return _task_brief(t)


def kanban_complete_task(
    task_uuid: UUID, ok: bool, *, actor: str = "", detail: str = "",
    review: bool = False,
) -> dict[str, Any] | None:
    """Report the outcome of working a task. ok=True moves it to the board's
    LAST column (done by convention) and records a 'done' event; ok=False
    leaves it where it is and records a 'failed' event with the reason — a
    failure is information for the operator, not an automatic state change.
    Either way the lease is RELEASED: a finished task holds no claim, and a
    failed one becomes immediately claimable again (the retry path is
    complete(ok=False) → claim again).

    review=True (unverified agents — kanban_verified is False): ok=True
    routes to the first column named 'review' (case-insensitive, board
    order) and records a 'review' event instead of 'done' — a human moves
    Review → Done. A board with no Review column falls back to the normal
    Done behavior: the operator opts into review per board by adding the
    column."""
    t = _task(task_uuid)
    if t is None:
        return None
    t.claimed_by = None
    t.claimed_at = None
    t.claim_expires_at = None
    kind = "done" if ok else "failed"
    if ok:
        columns = db.session.execute(
            sa.select(KanbanColumn).where(KanbanColumn.board_uuid == t.board_uuid)
            .order_by(KanbanColumn.position, KanbanColumn.id)
        ).scalars().all()
        target = None
        if review:
            target = next((c for c in columns
                           if c.name.strip().lower() == "review"), None)
            if target is not None:
                kind = "review"
        if target is None and columns:
            target = columns[-1]
        if target is not None and t.column_uuid != target.uuid:
            kanban_move_task(task_uuid, target.uuid, actor=actor)
    db.session.add(KanbanTaskEvent(task_uuid=task_uuid, kind=kind,
                                   actor=str(actor or ""), detail=str(detail or "")))
    db.session.commit()
    return _task_brief(_task(task_uuid))  # type: ignore[arg-type]
