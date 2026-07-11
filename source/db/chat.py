"""Chat persistence.

Split out of db.py. Holds chatrooms, chat users, memberships, messages, the
LISTEN/NOTIFY helpers, per-room workspace-shell state, default seeding
(seed_chat_defaults), and the cron-events helper (post_cron_event). Re-exported
from db for import compatibility.
"""
import hashlib
import json
import logging
from collections import defaultdict
from typing import Any
from uuid import UUID

import sqlalchemy as sa

from db.models import (
    CHAT_NOTIFY_CHANNEL,
    CHAT_NOTIFY_MAX_TEXT,
    CRON_ROOM_UUID,
    CRON_SYSTEM_NAME,
    CRON_SYSTEM_UUID,
    AssistantWriteIntent,
    ChatMessage,
    ChatUser,
    Chatroom,
    ChatroomFolder,
    ChatroomMember,
    FeedbackEvent,
    WorkspaceShellState,
    db,
)

logger = logging.getLogger(__name__)

# Message kinds that represent an agent's *final* output for a turn. Posting any
# of these clears the sender's lingering kind="progress" bubbles. "message" is a
# real conversational reply (fed back to LLMs as transcript context); "notice"
# is an operational message (e.g. "the model server is down") — visible in the
# UI but deliberately NOT a conversation turn, so transcript builders that filter
# to kind=="message" exclude it and models can't parrot it back.
_TERMINAL_KINDS: frozenset[str] = frozenset({"message", "notice"})


def get_human_user() -> ChatUser | None:
    """The single human operator (this demo always seeds exactly one)."""
    return (
        db.session.query(ChatUser)
        .filter(ChatUser.user_type == "human")
        .order_by(ChatUser.id.asc())
        .first()
    )


def get_chat_user(user_uuid: UUID) -> ChatUser | None:
    return (
        db.session.query(ChatUser).filter(ChatUser.uuid == user_uuid).one_or_none()
    )


def list_agent_chat_users() -> list[ChatUser]:
    return (
        db.session.query(ChatUser)
        .filter(ChatUser.user_type == "agent")
        .order_by(ChatUser.name.asc())
        .all()
    )


def get_chatroom(room_uuid: UUID) -> Chatroom | None:
    return (
        db.session.query(Chatroom).filter(Chatroom.uuid == room_uuid).one_or_none()
    )


def rename_chatroom(room_uuid: UUID, name: str) -> None:
    room = get_chatroom(room_uuid)
    if room is None:
        raise LookupError(f"chatroom {room_uuid} not found")
    room.name = name
    db.session.commit()


def delete_chatroom(room_uuid: UUID) -> None:
    """Delete a room and everything that hangs off it. chat_message,
    chatroom_member and workspace_shell_state all declare ON DELETE CASCADE, so
    deleting the chatroom row removes its messages and members along with it."""
    room = get_chatroom(room_uuid)
    if room is None:
        raise LookupError(f"chatroom {room_uuid} not found")
    db.session.delete(room)
    db.session.commit()


def list_room_members(room_uuid: UUID) -> list[dict[str, Any]]:
    """Members of a room as display dicts {uuid, name, user_type}, humans first
    then agents, each group by name."""
    rows = (
        db.session.query(ChatUser)
        .join(ChatroomMember, ChatroomMember.user_uuid == ChatUser.uuid)
        .filter(ChatroomMember.room_uuid == room_uuid)
        .order_by(ChatUser.user_type.desc(), ChatUser.name.asc())
        .all()
    )
    return [
        {"uuid": str(u.uuid), "name": u.name, "user_type": u.user_type} for u in rows
    ]


def get_room_member_uuids(room_uuid: UUID) -> list[UUID]:
    rows = (
        db.session.query(ChatroomMember)
        .filter(ChatroomMember.room_uuid == room_uuid)
        .all()
    )
    return [r.user_uuid for r in rows]


def add_room_member(room_uuid: UUID, user_uuid: UUID) -> bool:
    """Add a user to a room. Idempotent: returns True if a new membership row
    was created, False if the user was already a member (no duplicate inserted).
    The (room_uuid, user_uuid) unique index also guards against duplicates."""
    existing = (
        db.session.query(ChatroomMember)
        .filter(
            ChatroomMember.room_uuid == room_uuid,
            ChatroomMember.user_uuid == user_uuid,
        )
        .first()
    )
    if existing is not None:
        return False
    db.session.add(ChatroomMember(room_uuid=room_uuid, user_uuid=user_uuid))
    db.session.commit()
    return True


