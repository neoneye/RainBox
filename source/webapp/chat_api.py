"""JSON + SSE endpoints backing the /chat page.

Reads/writes go through db.py helpers. Live updates use Postgres LISTEN/NOTIFY
rather than polling: posting a message NOTIFYs `db.CHAT_NOTIFY_CHANNEL`, and the
/chat/stream endpoint holds a dedicated psycopg connection that blocks in the
kernel on `conn.notifies(timeout=...)`, forwarding each notification to the
browser as a Server-Sent Event.
"""

from uuid import UUID

import psycopg
from flask import Response, abort, jsonify, request, stream_with_context

import db
from agents.config import (
    ASSISTANT_UUID,
    CHAT_STRUCTURED_UUID,
    CHAT_UNSTRUCTURED_UUID,
    MCP_UUID,
    QUERY_FILTER_ROUTER_UUID,
    QUERY_ROUTER_UUID,
    QUERY_UUID,
    ROUTER_UUID,
    TOOL_DEMO_UUID,
    WORKSPACE_SHELL_UUID,
)

from .core import app

# SSE idle heartbeat: how long notifies() blocks before we emit a keepalive
# comment. Doubles as the select() deadline (event-driven, no busy polling).
SSE_HEARTBEAT_SECONDS: float = 15.0

# Agents that reply to human chat messages. Each one that is a member of the
# room gets enqueued when a human posts (see _maybe_trigger_chat_agents).
CHAT_RESPONDER_UUIDS = (
    CHAT_STRUCTURED_UUID,
    CHAT_UNSTRUCTURED_UUID,
    TOOL_DEMO_UUID,
    WORKSPACE_SHELL_UUID,
    ROUTER_UUID,
    QUERY_UUID,
    QUERY_ROUTER_UUID,
    QUERY_FILTER_ROUTER_UUID,
    MCP_UUID,
    ASSISTANT_UUID,
)


def _parse_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except (ValueError, TypeError):
        abort(400, "invalid uuid")


def _maybe_trigger_chat_agents(
    room_uuid: UUID, sender_uuid: UUID, message_uuid: UUID
) -> None:
    """Enqueue a job for each responder agent (CHAT_RESPONDER_UUIDS) that
    belongs to the room, when a *human* posts in it. The human-only guard is
    what prevents an infinite loop: an agent's own reply (sender_type 'agent',
    and posted directly, not via this endpoint) never re-triggers anything.
    Requires main.py (the supervisor) to be running and a model group assigned
    to each responder agent for a reply to appear.

    The triggering message's uuid is carried in the payload so each enqueued
    item runs its own command rather than whatever is newest at dispatch time."""
    sender = db.get_chat_user(sender_uuid)
    if sender is None or sender.user_type != "human":
        return
    members = db.get_room_member_uuids(room_uuid)
    for agent_uuid in CHAT_RESPONDER_UUIDS:
        if agent_uuid in members:
            db.enqueue(
                agent_uuid,
                {"room_uuid": str(room_uuid), "message_uuid": str(message_uuid)},
            )


@app.route("/chat/api/rooms", methods=["GET", "POST"])
def chat_rooms() -> Response | tuple[Response, int]:
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        if not name:
            abort(400, "room name required")
        human = db.get_human_user()
        if human is None:
            abort(500, "no human user seeded")
        member_uuids = [_parse_uuid(raw) for raw in data.get("member_uuids", [])]
        room = db.create_chatroom(name, human.uuid, member_uuids)
        return jsonify({"uuid": str(room.uuid), "name": room.name}), 201
    return jsonify(db.list_chatrooms())


@app.route("/chat/api/rooms/details")
def chat_room_details() -> Response:
    """Per-room agent names, message count, and last-message time, for all
    rooms. Fetched lazily when a folder is selected (the folder-contents
    table); kept separate from the tree load to keep that light."""
    return jsonify(db.list_chatroom_details())


@app.route("/chat/api/tree", methods=["GET", "PUT"])
def chat_tree() -> Response | tuple[Response, int]:
    """The left-panel folder/room tree. GET hydrates {folders, rooms, version};
    PUT bulk-saves folder placement + room ordering (version-guarded). The PUT
    never creates or deletes rooms — folder/room deletion has dedicated
    endpoints (mirrors /cron/api/tree, but without room destruction)."""
    if request.method == "PUT":
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "request body must be a JSON object"}), 400
        version = data.get("version")
        if not isinstance(version, str) or not version:
            return jsonify({"ok": False,
                            "error": "missing tree 'version' (hydrate via GET first)"}), 400
        try:
            db.chat_save_tree(data.get("folders", []), data.get("rooms", []),
                              base_version=version)
        except db.ChatTreeConflict as exc:
            return jsonify({"ok": False, "error": str(exc),
                            "version": db.chat_tree_version()}), 409
        except db.ChatTreeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "version": db.chat_tree_version()})
    return jsonify(db.chat_load_tree())


