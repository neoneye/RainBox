"""Admin JSON endpoints to start / stop / inspect a persona conversation run.

A run is driven by the ConversationManagerAgent (agent_conversation.py): the
endpoints just create the `conversation_run` row and enqueue the first manager
tick (start), flip the stop flag and nudge the manager (stop), or read state
(get). The supervisor (main.py) must be running for turns to actually advance.
"""

from uuid import UUID

from flask import Response, abort, jsonify, request

import db
from agents.config import CONVERSATION_MANAGER_UUID
from agents.persona import (
    list_conversation_templates,
    load_conversation_template,
    personas_by_slug,
)

from .core import app


@app.route("/conversation/api/templates")
def conversation_templates() -> Response:
    return jsonify(list_conversation_templates())


@app.route("/conversation/api/runs", methods=["GET"])
def conversation_list_runs() -> Response:
    return jsonify(db.list_conversation_runs())


@app.route("/conversation/api/runs", methods=["POST"])
def conversation_start() -> tuple[Response, int]:
    data = request.get_json(silent=True) or {}
    template_slug = (data.get("template_slug") or "").strip()
    room_uuid_raw = data.get("room_uuid")
    if not template_slug or not room_uuid_raw:
        abort(400, "template_slug and room_uuid required")
    try:
        room_uuid = UUID(str(room_uuid_raw))
    except (ValueError, TypeError):
        abort(400, "invalid room_uuid")
    if db.get_chatroom(room_uuid) is None:
        abort(404, "room not found")
    try:
        template = load_conversation_template(template_slug)
    except FileNotFoundError:
        abort(404, f"no conversation template {template_slug!r}")

    by_slug = personas_by_slug()
    participants: list[dict] = []
    for i, part in enumerate(template.get("participants", [])):
        slug = part.get("persona_slug")
        persona = by_slug.get(slug)
        if persona is None:
            abort(400, f"unknown persona slug {slug!r}")
        participants.append({
            "persona_id": str(persona.persona_id),
            "slug": persona.slug,
            "agent_uuid": str(persona.agent_uuid),
            "agent_kind": persona.agent_kind,
            "turn_order": int(part.get("turn_order", i + 1)),
        })
    if len(participants) < 2:
        abort(400, "a conversation needs at least 2 participants")

    # Interruption watermark: the newest human message already in the room. A
    # human message posted after this pauses the run.
    msgs = db.list_room_messages(room_uuid)
    human_ids = [m["id"] for m in msgs if m.get("sender_type") == "human"]
    last_human = max(human_ids) if human_ids else 0

    run = db.create_conversation_run(
        room_uuid, participants, template.get("turn_policy", {}),
        last_human_message_id=last_human,
    )
    db.enqueue(CONVERSATION_MANAGER_UUID,
               {"run_uuid": str(run.id), "kind": "tick", "expected_tick_count": 0})
    return jsonify({"run_uuid": str(run.id), "status": run.status, "room_uuid": str(room_uuid)}), 201


@app.route("/conversation/api/runs/<run_uuid>/stop", methods=["POST"])
def conversation_stop(run_uuid: str) -> Response:
    try:
        ruuid = UUID(run_uuid)
    except (ValueError, TypeError):
        abort(400, "invalid run_uuid")
    tick = db.current_tick_count(ruuid)
    action = db.stop_conversation(ruuid)
    if action == "missing":
        abort(404, "run not found")
    # A running run only sets the flag; nudge the manager so it observes the stop
    # promptly. A paused run was already transitioned to 'stopped' in the helper.
    if action == "stopping":
        db.enqueue(CONVERSATION_MANAGER_UUID,
                   {"run_uuid": str(ruuid), "kind": "tick", "expected_tick_count": tick})
    return jsonify({"run_uuid": str(ruuid), "action": action})


@app.route("/conversation/api/runs/<run_uuid>/resume", methods=["POST"])
def conversation_resume(run_uuid: str) -> Response:
    try:
        ruuid = UUID(run_uuid)
    except (ValueError, TypeError):
        abort(400, "invalid run_uuid")
    if db.get_conversation_run(ruuid) is None:
        abort(404, "run not found")
    res = db.resume_conversation(ruuid)
    if res["status"] == "running":
        db.enqueue(CONVERSATION_MANAGER_UUID, {
            "run_uuid": str(ruuid), "kind": "tick",
            "expected_tick_count": res["tick_count"],
        })
    return jsonify({"run_uuid": str(ruuid), **res})


@app.route("/conversation/api/runs/<run_uuid>/reconcile", methods=["POST"])
def conversation_reconcile(run_uuid: str) -> Response:
    try:
        ruuid = UUID(run_uuid)
    except (ValueError, TypeError):
        abort(400, "invalid run_uuid")
    if db.get_conversation_run(ruuid) is None:
        abort(404, "run not found")
    res = db.reconcile_conversation(ruuid)
    # A recovered (retry) run needs a manager tick to reschedule the turn.
    if res.get("status") == "retry":
        db.enqueue(CONVERSATION_MANAGER_UUID, {
            "run_uuid": str(ruuid), "kind": "tick",
            "expected_tick_count": res["tick_count"],
        })
    return jsonify({"run_uuid": str(ruuid), **res})


@app.route("/conversation/api/runs/<run_uuid>")
def conversation_get(run_uuid: str) -> Response:
    try:
        ruuid = UUID(run_uuid)
    except (ValueError, TypeError):
        abort(400, "invalid run_uuid")
    run = db.get_conversation_run(ruuid)
    if run is None:
        abort(404, "run not found")
    return jsonify({
        "run_uuid": str(run.id),
        "status": run.status,
        "turn": run.turn,
        "room_uuid": str(run.room_uuid),
        "reason": run.reason,
        "active_turn": run.active_turn,
        "stop_requested": run.stop_requested,
    })
