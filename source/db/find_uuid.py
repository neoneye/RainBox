"""Fuzzy uuid lookup across every uuid-bearing table.

Nearly everything in rainbox is uuid-addressed (kanban boards/columns/tasks,
cron jobs, chat rooms/messages, prompts, profiles, runs, …), and uuids leak
into chats, logs, and half-remembered pastes as fragments: a prefix, a
suffix, a typo'd character. `find_uuid` answers "what IS this uuid?" without
the caller knowing which table to look in or having the string exactly right
— so a weak LLM (or a human with a scrap of hex) can resolve an id into a
real entity, its parents, and the page it lives on.

Matching, strictest first (a pass only runs when the stricter ones found
nothing is false — substring hits suppress the fuzzy pass, not exact ones):

- exact: the query is the full 32-hex uuid (dashes/braces/spaces ignored)
- substring: the query is a contiguous fragment — beginning, end, or middle
- fuzzy (query ≥ 8 hex chars, only when nothing matched exactly/substring):
  best SequenceMatcher ratio of the query against every same-length window
  of each uuid's hex — catches one or two typo'd characters

Every match is described with its kind, display name, parent chain
(inner → outer: a task's column, board, folders …), and the `?id=` deep-link
url of the page that shows it. Read-only; no events are written.

Re-exported from db for import compatibility.
"""
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Callable
from uuid import UUID

import sqlalchemy as sa

from db.models import (AssistantRun, AssistantStep, ChatMessage, Chatroom,
                       ChatroomFolder, ChatUser, CronFolder, CronJob, CronRun,
                       GitFolder, GitRepo, Journal, KanbanBoard,
                       KanbanBoardFolder, KanbanColumn, KanbanTask,
                       MemoryClaim, ModelConfig, ModelConfigOverride,
                       ModelGroup, Profile, ProfileFolder, Prompt,
                       PromptFolder, db)

__all__ = ["find_uuid", "FIND_UUID_MIN_QUERY", "FIND_UUID_MIN_FUZZY_QUERY"]

# Shorter fragments match half the database; refuse loudly instead.
FIND_UUID_MIN_QUERY = 4
# Fuzzy needs enough signal that a ratio means anything.
FIND_UUID_MIN_FUZZY_QUERY = 8
_FUZZY_THRESHOLD = 0.78


def _excerpt(text: str, n: int = 60) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= n else flat[: n - 1] + "…"


def _row(model: Any, uuid_value: UUID, uuid_attr: str = "uuid") -> Any:
    return db.session.execute(
        sa.select(model).where(getattr(model, uuid_attr) == uuid_value)
    ).scalar_one_or_none()


def _parent_ref(kind: str, row: Any, name: str | None = None) -> dict[str, str]:
    return {"kind": kind, "uuid": str(row.uuid),
            "name": name if name is not None else (row.name or "(unnamed)")}


def _folder_chain(model: Any, kind: str, start: UUID | None) -> list[dict]:
    """Walk folder parent pointers to the root (cycle-safe: folders came from
    user drags; a corrupt chain must not hang the lookup)."""
    out: list[dict] = []
    seen: set[UUID] = set()
    cur = start
    while cur is not None and cur not in seen:
        seen.add(cur)
        row = _row(model, cur)
        if row is None:
            break
        out.append(_parent_ref(kind, row))
        cur = row.parent_uuid
    return out


# ---- per-kind describers: row -> {name, url, parents} ----

def _kanban_board(row: Any) -> dict:
    return {"name": row.name, "url": f"/kanban?id={row.uuid}",
            "parents": _folder_chain(KanbanBoardFolder, "kanban folder",
                                     row.folder_uuid)}


def _kanban_folder(row: Any) -> dict:
    return {"name": row.name, "url": f"/kanban?id={row.uuid}",
            "parents": _folder_chain(KanbanBoardFolder, "kanban folder",
                                     row.parent_uuid)}


def _kanban_column(row: Any) -> dict:
    board = _row(KanbanBoard, row.board_uuid)
    parents = []
    if board is not None:
        parents.append(_parent_ref("kanban board", board))
        parents.extend(_folder_chain(KanbanBoardFolder, "kanban folder",
                                     board.folder_uuid))
    # No per-column deep link exists; the board page shows the column.
    return {"name": row.name, "url": f"/kanban?id={row.board_uuid}",
            "parents": parents}


def _kanban_task(row: Any) -> dict:
    column = _row(KanbanColumn, row.column_uuid)
    board = _row(KanbanBoard, row.board_uuid)
    parents = []
    if column is not None:
        parents.append(_parent_ref("kanban column", column))
    if board is not None:
        parents.append(_parent_ref("kanban board", board))
        parents.extend(_folder_chain(KanbanBoardFolder, "kanban folder",
                                     board.folder_uuid))
    return {"name": row.title, "url": f"/kanban?id={row.uuid}", "parents": parents}