@app.route("/chat/api/folders", methods=["POST"])
def chat_create_folder() -> tuple[Response, int]:
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        abort(400, "folder name required")
    parent_raw = data.get("parent_uuid")
    parent_uuid = _parse_uuid(parent_raw) if parent_raw else None
    folder = db.create_chatroom_folder(name, parent_uuid)
    return jsonify({
        "id": str(folder.uuid),
        "name": folder.name,
        "parentId": str(folder.parent_uuid) if folder.parent_uuid else None,
    }), 201


@app.route("/chat/api/folders/<folder_uuid>/delete-preview")
def chat_folder_delete_preview(folder_uuid: str) -> Response:
    fuuid = _parse_uuid(folder_uuid)
    try:
        return jsonify(db.chatroom_folder_delete_preview(fuuid))
    except LookupError:
        abort(404, "folder not found")


@app.route("/chat/api/folders/<folder_uuid>", methods=["DELETE"])
def chat_delete_folder(folder_uuid: str) -> Response:
    fuuid = _parse_uuid(folder_uuid)
    try:
        db.delete_chatroom_folder(fuuid)
    except LookupError:
        abort(404, "folder not found")
    return jsonify({"id": str(fuuid), "deleted": True})


@app.route("/chat/api/rooms/<room_uuid>/delete-preview")
def chat_room_delete_preview(room_uuid: str) -> Response:
    ruuid = _parse_uuid(room_uuid)
    try:
        return jsonify(db.chatroom_delete_preview(ruuid))
    except LookupError:
        abort(404, "room not found")


@app.route("/chat/api/rooms/<room_uuid>/rename", methods=["POST"])
def rename_chat_room(room_uuid: str) -> Response:
    ruuid = _parse_uuid(room_uuid)
    if db.get_chatroom(ruuid) is None:
        abort(404, "room not found")
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        abort(400, "name required")
    db.rename_chatroom(ruuid, name)
    return jsonify({"uuid": str(ruuid), "name": name})


@app.route("/chat/api/rooms/<room_uuid>", methods=["DELETE"])
def delete_chat_room(room_uuid: str) -> Response:
    ruuid = _parse_uuid(room_uuid)
    if db.get_chatroom(ruuid) is None:
        abort(404, "room not found")
    db.delete_chatroom(ruuid)
    return jsonify({"uuid": str(ruuid), "deleted": True})


@app.route("/chat/api/rooms/<room_uuid>/members", methods=["GET", "POST"])
def chat_room_members(room_uuid: str) -> Response:
    ruuid = _parse_uuid(room_uuid)
    if db.get_chatroom(ruuid) is None:
        abort(404, "room not found")
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        raw = data.get("user_uuid")
        if not raw:
            abort(400, "user_uuid required")
        uuser = _parse_uuid(raw)
        if db.get_chat_user(uuser) is None:
            abort(404, "user not found")
        added = db.add_room_member(ruuid, uuser)
        return jsonify(
            {"room_uuid": str(ruuid), "user_uuid": str(uuser), "added": added}
        )
    return jsonify(db.list_room_members(ruuid))


@app.route(
    "/chat/api/rooms/<room_uuid>/members/<user_uuid>", methods=["DELETE"]
)
def remove_chat_room_member(room_uuid: str, user_uuid: str) -> Response:
    ruuid = _parse_uuid(room_uuid)
    if db.get_chatroom(ruuid) is None:
        abort(404, "room not found")
    uuser = _parse_uuid(user_uuid)
    target = db.get_chat_user(uuser)
    # Defense-in-depth: the UI never offers to remove the human, but reject it
    # here too so a room can't be orphaned by a hand-crafted request.
    if target is not None and target.user_type == "human":
        abort(409, "cannot remove the human from a room")
    removed = db.remove_room_member(ruuid, uuser)
    return jsonify(
        {"room_uuid": str(ruuid), "user_uuid": str(uuser), "removed": removed}
    )


@app.route("/chat/api/agents")
def chat_agents() -> Response:
    """Agent users selectable as members when creating a room."""
    return jsonify(
        [{"uuid": str(u.uuid), "name": u.name} for u in db.list_agent_chat_users()]
    )


@app.route("/chat/api/rooms/<room_uuid>/messages", methods=["GET", "POST"])
def chat_room_messages(room_uuid: str) -> Response | tuple[Response, int]:
    ruuid = _parse_uuid(room_uuid)
    if db.get_chatroom(ruuid) is None:
        abort(404, "room not found")

    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        text = (data.get("text") or "").strip()
        if not text:
            abort(400, "text required")
        sender_raw = data.get("sender_uuid")
        if sender_raw:
            sender = _parse_uuid(sender_raw)
            if sender not in db.get_room_member_uuids(ruuid):
                abort(403, "sender is not a member of this room")
        else:
            human = db.get_human_user()
            if human is None:
                abort(500, "no human user seeded")
            sender = human.uuid
        msg = db.post_chat_message(ruuid, sender, text, db.detect_content_type(text))
        _maybe_trigger_chat_agents(ruuid, sender, msg.uuid)
        return jsonify({"id": msg.id, "uuid": str(msg.uuid)}), 201

    try:
        after_id = int(request.args.get("after", "0"))
    except ValueError:
        after_id = 0
    return jsonify(db.list_room_messages(ruuid, after_id))