def remove_room_member(room_uuid: UUID, user_uuid: UUID) -> bool:
    """Remove a user from a room. Returns True if a membership row was deleted,
    False if the user wasn't a member. Messages are untouched (chat_message has
    no FK on sender_uuid), so a removed agent's history stays in the room."""
    deleted = (
        db.session.query(ChatroomMember)
        .filter(
            ChatroomMember.room_uuid == room_uuid,
            ChatroomMember.user_uuid == user_uuid,
        )
        .delete()
    )
    db.session.commit()
    return deleted > 0


def create_chatroom(
    name: str, created_by: UUID, member_uuids: list[UUID],
    room_type: str = "agents",
) -> Chatroom:
    """Create a room. The creator (the human) is always a member; `member_uuids`
    are the additional participants (agents). Duplicates are ignored.
    `room_type` is "agents" (group chat with responder agents) or "direct"
    (one-to-one operator<->model chat; see DirectChatAgent)."""
    if room_type not in ("agents", "direct"):
        raise ValueError(f"invalid room_type: {room_type!r}")
    room = Chatroom(name=name, created_by=created_by, room_type=room_type)
    db.session.add(room)
    db.session.flush()  # assign room.uuid before inserting members
    seen: set[UUID] = set()
    for member in [created_by, *member_uuids]:
        if member in seen:
            continue
        seen.add(member)
        db.session.add(ChatroomMember(room_uuid=room.uuid, user_uuid=member))
    db.session.commit()
    return room


# Sentinel for set_chatroom_settings: distinguishes "leave this field alone"
# from "set it to None" (clearing the model).
_UNSET: Any = object()


def set_chatroom_settings(
    room_uuid: UUID,
    *,
    system_prompt: str = _UNSET,
    model_uuid: UUID | None = _UNSET,
) -> Chatroom:
    """Update a direct room's settings; only the fields passed are changed
    (model_uuid=None clears the model). Applied mid-conversation: the next
    direct-chat turn reads the room row fresh. Raises LookupError if the room
    is gone, ValueError if it isn't a direct room."""
    room = get_chatroom(room_uuid)
    if room is None:
        raise LookupError(f"chatroom {room_uuid} not found")
    if room.room_type != "direct":
        raise ValueError("settings apply to direct rooms only")
    if system_prompt is not _UNSET:
        room.system_prompt = system_prompt
    if model_uuid is not _UNSET:
        room.model_uuid = model_uuid
    db.session.commit()
    return room


def edit_chat_message(message_id: int, text: str) -> ChatMessage:
    """Replace a message's text (direct-room message editing). Re-detects
    content_type and NOTIFYs with the row's kind + streaming:false + the new
    text, so open tabs update the bubble in place via the existing streaming
    upsert path — no new SSE machinery. Raises LookupError if the row is gone,
    ValueError on a non-"message" kind or a row still streaming."""
    msg = db.session.get(ChatMessage, message_id)
    if msg is None:
        raise LookupError(f"chat message {message_id} not found")
    if msg.kind != "message":
        raise ValueError("only kind='message' rows are editable")
    if msg.streaming:
        raise ValueError("cannot edit a message that is still streaming")
    msg.text = text
    msg.content_type = detect_content_type(text)
    db.session.flush()
    _chat_notify(
        room_uuid=msg.room_uuid,
        message_id=msg.id,
        kind=msg.kind,
        streaming=False,
        text=text,
    )
    db.session.commit()
    return msg


def delete_chat_message(message_id: int) -> None:
    """Delete a message row (direct-room message deletion) and NOTIFY so open
    tabs drop the bubble live. Reuses the deleted_progress_ids mechanism — the
    client removes DOM nodes by id regardless of kind — with message_id=0
    marking a pure deletion (no new message), so background rooms don't count
    it as unread. Raises LookupError if the row is gone, ValueError on a
    non-"message" kind or a row still streaming."""
    msg = db.session.get(ChatMessage, message_id)
    if msg is None:
        raise LookupError(f"chat message {message_id} not found")
    if msg.kind != "message":
        raise ValueError("only kind='message' rows are deletable")
    if msg.streaming:
        raise ValueError("cannot delete a message that is still streaming")
    room_uuid = msg.room_uuid
    db.session.delete(msg)
    db.session.flush()
    _chat_notify(
        room_uuid=room_uuid,
        message_id=0,
        deleted_progress_ids=[message_id],
    )
    db.session.commit()


class ChatTreeError(ValueError):
    """A chat folder/room tree payload failed structural validation (bad uuid,
    dangling/cyclic folder ref, unknown room folderId, missing/unknown room).
    Callers turn this into a 4xx rather than a 500."""


class ChatTreeConflict(Exception):
    """The chat tree changed since the caller hydrated (stale base_version on
    save). Callers map this to HTTP 409 so the client re-hydrates instead of
    clobbering another writer's changes."""