def _cron_job(row: Any) -> dict:
    return {"name": row.name, "url": f"/cron?id={row.uuid}",
            "parents": _folder_chain(CronFolder, "cron folder", row.folder_uuid)}


def _cron_folder(row: Any) -> dict:
    return {"name": row.name, "url": f"/cron?id={row.uuid}",
            "parents": _folder_chain(CronFolder, "cron folder", row.parent_uuid)}


def _cron_run(row: Any) -> dict:
    job = _row(CronJob, row.cron_uuid)
    parents = []
    if job is not None:
        parents.append(_parent_ref("cron job", job))
        parents.extend(_folder_chain(CronFolder, "cron folder", job.folder_uuid))
    fired = row.fired_at.isoformat() if row.fired_at else "?"
    return {"name": f"{row.trigger} @ {fired}",
            "url": f"/cron?id={row.cron_uuid}", "parents": parents}


def _chat_room(row: Any) -> dict:
    return {"name": row.name, "url": f"/chat?id={row.uuid}",
            "parents": _folder_chain(ChatroomFolder, "chat folder",
                                     row.folder_uuid)}


def _chat_folder(row: Any) -> dict:
    return {"name": row.name, "url": f"/chat?id={row.uuid}",
            "parents": _folder_chain(ChatroomFolder, "chat folder",
                                     row.parent_uuid)}


def _chat_message(row: Any) -> dict:
    room = _row(Chatroom, row.room_uuid)
    parents = []
    if room is not None:
        parents.append(_parent_ref("chat room", room))
        parents.extend(_folder_chain(ChatroomFolder, "chat folder",
                                     room.folder_uuid))
    return {"name": _excerpt(row.text), "url": f"/chat?id={row.room_uuid}",
            "parents": parents}


def _chat_user(row: Any) -> dict:
    return {"name": f"{row.name} ({row.user_type})", "url": None, "parents": []}


def _git_repo(row: Any) -> dict:
    return {"name": row.name, "url": f"/git?id={row.uuid}",
            "parents": _folder_chain(GitFolder, "git folder", row.folder_uuid)}


def _git_folder(row: Any) -> dict:
    return {"name": row.name, "url": f"/git?id={row.uuid}",
            "parents": _folder_chain(GitFolder, "git folder", row.parent_uuid)}


def _prompt(row: Any) -> dict:
    return {"name": row.name, "url": f"/prompt?id={row.uuid}",
            "parents": _folder_chain(PromptFolder, "prompt folder",
                                     row.folder_uuid)}


def _prompt_folder(row: Any) -> dict:
    return {"name": row.name, "url": f"/prompt?id={row.uuid}",
            "parents": _folder_chain(PromptFolder, "prompt folder",
                                     row.parent_uuid)}


def _profile(row: Any) -> dict:
    return {"name": row.name, "url": f"/profile?id={row.uuid}",
            "parents": _folder_chain(ProfileFolder, "profile folder",
                                     row.folder_uuid)}


def _profile_folder(row: Any) -> dict:
    return {"name": row.name, "url": f"/profile?id={row.uuid}",
            "parents": _folder_chain(ProfileFolder, "profile folder",
                                     row.parent_uuid)}


def _model_config_name(row: Any) -> str:
    return row.display_name or row.model_name


def _model_config(row: Any) -> dict:
    return {"name": _model_config_name(row), "url": f"/models?id={row.uuid}",
            "parents": []}


def _model_config_override(row: Any) -> dict:
    base = _row(ModelConfig, row.model_config_uuid)
    parents = ([_parent_ref("model config", base, _model_config_name(base))]
               if base else [])
    return {"name": row.display_name, "url": f"/models?id={row.uuid}",
            "parents": parents}


def _model_group(row: Any) -> dict:
    return {"name": row.name, "url": f"/modelgroup?id={row.uuid}", "parents": []}


def _memory_claim(row: Any) -> dict:
    return {"name": _excerpt(row.text), "url": f"/memory?id={row.uuid}",
            "parents": []}


def _assistant_run(row: Any) -> dict:
    room = _row(Chatroom, row.room_uuid)
    parents = [_parent_ref("chat room", room)] if room else []
    return {"name": f"{row.status}: {_excerpt(row.final_summary or '', 50)}".rstrip(": "),
            "url": f"/assistant?id={row.uuid}", "parents": parents}


def _assistant_step(row: Any) -> dict:
    run = _row(AssistantRun, row.run_uuid)
    parents = []
    if run is not None:
        parents.append({"kind": "assistant run", "uuid": str(run.uuid),
                        "name": run.status})
    return {"name": f"step {row.step_index}: {row.action or row.phase}",
            "url": f"/assistant?id={row.run_uuid}#step-{row.uuid}",
            "parents": parents}


def _journal(row: Any) -> dict:
    agent = _agent_names().get(str(row.agent_uuid), str(row.agent_uuid))
    return {"name": f"{agent}: {row.state}", "url": None, "parents": []}