@app.route("/chat/api/rooms/<room_uuid>/messages/<int:message_id>")
def chat_room_message(room_uuid: str, message_id: int) -> Response:
    """One message row by id — used by the browser to refetch a streamed row
    whose updated text was too large to inline in the NOTIFY payload."""
    ruuid = _parse_uuid(room_uuid)
    msg = db.get_room_message(ruuid, message_id)
    if msg is None:
        abort(404, "message not found")
    return jsonify(msg)


@app.route("/chat/api/assistant/runs/<int:run_id>")
def assistant_run_steps(run_id: int) -> Response:
    """An assistant run plus its step trace. The chat UI calls this to render the
    inline plan/action/observation behind a thin `debug-assistant` pointer row
    (which carries only {run_id, step_index}); the tables are the source of
    truth."""
    run = db.get_assistant_run(run_id)
    if run is None:
        abort(404, "assistant run not found")
    steps = db.list_assistant_steps(run_id)
    return jsonify(
        {
            "run": {
                "id": run.id,
                "uuid": str(run.uuid),
                "status": run.status,
                "step_limit": run.step_limit,
                "final_summary": run.final_summary,
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            },
            "steps": [
                {
                    "id": s.id,
                    "step_index": s.step_index,
                    "phase": s.phase,
                    "action": s.action,
                    "reason": s.reason,
                    "args": s.args,
                    "observation_preview": s.observation_preview,
                    "error": s.error,
                    "created_at": s.created_at.isoformat() if s.created_at else None,
                }
                for s in steps
            ],
        }
    )


@app.route("/chat/api/messages/<message_uuid>/feedback", methods=["POST"])
def post_feedback(message_uuid: str) -> Response | tuple[Response, int]:
    """Capture an upvote/downvote on an agent's user-facing chat reply.

    Validations:
    - message must exist (404 otherwise)
    - message kind must be "message" (400)
    - sender must be an agent (400)
    - rating must be "upvote" or "downvote" (400)
    """
    msg_uuid = _parse_uuid(message_uuid)
    msg = db.db.session.query(db.ChatMessage).filter_by(uuid=msg_uuid).first()
    if msg is None:
        abort(404, "message not found")
    if msg.kind != "message":
        abort(400, "feedback can only be posted on a user-facing message row")
    sender = db.get_chat_user(msg.sender_uuid)
    if sender is None or sender.user_type != "agent":
        abort(400, "feedback can only be posted on agent messages")

    data = request.get_json(silent=True) or {}
    rating = data.get("rating")
    if rating not in ("upvote", "downvote"):
        abort(400, "rating must be 'upvote' or 'downvote'")
    comment_raw = data.get("comment")
    comment = comment_raw.strip() if isinstance(comment_raw, str) else None
    if comment == "":
        comment = None

    human = db.get_human_user()
    fb = db.create_feedback_event(
        room_uuid=msg.room_uuid,
        message_uuid=msg.uuid,
        agent_uuid=msg.sender_uuid,
        rating=rating,
        comment=comment,
        created_by_uuid=human.uuid if human is not None else None,
    )
    try:
        db.link_downvote_to_retrieval_targets(fb.uuid)
    except Exception:
        app.logger.exception(
            "downvote telemetry wrapper raised; suppressing so "
            "feedback succeeds"
        )
        try:
            db.db.session.rollback()
        except Exception:
            pass
    return jsonify({"uuid": str(fb.uuid), "rating": fb.rating}), 201


@app.route("/chat/stream")
def chat_stream() -> Response:
    """Server-Sent Events: one `data:` line per new chat message (the NOTIFY
    payload, {room_uuid, message_id}); a `: keepalive` comment every heartbeat."""

    @stream_with_context
    def events():
        conn = psycopg.connect(db.psycopg_dsn(), autocommit=True)
        try:
            # Channel is a fixed internal constant, safe to interpolate (LISTEN
            # takes an identifier, which can't be a bind parameter).
            conn.execute(f"LISTEN {db.CHAT_NOTIFY_CHANNEL}")
            yield ": connected\n\n"
            while True:
                emitted = False
                for note in conn.notifies(timeout=SSE_HEARTBEAT_SECONDS):
                    emitted = True
                    yield f"data: {note.payload}\n\n"
                if not emitted:
                    yield ": keepalive\n\n"
        finally:
            conn.close()

    resp = Response(events(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"  # disable proxy buffering if any
    return resp