def _to_uuid(value: Any) -> UUID | None:
    """Parse to a UUID (normalizing case/format) or None. Lets callers key
    dedup/reference checks on the normalized value (mirrors db.cron._to_uuid;
    duplicated here to avoid a db.chat <-> db.cron import cycle)."""
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def create_chatroom_folder(name: str, parent_uuid: UUID | None = None) -> ChatroomFolder:
    """Create a left-panel folder. New folders are appended after existing
    siblings under the same parent (position = current sibling count)."""
    sibling_count = db.session.execute(
        sa.select(sa.func.count()).select_from(ChatroomFolder)
        .where(ChatroomFolder.parent_uuid.is_(parent_uuid) if parent_uuid is None
               else ChatroomFolder.parent_uuid == parent_uuid)
    ).scalar() or 0
    folder = ChatroomFolder(name=name, parent_uuid=parent_uuid, position=int(sibling_count))
    db.session.add(folder)
    db.session.commit()
    return folder


def list_chatroom_folders() -> list[dict[str, Any]]:
    """All folders as {id, name, parentId}, ordered by (position, id)."""
    folders = db.session.execute(
        sa.select(ChatroomFolder).order_by(ChatroomFolder.position, ChatroomFolder.id)
    ).scalars().all()
    return [
        {
            "id": str(f.uuid),
            "name": f.name,
            "parentId": str(f.parent_uuid) if f.parent_uuid else None,
        }
        for f in folders
    ]