def _agent_names() -> dict[str, str]:
    from agents.config import agent_config

    return {str(entry["uuid"]): name for name, entry in agent_config.items()}


@dataclass(frozen=True)
class _Source:
    kind: str
    model: Any
    describe: Callable[[Any], dict]
    uuid_attr: str = "uuid"


_SOURCES: tuple[_Source, ...] = (
    _Source("kanban board", KanbanBoard, _kanban_board),
    _Source("kanban folder", KanbanBoardFolder, _kanban_folder),
    _Source("kanban column", KanbanColumn, _kanban_column),
    _Source("kanban task", KanbanTask, _kanban_task),
    _Source("cron folder", CronFolder, _cron_folder),
    _Source("cron job", CronJob, _cron_job),
    _Source("cron run", CronRun, _cron_run),
    _Source("chat folder", ChatroomFolder, _chat_folder),
    _Source("chat room", Chatroom, _chat_room),
    _Source("chat message", ChatMessage, _chat_message),
    _Source("chat user", ChatUser, _chat_user),
    _Source("git folder", GitFolder, _git_folder),
    _Source("git repo", GitRepo, _git_repo),
    _Source("prompt folder", PromptFolder, _prompt_folder),
    _Source("prompt", Prompt, _prompt),
    _Source("profile folder", ProfileFolder, _profile_folder),
    _Source("profile", Profile, _profile),
    _Source("model config", ModelConfig, _model_config),
    _Source("model config override", ModelConfigOverride, _model_config_override),
    _Source("model group", ModelGroup, _model_group),
    _Source("memory claim", MemoryClaim, _memory_claim),
    _Source("assistant run", AssistantRun, _assistant_run),
    _Source("assistant step", AssistantStep, _assistant_step),
    _Source("journal", Journal, _journal, uuid_attr="id"),
)


def _normalize(query: str) -> str:
    """Lowercase and drop uuid punctuation/wrapping (dashes, braces, quotes,
    whitespace, urn: prefixes) — typo'd non-hex letters are KEPT so the fuzzy
    pass sees them as the mismatches they are."""
    q = str(query or "").strip().lower()
    q = q.removeprefix("urn:uuid:")
    return "".join(ch for ch in q if ch not in " \t\n{}()[]\"'`,;:-")


def _best_window_ratio(q: str, hex32: str) -> float:
    """The query's best SequenceMatcher ratio against every same-length
    window of the uuid's hex — a positional typo scores high wherever the
    fragment sits in the uuid."""
    n = len(q)
    if n >= len(hex32):
        return SequenceMatcher(None, q, hex32).ratio()
    best = 0.0
    for i in range(len(hex32) - n + 1):
        best = max(best, SequenceMatcher(None, q, hex32[i:i + n]).ratio())
    return best


def find_uuid(query: str, limit: int = 20) -> list[dict[str, Any]]:
    """Resolve a (partial, possibly typo'd) uuid to matches across every
    registered table: [{kind, uuid, name, url, parents, match, confidence}],
    best first. Raises ValueError for a query under FIND_UUID_MIN_QUERY
    useful characters."""
    q = _normalize(query)
    if len(q) < FIND_UUID_MIN_QUERY:
        raise ValueError(
            f"query too short — give at least {FIND_UUID_MIN_QUERY} "
            f"characters of the uuid")
    pool: list[tuple[_Source, UUID]] = []
    for source in _SOURCES:
        uuids = db.session.execute(
            sa.select(getattr(source.model, source.uuid_attr))
        ).scalars().all()
        pool.extend((source, u) for u in uuids)
    scored: list[tuple[float, str, _Source, UUID]] = []
    for source, u in pool:
        hex32 = u.hex
        if q == hex32:
            scored.append((1.0, "exact", source, u))
        elif q in hex32:
            # Longer fragments score higher; where the fragment sits breaks
            # ties — people quote the BEGINNING of a uuid far more often than
            # the end, and the end more often than the middle.
            score = 0.70 + 0.25 * len(q) / 32
            if hex32.startswith(q):
                score += 0.05
            elif hex32.endswith(q):
                score += 0.02
            scored.append((score, "substring", source, u))
    if not scored and len(q) >= FIND_UUID_MIN_FUZZY_QUERY:
        for source, u in pool:
            ratio = _best_window_ratio(q, u.hex)
            if ratio >= _FUZZY_THRESHOLD:
                scored.append((0.9 * ratio, "fuzzy", source, u))
    scored.sort(key=lambda m: (-m[0], m[2].kind))
    out: list[dict[str, Any]] = []
    for score, match, source, u in scored[:limit]:
        row = _row(source.model, u, source.uuid_attr)
        if row is None:
            continue  # deleted between the scan and now
        desc = source.describe(row)
        out.append({"kind": source.kind, "uuid": str(u), "match": match,
                    "confidence": round(score, 3), **desc})
    return out
