"""JSON + SSE endpoints backing the /chat page.

Reads/writes go through db.py helpers. Live updates use Postgres LISTEN/NOTIFY
rather than polling: posting a message NOTIFYs `db.CHAT_NOTIFY_CHANNEL`, and the
/chat/stream endpoint holds a dedicated psycopg connection that blocks in the
kernel on `conn.notifies(timeout=...)`, forwarding each notification to the
browser as a Server-Sent Event.
"""

from datetime import datetime, timezone
from uuid import UUID

import psycopg
from flask import Response, abort, jsonify, request, stream_with_context

import db
from agents.config import (
    ASSISTANT_RUN_SUMMARIZER_UUID,
    ASSISTANT_UUID,
    ASSISTANT_WORKING_NOTICE,
    CHAT_STRUCTURED_UUID,
    CHAT_UNSTRUCTURED_UUID,
    DIRECT_CHAT_UUID,
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
            # Post the assistant's progress bubble now, at enqueue time — the
            # agent process still has to spawn and import its stack before its
            # handle() runs, so posting here is what the operator sees
            # immediately. kind="progress" is reaped when the real reply lands.
            if agent_uuid == ASSISTANT_UUID:
                db.post_chat_message(
                    room_uuid, ASSISTANT_UUID, ASSISTANT_WORKING_NOTICE,
                    kind="progress",
                )


@app.route("/chat/api/rooms", methods=["GET", "POST"])
def chat_rooms() -> Response | tuple[Response, int]:
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        if not name:
            abort(400, "room name required")
        room_type = data.get("room_type") or "agents"
        if room_type not in ("agents", "direct"):
            abort(400, "room_type must be 'agents' or 'direct'")
        human = db.get_human_user()
        if human is None:
            abort(500, "no human user seeded")
        if room_type == "direct":
            # A direct room is always exactly operator + the direct-chat
            # responder; any submitted member_uuids are ignored.
            member_uuids = [DIRECT_CHAT_UUID]
        else:
            member_uuids = [_parse_uuid(raw) for raw in data.get("member_uuids", [])]
        room = db.create_chatroom(name, human.uuid, member_uuids, room_type=room_type)
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
    tree = db.chat_load_tree()
    # Read-only extra: whether a global default model exists, so the client
    # can skip the "pick a model" nudge for model-less direct rooms.
    default_model = db.get_setting("chat.default_model")
    tree["default_model_uuid"] = str(default_model) if default_model else None
    return jsonify(tree)


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


def _maybe_trigger_direct_chat(
    room_uuid: UUID, sender_uuid: UUID, message_uuid: UUID
) -> None:
    """Direct-room sibling of _maybe_trigger_chat_agents: a human post enqueues
    the direct-chat responder (and nothing else). Same human-only guard, so the
    model's own reply never re-triggers a turn."""
    sender = db.get_chat_user(sender_uuid)
    if sender is None or sender.user_type != "human":
        return
    db.enqueue(
        DIRECT_CHAT_UUID,
        {"room_uuid": str(room_uuid), "message_uuid": str(message_uuid)},
    )


@app.route("/chat/api/rooms/<room_uuid>/messages", methods=["GET", "POST"])
def chat_room_messages(room_uuid: str) -> Response | tuple[Response, int]:
    ruuid = _parse_uuid(room_uuid)
    room = db.get_chatroom(ruuid)
    if room is None:
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
        if room.room_type == "direct":
            _maybe_trigger_direct_chat(ruuid, sender, msg.uuid)
        else:
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


@app.route("/chat/api/rooms/<room_uuid>/messages/<int:message_id>",
           methods=["PUT", "DELETE"])
def edit_chat_room_message(room_uuid: str, message_id: int) -> Response:
    """Edit (PUT) or delete (DELETE) a message — direct-room-only affordances
    (the operator can rewrite or remove their own and the model's earlier
    turns to steer the conversation). Refused in agent rooms; neither triggers
    a model turn."""
    ruuid = _parse_uuid(room_uuid)
    room = db.get_chatroom(ruuid)
    if room is None:
        abort(404, "room not found")
    if room.room_type != "direct":
        abort(403, "messages can only be edited in direct rooms")
    if db.get_room_message(ruuid, message_id) is None:
        abort(404, "message not found")
    if request.method == "DELETE":
        try:
            db.delete_chat_message(message_id)
        except ValueError as exc:
            abort(409, str(exc))
        return jsonify({"id": message_id, "deleted": True})
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        abort(400, "text required")
    try:
        db.edit_chat_message(message_id, text)
    except ValueError as exc:
        abort(409, str(exc))
    return jsonify(db.get_room_message(ruuid, message_id))


@app.route("/chat/api/rooms/<room_uuid>/messages/<int:message_id>/retry",
           methods=["POST"])
def retry_chat_room_message(room_uuid: str, message_id: int) -> Response:
    """Ask the model again from this turn (direct rooms only) — e.g. after a
    timeout or a low-quality answer, typically with a different model picked
    in Settings first.

    The retry anchor is the message itself when it's a human turn, else the
    last human turn before it. Everything after the anchor is deleted (the
    new reply replaces the old turn's output — the client warns first when
    that includes the operator's own later messages), then the direct-chat
    responder is enqueued on the anchor."""
    ruuid = _parse_uuid(room_uuid)
    room = db.get_chatroom(ruuid)
    if room is None:
        abort(404, "room not found")
    if room.room_type != "direct":
        abort(403, "retry is only available in direct rooms")
    msg = db.get_room_message(ruuid, message_id)
    if msg is None:
        abort(404, "message not found")
    if msg["sender_type"] == "human" and msg["kind"] == "message":
        anchor = msg
    else:
        rows = db.list_room_messages(ruuid)
        priors = [r for r in rows if r["id"] < message_id
                  and r["sender_type"] == "human" and r["kind"] == "message"]
        if not priors:
            abort(409, "no earlier user message to retry from")
        anchor = priors[-1]
    try:
        deleted = db.delete_room_messages_from(ruuid, anchor["id"] + 1)
    except ValueError as exc:
        abort(409, str(exc))
    _maybe_trigger_direct_chat(
        ruuid, UUID(anchor["sender_uuid"]), UUID(anchor["uuid"]))
    return jsonify({"ok": True, "retry_of": anchor["uuid"],
                    "deleted_ids": deleted})


# Parameter names whose values must not leave the server in an export
# (ModelConfig.arguments carries credentials like api_key).
_SECRET_PARAM_MARKERS = ("key", "token", "secret", "password")


def _redacted_parameters(args: dict) -> dict:
    return {
        k: "[redacted]"
        if any(m in k.lower() for m in _SECRET_PARAM_MARKERS) else v
        for k, v in args.items()
    }


def _export_model_info(room) -> dict | None:
    """The model a direct room currently talks to (its own setting, else the
    chat.default_model fallback), described for an export: picker label,
    provider, model name, and the resolved constructor parameters with
    credential-like values redacted. None for agents rooms, rooms with no
    model, or a model uuid that no longer resolves."""
    if room.room_type != "direct":
        return None
    target = room.model_uuid
    if target is None:
        raw = db.get_setting("chat.default_model")
        try:
            target = UUID(str(raw)) if raw else None
        except ValueError:
            target = None
    if target is None:
        return None
    try:
        provider, model_name, args = db.resolved_model_kwargs(target)
    except LookupError:
        return None
    labels = {c["uuid"]: c["label"] for c in db.chat_model_choices()}
    return {
        "uuid": str(target),
        "label": labels.get(str(target)),
        "provider": provider,
        "model_name": model_name,
        "parameters": _redacted_parameters(args),
    }


@app.route("/chat/api/rooms/<room_uuid>/export")
def chat_room_export(room_uuid: str) -> Response:
    """The room's history as a self-contained JSON document (the Export
    sidebar's Download / Copy source).

    Query params: `limit` keeps only the last N messages (absent = all);
    `metadata` is `full` (room + model info, uuids, dates, sender names) or
    `minimal` (senders collapsed to user/assistant roles, text only — rows
    whose kind isn't a real message keep a `kind` tag so notices and thinking
    dumps aren't mistaken for replies)."""
    ruuid = _parse_uuid(room_uuid)
    room = db.get_chatroom(ruuid)
    if room is None:
        abort(404, "room not found")
    metadata = request.args.get("metadata", "full")
    if metadata not in ("full", "minimal"):
        abort(400, "metadata must be 'full' or 'minimal'")
    raw_limit = request.args.get("limit")
    limit = None
    if raw_limit is not None:
        try:
            limit = int(raw_limit)
        except ValueError:
            abort(400, "limit must be a positive integer")
        if limit <= 0:
            abort(400, "limit must be a positive integer")
    rows = db.list_room_messages(ruuid)
    total = len(rows)
    if limit is not None:
        rows = rows[-limit:]

    if metadata == "minimal":
        messages = []
        for r in rows:
            m = {
                "role": "user" if r["sender_type"] == "human" else "assistant",
                "text": r["text"],
            }
            if r["kind"] != "message":
                m["kind"] = r["kind"]
            messages.append(m)
        return jsonify({"messages": messages})

    room_info = {
        "uuid": str(ruuid),
        "name": room.name,
        "room_type": room.room_type,
    }
    if room.room_type == "direct":
        room_info["system_prompt"] = db.resolve_room_system_prompt(room) or None
        room_info["request_timeout"] = room.request_timeout
    return jsonify({
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "room": room_info,
        "model": _export_model_info(room),
        "message_count": len(rows),
        "total_message_count": total,
        "messages": [
            {
                "uuid": r["uuid"],
                "sender_uuid": r["sender_uuid"],
                "sender_name": r["sender_name"],
                "sender_type": r["sender_type"],
                "kind": r["kind"],
                "content_type": r["content_type"],
                "timestamp": r["timestamp"],
                "text": r["text"],
            }
            for r in rows
        ],
    })


@app.route("/chat/api/rooms/<room_uuid>/settings", methods=["GET", "PUT"])
def chat_room_settings(room_uuid: str) -> Response | tuple[Response, int]:
    """A direct room's settings: its system prompt (free text OR a link to a
    stored /prompt version) and which model it talks to. PUT applies
    mid-conversation — the next turn reads the room fresh."""
    ruuid = _parse_uuid(room_uuid)
    room = db.get_chatroom(ruuid)
    if room is None:
        abort(404, "room not found")
    if request.method == "PUT":
        if room.room_type != "direct":
            abort(400, "settings apply to direct rooms only")
        data = request.get_json(silent=True) or {}
        kwargs = {}
        if "system_prompt" in data:
            prompt = data.get("system_prompt")
            if not isinstance(prompt, str):
                abort(400, "system_prompt must be a string")
            kwargs["system_prompt"] = prompt
        if "model_uuid" in data:
            raw = data.get("model_uuid")
            if raw is None:
                kwargs["model_uuid"] = None
            else:
                muuid = _parse_uuid(raw)
                try:
                    db.resolved_model_kwargs(muuid)
                except LookupError:
                    abort(400, "model_uuid names no model config or override")
                kwargs["model_uuid"] = muuid
        if "prompt_uuid" in data:
            raw = data.get("prompt_uuid")
            if raw is None:
                kwargs["prompt_uuid"] = None
            else:
                puuid = _parse_uuid(raw)
                if db.prompt_get(puuid) is None:
                    abort(400, "prompt_uuid names no stored prompt")
                kwargs["prompt_uuid"] = puuid
        if "request_timeout" in data:
            raw = data.get("request_timeout")
            if raw is None:
                kwargs["request_timeout"] = None
            else:
                if not isinstance(raw, int) or isinstance(raw, bool) or raw <= 0:
                    abort(400, "request_timeout must be a positive integer "
                               "(seconds) or null")
                kwargs["request_timeout"] = raw
        room = db.set_chatroom_settings(ruuid, **kwargs)
    # Resolve the linked prompt's name so the sidebar can label the link
    # without a second request ("prompt_exists": false = the linked version
    # was deleted; the room sends no system message until relinked).
    linked = db.prompt_get(room.prompt_uuid) if room.prompt_uuid else None
    default_model = db.get_setting("chat.default_model")
    return jsonify({
        "room_type": room.room_type,
        "system_prompt": room.system_prompt or "",
        "model_uuid": str(room.model_uuid) if room.model_uuid else None,
        # What the room falls back to while model_uuid is null (the global
        # chat.default_model setting), so the sidebar can label that state.
        "default_model_uuid": str(default_model) if default_model else None,
        "request_timeout": room.request_timeout,
        "prompt_uuid": str(room.prompt_uuid) if room.prompt_uuid else None,
        "prompt_name": linked["name"] if linked else None,
        "prompt_exists": (linked is not None) if room.prompt_uuid else None,
    })


@app.route("/chat/api/models")
def chat_models() -> Response:
    """Models selectable in a direct room's Settings sidebar: every model
    config and every override, flattened to {uuid, label, available},
    available ones first."""
    return jsonify(db.chat_model_choices())


@app.route("/chat/api/assistant/runs/<uuid:run_uuid>")
def assistant_run_steps(run_uuid: UUID) -> Response:
    """An assistant run plus its step trace, addressed by the run's uuid."""
    run = db.get_assistant_run(run_uuid)
    if run is None:
        abort(404, "assistant run not found")
    steps = db.list_assistant_steps(run_uuid)
    return jsonify(
        {
            "run": {
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


@app.route("/chat/api/assistant/runs/<uuid:run_uuid>/stop", methods=["POST"])
def stop_assistant_run(run_uuid: UUID) -> Response:
    """Request a clean stop of an in-flight run. Inserts a stop control the loop
    honours at its next step boundary and flags the run `stopping`."""
    if db.get_assistant_run(run_uuid) is None:
        abort(404, "assistant run not found")
    human = db.get_human_user()
    db.create_assistant_control(
        run_uuid=run_uuid, command="stop",
        requested_by_uuid=human.uuid if human else None,
    )
    db.request_run_stop(run_uuid)
    return jsonify({"ok": True, "status": "stopping"})


@app.route("/chat/api/assistant/runs/<uuid:run_uuid>/redirect", methods=["POST"])
def redirect_assistant_run(run_uuid: UUID) -> Response | tuple[Response, int]:
    """Steer an in-flight run: the loop folds the instruction into the next step."""
    if db.get_assistant_run(run_uuid) is None:
        abort(404, "assistant run not found")
    data = request.get_json(silent=True) or {}
    instruction = (data.get("instruction") or "").strip()
    if not instruction:
        abort(400, "instruction required")
    human = db.get_human_user()
    db.create_assistant_control(
        run_uuid=run_uuid, command="redirect", payload={"instruction": instruction},
        requested_by_uuid=human.uuid if human else None,
    )
    return jsonify({"ok": True})


@app.route("/chat/api/assistant/runs/<uuid:run_uuid>/resummarize", methods=["POST"])
def resummarize_assistant_run(run_uuid: UUID) -> Response:
    """Re-run the summarizer for a completed run — same enqueue the assistant does
    at a terminal state — so the operator can regenerate a stale or missing digest."""
    if db.get_assistant_run(run_uuid) is None:
        abort(404, "assistant run not found")
    db.enqueue(ASSISTANT_RUN_SUMMARIZER_UUID, {"run_uuid": str(run_uuid)})
    return jsonify({"ok": True, "text": "Summary refresh queued."})


@app.route("/chat/api/assistant/write-intents/<uuid:intent_uuid>/confirm", methods=["POST"])
def confirm_assistant_write_intent(intent_uuid: UUID) -> Response:
    """Approve and execute a confirm-tier write the assistant proposed. Execution
    is bound to the proposed payload; the assistant cannot run it itself."""
    from agents.assistant_writes import execute_write_intent

    human = db.get_human_user()
    obs = execute_write_intent(
        intent_uuid, confirmed_by_uuid=human.uuid if human else None
    )
    return jsonify({"ok": obs.ok, "text": obs.text, "data": obs.data})


@app.route("/chat/api/assistant/write-intents/<uuid:intent_uuid>/reject", methods=["POST"])
def reject_assistant_write_intent(intent_uuid: UUID) -> Response:
    """Decline a proposed confirm-tier write."""
    from agents.assistant_writes import reject_write_intent

    # `ok` so the UI flags failure: reject only succeeds from `proposed`, so a
    # stale or double-clicked reject returns ok:false instead of a false "Done."
    rejected = reject_write_intent(intent_uuid)
    return jsonify({
        "ok": rejected,
        "text": "Write rejected." if rejected
        else "Write was not in a rejectable (proposed) state.",
    })


@app.route("/chat/api/assistant/write-intents/<uuid:intent_uuid>/undo", methods=["POST"])
def undo_assistant_write_intent(intent_uuid: UUID) -> Response:
    """Revert a completed log-and-undo write (e.g. a kanban move)."""
    from agents.assistant_writes import undo_write_intent

    obs = undo_write_intent(intent_uuid)
    return jsonify({"ok": obs.ok, "text": obs.text, "data": obs.data})


@app.route("/chat/api/messages/<message_uuid>/feedback", methods=["POST"])
def post_feedback(message_uuid: str) -> Response | tuple[Response, int]:
    """Capture an upvote/downvote on an agent's user-facing chat reply.

    Validations:
    - message must exist (404 otherwise)
    - message must not be in a direct room (400)
    - message kind must be "message" (400)
    - sender must be an agent (400)
    - rating must be "upvote" or "downvote" (400)
    """
    msg_uuid = _parse_uuid(message_uuid)
    msg = db.db.session.query(db.ChatMessage).filter_by(uuid=msg_uuid).first()
    if msg is None:
        abort(404, "message not found")
    room = db.get_chatroom(msg.room_uuid)
    # Feedback rates the responder agents; a direct room has none (the
    # operator steers by editing/deleting messages instead). The UI hides the
    # buttons there — reject hand-crafted requests too.
    if room is not None and room.room_type == "direct":
        abort(400, "feedback is not available in direct rooms")
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