def chat_tree_version() -> str:
    """Opaque version token over the user-managed tree fields only (folder:
    uuid/name/parentId/position; room: uuid/folderId/position). Volatile fields
    (a room's message count / last id) are excluded, so a new message never
    invalidates an open page — only a structural edit by another writer does.
    The page hydrates with this token and echoes it on PUT (409 if stale)."""
    folders = db.session.execute(
        sa.select(ChatroomFolder).order_by(ChatroomFolder.uuid)
    ).scalars().all()
    rooms = db.session.execute(
        sa.select(Chatroom).order_by(Chatroom.uuid)
    ).scalars().all()
    payload = [
        [[str(f.uuid), f.name,
          str(f.parent_uuid) if f.parent_uuid else None, f.position]
         for f in folders],
        [[str(r.uuid),
          str(r.folder_uuid) if r.folder_uuid else None, r.position]
         for r in rooms],
    ]
    blob = json.dumps(payload, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def chat_load_tree() -> dict[str, Any]:
    """The whole left-panel tree: folders, rooms (with member_count/last id +
    folderId, reusing list_chatrooms), and the version token."""
    return {
        "folders": list_chatroom_folders(),
        "rooms": list_chatrooms(),
        "version": chat_tree_version(),
    }


def validate_chat_tree(
    folders: list[dict[str, Any]], rooms: list[dict[str, Any]]
) -> None:
    """Structural integrity check for an incoming chat tree, run before any DB
    write (mirrors validate_cron_tree). Rejects bad uuids, duplicate/dangling/
    cyclic folder refs, a room folderId that names no folder in the payload, and
    a room uuid that collides with a folder id (a node is identified globally by
    uuid). Does NOT touch the DB; raises ChatTreeError on the first problem."""
    if not isinstance(folders, list):
        raise ChatTreeError(f"'folders' must be a list, got {type(folders).__name__}")
    if not isinstance(rooms, list):
        raise ChatTreeError(f"'rooms' must be a list, got {type(rooms).__name__}")
    parent_of: dict[UUID, UUID | None] = {}
    for f in folders:
        if not isinstance(f, dict):
            raise ChatTreeError(f"folder entry must be an object, got {type(f).__name__}")
        fid = _to_uuid(f.get("id"))
        if fid is None:
            raise ChatTreeError(f"folder id is not a uuid: {f.get('id')!r}")
        if fid in parent_of:
            raise ChatTreeError(f"duplicate folder id: {fid}")
        if not isinstance(f.get("name", ""), str):
            raise ChatTreeError(f"folder {fid} name must be a string")
        pid_raw = f.get("parentId")
        if pid_raw is None:
            pid: UUID | None = None
        else:
            pid = _to_uuid(pid_raw)
            if pid is None:
                raise ChatTreeError(f"folder {fid} parentId is not a uuid: {pid_raw!r}")
        parent_of[fid] = pid
    for fid, pid in parent_of.items():
        if pid is not None and pid not in parent_of:
            raise ChatTreeError(f"folder {fid} references missing parent {pid}")
    # Acyclic: walking parents from any folder must terminate at a root.
    for start in parent_of:
        seen: set[UUID] = set()
        cur = parent_of[start]
        while cur is not None:
            if cur == start or cur in seen:
                raise ChatTreeError(f"folder cycle detected involving {start}")
            seen.add(cur)
            cur = parent_of.get(cur)
    room_uuids: set[UUID] = set()
    for r in rooms:
        if not isinstance(r, dict):
            raise ChatTreeError(f"room entry must be an object, got {type(r).__name__}")
        ru = _to_uuid(r.get("uuid"))
        if ru is None:
            raise ChatTreeError(f"room uuid is not a uuid: {r.get('uuid')!r}")
        if ru in room_uuids:
            raise ChatTreeError(f"duplicate room uuid: {ru}")
        if ru in parent_of:
            raise ChatTreeError(f"room uuid {ru} collides with a folder id")
        room_uuids.add(ru)
        fld_raw = r.get("folderId")
        if fld_raw is not None:
            fld = _to_uuid(fld_raw)
            if fld is None:
                raise ChatTreeError(f"room {ru} folderId is not a uuid: {fld_raw!r}")
            if fld not in parent_of:
                raise ChatTreeError(f"room {ru} references missing folder {fld}")


def chat_save_tree(
    folders: list[dict[str, Any]], rooms: list[dict[str, Any]],
    *, base_version: str | None = None,
) -> None:
    """Upsert the left-panel tree. Folders are created/updated/reordered by
    uuid (list order becomes `position`); a folder uuid absent from the payload
    is deleted (only ever an emptied folder — room placement is reassigned
    first by the caller). Rooms are NEVER created or deleted here: only their
    `folder_uuid` + `position` change, and the payload MUST list exactly the
    existing rooms. A missing room would otherwise be silently dropped (and its
    messages with it via cascade) on a truncated payload — destructive folder/
    room deletion goes through the dedicated endpoints instead.

    base_version, when given, is the chat_tree_version() the caller hydrated
    with: a stale token raises ChatTreeConflict (HTTP 409 upstream) — checked
    before structural validation so a concurrent edit surfaces as 409, not 400.
    Validates structure next (raises ChatTreeError before any mutation)."""
    if base_version is not None and base_version != chat_tree_version():
        raise ChatTreeConflict("chat tree changed since it was loaded")
    validate_chat_tree(folders, rooms)
    existing_f = {
        f.uuid: f for f in db.session.execute(sa.select(ChatroomFolder)).scalars().all()
    }
    existing_r = {
        r.uuid: r for r in db.session.execute(sa.select(Chatroom)).scalars().all()
    }
    incoming_rooms = {UUID(r["uuid"]) for r in rooms}
    missing = set(existing_r) - incoming_rooms
    if missing:
        raise ChatTreeError(
            f"chat tree save omitted {len(missing)} existing room(s) — refusing "
            f"(the tree save never deletes rooms)"
        )
    unknown = incoming_rooms - set(existing_r)
    if unknown:
        raise ChatTreeError(f"chat tree save references {len(unknown)} unknown room(s)")
    # Folders: update existing by uuid, insert new, delete the rest.
    seen_f: set[UUID] = set()
    for i, f in enumerate(folders):
        fu = UUID(f["id"])
        seen_f.add(fu)
        row = existing_f.get(fu)
        if row is None:
            row = ChatroomFolder(uuid=fu)
            db.session.add(row)
        row.name = f.get("name", "")
        row.parent_uuid = UUID(f["parentId"]) if f.get("parentId") else None
        row.position = i
    for fu, row in existing_f.items():
        if fu not in seen_f:
            db.session.delete(row)
    # Rooms: only placement + order (never name/membership/messages).
    for i, r in enumerate(rooms):
        row = existing_r[UUID(r["uuid"])]
        row.folder_uuid = UUID(r["folderId"]) if r.get("folderId") else None
        row.position = i
    db.session.commit()


def _descendant_chatroom_folder_uuids(folder_uuid: UUID) -> list[UUID]:
    """`folder_uuid` plus every folder nested under it (any depth). Cycle-guarded
    via a visited set so a malformed parent loop can't spin forever."""
    children: dict[UUID | None, list[UUID]] = defaultdict(list)
    for f in db.session.execute(sa.select(ChatroomFolder)).scalars().all():
        children[f.parent_uuid].append(f.uuid)
    result: list[UUID] = []
    seen: set[UUID] = set()
    stack = [folder_uuid]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        result.append(cur)
        stack.extend(children.get(cur, []))
    return result


def chatroom_folder_delete_preview(folder_uuid: UUID) -> dict[str, Any]:
    """Authoritative rollup for the delete-confirm dialog: the folder's name and
    the total chatrooms + messages that a recursive delete would remove (across
    all nested subfolders). Raises LookupError if the folder is gone."""
    folder = db.session.execute(
        sa.select(ChatroomFolder).where(ChatroomFolder.uuid == folder_uuid)
    ).scalar_one_or_none()
    if folder is None:
        raise LookupError(f"chatroom folder {folder_uuid} not found")
    folder_uuids = _descendant_chatroom_folder_uuids(folder_uuid)
    room_uuids = db.session.execute(
        sa.select(Chatroom.uuid).where(Chatroom.folder_uuid.in_(folder_uuids))
    ).scalars().all()
    message_count = 0
    if room_uuids:
        message_count = int(db.session.execute(
            sa.select(sa.func.count()).select_from(ChatMessage)
            .where(ChatMessage.room_uuid.in_(room_uuids))
        ).scalar() or 0)
    return {
        "folder_name": folder.name,
        "room_count": len(room_uuids),
        "message_count": message_count,
    }


def delete_chatroom_folder(folder_uuid: UUID) -> None:
    """Recursively delete a folder: every nested subfolder, every chatroom in
    that subtree, and (via the chatroom row's ON DELETE CASCADE) those rooms'
    messages, members, and workspace-shell state. Raises LookupError if the
    folder is gone. This is the destructive op the type-to-confirm dialog
    guards — chat_save_tree never deletes rooms."""
    folder = db.session.execute(
        sa.select(ChatroomFolder).where(ChatroomFolder.uuid == folder_uuid)
    ).scalar_one_or_none()
    if folder is None:
        raise LookupError(f"chatroom folder {folder_uuid} not found")
    folder_uuids = _descendant_chatroom_folder_uuids(folder_uuid)
    rooms = db.session.execute(
        sa.select(Chatroom).where(Chatroom.folder_uuid.in_(folder_uuids))
    ).scalars().all()
    for room in rooms:
        db.session.delete(room)  # cascades messages + members + workspace_shell_state
    db.session.execute(
        sa.delete(ChatroomFolder).where(ChatroomFolder.uuid.in_(folder_uuids))
    )
    db.session.commit()


def chatroom_delete_preview(room_uuid: UUID) -> dict[str, Any]:
    """Rollup for a single-room delete-confirm dialog: the room's name and how
    many messages it holds. Raises LookupError if the room is gone."""
    room = get_chatroom(room_uuid)
    if room is None:
        raise LookupError(f"chatroom {room_uuid} not found")
    message_count = int(db.session.execute(
        sa.select(sa.func.count()).select_from(ChatMessage)
        .where(ChatMessage.room_uuid == room_uuid)
    ).scalar() or 0)
    return {"room_name": room.name, "message_count": message_count}


def list_chatrooms() -> list[dict[str, Any]]:
    """Rooms for the left panel, ordered by saved position (then id), each with
    member count, last-message id, and its folder placement (folderId, null =
    top level)."""
    rooms = (
        db.session.query(Chatroom)
        .order_by(Chatroom.position.asc(), Chatroom.id.asc())
        .all()
    )
    member_counts = dict(
        db.session.query(ChatroomMember.room_uuid, sa.func.count())
        .group_by(ChatroomMember.room_uuid)
        .all()
    )
    last_ids = dict(
        db.session.query(ChatMessage.room_uuid, sa.func.max(ChatMessage.id))
        .group_by(ChatMessage.room_uuid)
        .all()
    )
    return [
        {
            "uuid": str(r.uuid),
            "name": r.name,
            "member_count": int(member_counts.get(r.uuid, 0)),
            "last_message_id": int(last_ids.get(r.uuid) or 0),
            "folderId": str(r.folder_uuid) if r.folder_uuid else None,
            "room_type": r.room_type,
            # Direct rooms only: lets the client know whether the room has a
            # model without an extra fetch (drives the auto-open of Settings).
            "model_uuid": str(r.model_uuid) if r.model_uuid else None,
        }
        for r in rooms
    ]


def list_chatroom_details() -> list[dict[str, Any]]:
    """Per-room stats for the folder-contents table: the room's agent member
    names (non-human members), its message count, and its last-message time.
    Fetched lazily on folder selection (kept out of list_chatrooms so the
    frequently re-fetched tree load stays light). One query per aggregate."""
    agents_by_room: dict[UUID, list[str]] = defaultdict(list)
    for room_uuid, name in (
        db.session.query(ChatroomMember.room_uuid, ChatUser.name)
        .join(ChatUser, ChatUser.uuid == ChatroomMember.user_uuid)
        .filter(ChatUser.user_type != "human")
        .order_by(ChatroomMember.room_uuid, ChatUser.name.asc(), ChatUser.id.asc())
        .all()
    ):
        agents_by_room[room_uuid].append(name)
    message_counts = dict(
        db.session.query(ChatMessage.room_uuid, sa.func.count(ChatMessage.id))
        .group_by(ChatMessage.room_uuid)
        .all()
    )
    last_at = dict(
        db.session.query(ChatMessage.room_uuid, sa.func.max(ChatMessage.created_at))
        .group_by(ChatMessage.room_uuid)
        .all()
    )
    rooms = db.session.query(Chatroom).all()
    return [
        {
            "uuid": str(r.uuid),
            "agents": agents_by_room.get(r.uuid, []),
            "message_count": int(message_counts.get(r.uuid, 0)),
            "last_message_at": (
                last_at[r.uuid].strftime("%Y-%m-%d %H:%M")
                if last_at.get(r.uuid) is not None
                else None
            ),
        }
        for r in rooms
    ]


def list_room_messages(room_uuid: UUID, after_id: int = 0) -> list[dict[str, Any]]:
    """Messages in a room with id > after_id, oldest first, sender resolved.
    Each row also carries the latest user feedback rating ("upvote" /
    "downvote" / None) so a reload restores the button state in the UI."""
    rows = (
        db.session.query(ChatMessage)
        .filter(ChatMessage.room_uuid == room_uuid, ChatMessage.id > after_id)
        .order_by(ChatMessage.id.asc())
        .all()
    )
    sender_uuids = {r.sender_uuid for r in rows}
    users: dict[UUID, ChatUser] = {}
    if sender_uuids:
        users = {
            u.uuid: u
            for u in db.session.query(ChatUser)
            .filter(ChatUser.uuid.in_(sender_uuids))
            .all()
        }
    # Latest rating per message uuid. Iterate oldest→newest so dict assignment
    # leaves the most recent rating per message at the end.
    latest_feedback: dict[UUID, str] = {}
    msg_uuids = [r.uuid for r in rows]
    if msg_uuids:
        for muuid, rating in (
            db.session.query(FeedbackEvent.message_uuid, FeedbackEvent.rating)
            .filter(FeedbackEvent.message_uuid.in_(msg_uuids))
            .order_by(FeedbackEvent.id.asc())
            .all()
        ):
            latest_feedback[muuid] = rating
    # Live write-intent state for proposal messages (meta.write_intent set), so a
    # card reflects a confirm/reject performed on /assistant. One batched lookup.
    intent_state: dict[str, str] = {}
    # A completed write's result may carry a `link` to what it created (e.g. a
    # reminder's /cron?id=... job); surfaced as meta.result_link so the card can
    # link to it on reload, not just right after the click.
    intent_result_link: dict[str, str] = {}
    wanted: list[UUID] = []
    for r in rows:
        wid = (r.meta or {}).get("write_intent")
        if wid:
            try:
                wanted.append(UUID(str(wid)))
            except ValueError:
                pass
    if wanted:
        for iu, st, res in (
            db.session.query(AssistantWriteIntent.uuid, AssistantWriteIntent.state,
                             AssistantWriteIntent.result)
            .filter(AssistantWriteIntent.uuid.in_(wanted))
            .all()
        ):
            intent_state[str(iu)] = st
            link = res.get("link") if isinstance(res, dict) else None
            if link:
                intent_result_link[str(iu)] = link
    out: list[dict[str, Any]] = []
    for r in rows:
        sender = users.get(r.sender_uuid)
        meta = dict(r.meta or {})
        wid = meta.get("write_intent")
        if wid and str(wid) in intent_state:
            meta["intent_state"] = intent_state[str(wid)]
        if wid and str(wid) in intent_result_link:
            meta["result_link"] = intent_result_link[str(wid)]
        out.append(
            {
                "id": r.id,
                "uuid": str(r.uuid),
                "sender_uuid": str(r.sender_uuid),
                "sender_name": sender.name if sender else "(unknown)",
                "sender_type": sender.user_type if sender else "agent",
                "text": r.text,
                "content_type": r.content_type,
                "kind": r.kind,
                "streaming": r.streaming,
                "timestamp": r.created_at.strftime("%Y-%m-%d %H:%M"),
                "feedback": latest_feedback.get(r.uuid),
                "meta": meta,
            }
        )
    return out


def detect_content_type(text: str) -> str:
    """Classify a message body: "json" if it parses as JSON, else "markdown".
    Used for human-posted messages; the chat agent declares its own type."""
    try:
        json.loads(text)
    except (ValueError, TypeError):
        return "markdown"
    return "json"


def _chat_event_payload(
    *,
    room_uuid: UUID,
    message_id: int,
    deleted_progress_ids: list[int] | None = None,
    kind: str | None = None,
    streaming: bool | None = None,
    text: str | None = None,
) -> dict[str, Any]:
    """Build the chat_events NOTIFY payload dict.

    A plain insert carries only {room_uuid, message_id, deleted_progress_ids} —
    the browser then fetches rows after its cursor (unchanged legacy path). A
    streaming insert/update additionally carries {kind, streaming, text?} so the
    browser can upsert that one bubble in place. `text` is inlined only when it
    fits under CHAT_NOTIFY_MAX_TEXT (Postgres caps NOTIFY at ~8000 bytes); past
    that it is omitted and the browser refetches the row by id (signalled by
    `text` absent while `streaming` is present)."""
    payload: dict[str, Any] = {
        "room_uuid": str(room_uuid),
        "message_id": message_id,
        "deleted_progress_ids": deleted_progress_ids or [],
    }
    if streaming is not None:
        payload["streaming"] = streaming
        payload["kind"] = kind
        if text is not None and len(text.encode("utf-8")) <= CHAT_NOTIFY_MAX_TEXT:
            payload["text"] = text
    return payload


def _chat_notify(**kwargs: Any) -> None:
    """Emit one chat_events NOTIFY (must run inside the writing transaction)."""
    db.session.execute(
        sa.text("SELECT pg_notify(:channel, :payload)"),
        {
            "channel": CHAT_NOTIFY_CHANNEL,
            "payload": json.dumps(_chat_event_payload(**kwargs)),
        },
    )


def post_chat_message(
    room_uuid: UUID,
    sender_uuid: UUID,
    text: str,
    content_type: str = "markdown",
    kind: str = "message",
    streaming: bool = False,
    meta: dict | None = None,
) -> ChatMessage:
    """Insert a message and NOTIFY the chat channel in the same transaction, so
    every connected SSE stream is pushed the new message id on commit.

    `kind` tags the message's role ("message" by default; e.g. "thinking",
    "debug-router", or "progress" for diagnostic / in-flight-status output).

    `streaming=True` marks a row whose `text` will grow in place via
    update_chat_message (token-by-token); the NOTIFY then carries the streaming
    flag + kind so browsers create the bubble in upsert mode.

    `meta` carries an optional structured attachment (e.g. write-proposal card
    data with write_intent, capability, step_link); defaults to {}.

    When `kind` is a *terminal* agent output (`"message"` or `"notice"` — a
    real reply or an operational notice such as "the model server is down"),
    the same transaction also deletes the sender's own `kind="progress"` rows
    in this room — so progress bubbles vanish the moment the agent actually
    replies or gives up. The deleted ids are carried in the NOTIFY payload
    (`deleted_progress_ids`) so open browsers can drop the corresponding DOM
    nodes live."""
    msg = ChatMessage(
        room_uuid=room_uuid,
        sender_uuid=sender_uuid,
        text=text,
        content_type=content_type,
        kind=kind,
        streaming=streaming,
        meta=meta or {},
    )
    db.session.add(msg)
    db.session.flush()  # assign msg.id for the notify payload

    deleted_progress_ids: list[int] = []
    if kind in _TERMINAL_KINDS:
        result = db.session.execute(
            sa.text(
                "DELETE FROM chat_message "
                "WHERE room_uuid = :r AND sender_uuid = :s AND kind = 'progress' "
                "RETURNING id"
            ),
            {"r": room_uuid, "s": sender_uuid},
        )
        deleted_progress_ids = [row[0] for row in result]

    _chat_notify(
        room_uuid=room_uuid,
        message_id=msg.id,
        deleted_progress_ids=deleted_progress_ids,
        kind=kind if streaming else None,
        streaming=streaming if streaming else None,
        text=text if streaming else None,
    )
    db.session.commit()
    return msg


def update_chat_message(
    message_id: int, text: str, *, streaming: bool
) -> None:
    """Replace a row's `text` (and `streaming` flag) and NOTIFY in one
    transaction — the in-place update used while streaming a reply. The NOTIFY
    carries the row's kind + the new text (or a refetch signal when too long)
    so browsers update that bubble live. No-op if the row is gone."""
    msg = db.session.get(ChatMessage, message_id)
    if msg is None:
        return
    msg.text = text
    msg.streaming = streaming
    db.session.flush()
    _chat_notify(
        room_uuid=msg.room_uuid,
        message_id=msg.id,
        kind=msg.kind,
        streaming=streaming,
        text=text,
    )
    db.session.commit()


def get_room_message(room_uuid: UUID, message_id: int) -> dict[str, Any] | None:
    """One message row (same dict shape as list_room_messages), or None if it
    isn't in this room. Used by the browser to refetch a streamed row whose
    text was too large to inline in the NOTIFY payload."""
    rows = list_room_messages(room_uuid, after_id=message_id - 1)
    for r in rows:
        if r["id"] == message_id:
            return r
    return None


def post_progress(room_uuid: UUID, sender_uuid: UUID, text: str) -> ChatMessage:
    """Append a kind='progress' status row for an agent's in-flight work.
    Delivered live via the chat NOTIFY channel. Cleared automatically when
    the same sender next posts a kind='message' reply in the same room
    (see post_chat_message)."""
    return post_chat_message(room_uuid, sender_uuid, text, kind="progress")


def get_workspace_shell_state(room_uuid: UUID) -> "WorkspaceShellState | None":
    """The persisted workspace-shell state for a room, or None if the room has run nothing yet."""
    return db.session.get(WorkspaceShellState, room_uuid)


def set_workspace_shell_state(room_uuid: UUID, cwd: str, env: dict[str, str]) -> None:
    """Upsert a room's workspace-shell state (working directory + baseline env)."""
    row = db.session.get(WorkspaceShellState, room_uuid)
    if row is None:
        db.session.add(WorkspaceShellState(room_uuid=room_uuid, cwd=cwd, env=env))
    else:
        row.cwd = cwd
        row.env = env
    db.session.commit()


def seed_chat_defaults() -> None:
    """Idempotent chat seed: exactly one human operator, an agent chat_user per
    agent_config entry, and — only if there are no rooms yet — a starter
    'general' room so /chat isn't empty on first load."""
    from agents.config import agent_config

    human = get_human_user()
    if human is None:
        human = ChatUser(name="operator", user_type="human")
        db.session.add(human)
        db.session.flush()

    existing = {
        u.uuid: u for u in db.session.query(ChatUser).all()
    }
    for name, entry in agent_config.items():
        u = existing.get(entry["uuid"])
        if u is None:
            db.session.add(
                ChatUser(uuid=entry["uuid"], name=name, user_type="agent")
            )
        elif u.name != name:
            # Agent was renamed in agent_config (e.g. edit_document → edit_document_v1);
            # keep the chat_user row's display name in step so /chat doesn't show the
            # stale identifier.
            u.name = name
    # Persona display names: agent_config seeds a chat_user per persona role
    # (named e.g. "persona_egon"); override it with the persona's friendly name
    # ("Egon") so the transcript reads naturally. Best-effort — a missing or
    # broken agent_profiles/ must not break chat seeding.
    try:
        from agents.persona import load_personas

        for p in load_personas().values():
            cu = (
                db.session.query(ChatUser)
                .filter_by(uuid=p.agent_uuid)
                .one_or_none()
            )
            if cu is not None and cu.name != p.name:
                cu.name = p.name
    except Exception:
        logger.warning("persona chat-user name seeding skipped", exc_info=True)

    # The cron event sender: a plain agent-type chat_user with a fixed uuid,
    # deliberately NOT in agent_config so the supervisor never runs it. It only
    # authors event lines in the cron room.
    if existing.get(CRON_SYSTEM_UUID) is None:
        db.session.add(
            ChatUser(uuid=CRON_SYSTEM_UUID, name=CRON_SYSTEM_NAME, user_type="agent")
        )
    db.session.commit()

    if db.session.query(Chatroom).count() == 0:
        agent_uuids = [entry["uuid"] for entry in agent_config.values()]
        room = create_chatroom("general", human.uuid, agent_uuids[:2])
        post_chat_message(room.uuid, human.uuid, "Channel created.")

    # Dedicated, fixed-uuid "cron" room for cron events. Idempotent on its uuid
    # so it's created once and survives even when other rooms already exist.
    if db.session.query(Chatroom).filter_by(uuid=CRON_ROOM_UUID).count() == 0:
        room = Chatroom(uuid=CRON_ROOM_UUID, name="cron", created_by=human.uuid)
        db.session.add(room)
        db.session.flush()
        db.session.add(ChatroomMember(room_uuid=CRON_ROOM_UUID, user_uuid=human.uuid))
        db.session.add(ChatroomMember(room_uuid=CRON_ROOM_UUID, user_uuid=CRON_SYSTEM_UUID))
        db.session.commit()
        post_chat_message(CRON_ROOM_UUID, CRON_SYSTEM_UUID, "Cron event channel initialized.")


def post_cron_event(text: str) -> ChatMessage | None:
    """Post a one-line cron event to the dedicated cron chatroom, authored by the
    cron system sender. This is what the scheduler/firing path will call once it
    exists — e.g. a start line, an "X fired" line per fire, or an error line:

        post_cron_event('▶ fired "Backup" (command) · 2026-06-06 10:00 UTC')
        post_cron_event('✖ "Backup" failed: exit 1')

    No-op (returns None) if the cron room hasn't been seeded yet."""
    if db.session.query(Chatroom).filter_by(uuid=CRON_ROOM_UUID).count() == 0:
        return None
    return post_chat_message(CRON_ROOM_UUID, CRON_SYSTEM_UUID, text)
