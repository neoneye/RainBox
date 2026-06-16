"""JSON API backing the /kanban page and the agent-facing task operations.

Boards: list / create / delete, plus a per-board bulk load + save (the page's
debounced PUT) guarded like the cron tree PUT — the payload must echo the
`version` token it hydrated with (stale → 409, re-hydrate instead of
clobbering) and declare its deletions (`deletes`, refusing a truncated
payload). The markdown endpoint serves the canonical LLM-facing serialization
generated from DB state (spoof-resistant escaping in db.kanban).

Tasks: the narrow, uuid-addressed agent operations (claim / move / events /
complete) — these are the robust write path the board exists for
(docs/plan.md): no document editing, each call succeeds atomically or fails
loudly, and everything lands in the kanban_task_event audit trail.
"""

from uuid import UUID

from flask import Response, jsonify, request

import db

from .core import app


def _uuid_or_none(raw: str) -> UUID | None:
    try:
        return UUID(raw)
    except (ValueError, TypeError):
        return None


@app.route("/kanban/api/boards", methods=["GET", "POST"])
def kanban_boards() -> tuple[Response, int] | Response:
    if request.method == "POST":
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "request body must be a JSON object"}), 400
        folder = None
        if data.get("folderId") is not None:
            folder = _uuid_or_none(str(data.get("folderId")))
            if folder is None:
                return jsonify({"ok": False, "error": "'folderId' must be a uuid"}), 400
        try:
            board = db.kanban_create_board(data.get("name", ""),
                                           data.get("description", ""),
                                           folder_uuid=folder)
        except db.KanbanError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "board": board})
    return jsonify({"boards": db.kanban_list_boards()})


@app.route("/kanban/api/board/<board_uuid>", methods=["GET", "PUT", "DELETE"])
def kanban_board(board_uuid: str) -> tuple[Response, int] | Response:
    bu = _uuid_or_none(board_uuid)
    if bu is None:
        return jsonify({"ok": False, "error": "bad uuid"}), 400
    if request.method == "DELETE":
        if not db.kanban_delete_board(bu):
            return jsonify({"ok": False, "error": "board not found"}), 404
        return jsonify({"ok": True})
    if request.method == "PUT":
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "request body must be a JSON object"}), 400
        version = data.get("version")
        if not isinstance(version, str) or not version:
            return jsonify({"ok": False, "error":
                            "missing board 'version' (hydrate via GET first)"}), 400
        deletes = data.get("deletes", 0)
        if not isinstance(deletes, int) or isinstance(deletes, bool) or deletes < 0:
            return jsonify({"ok": False, "error":
                            "'deletes' must be a non-negative integer"}), 400
        try:
            db.kanban_save_board(bu, data, base_version=version,
                                 expected_deletes=deletes)
        except db.KanbanConflict as exc:
            return jsonify({"ok": False, "error": str(exc),
                            "version": db.kanban_board_version(bu)}), 409
        except db.KanbanError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "version": db.kanban_board_version(bu)})
    board = db.kanban_load_board(bu)
    if board is None:
        return jsonify({"ok": False, "error": "board not found"}), 404
    return jsonify(board)


@app.route("/kanban/api/board/<board_uuid>/duplicate", methods=["POST"])
def kanban_board_duplicate(board_uuid: str) -> tuple[Response, int] | Response:
    """Deep-clone a board (fresh uuids throughout, audit trail not copied);
    returns the new board's payload."""
    bu = _uuid_or_none(board_uuid)
    if bu is None:
        return jsonify({"ok": False, "error": "bad uuid"}), 400
    board = db.kanban_duplicate_board(bu)
    if board is None:
        return jsonify({"ok": False, "error": "board not found"}), 404
    return jsonify({"ok": True, "board": board})


@app.route("/kanban/api/board/<board_uuid>/markdown")
def kanban_board_markdown(board_uuid: str) -> tuple[Response, int] | Response:
    """The canonical LLM-facing serialization, generated server-side from DB
    state — agents fetch board context here without a browser.
    ?focus=in-progress: the asymmetric executing-agent view (lease state +
    recent events in middle columns, ends compressed)."""
    bu = _uuid_or_none(board_uuid)
    if bu is None:
        return jsonify({"ok": False, "error": "bad uuid"}), 400
    focus = request.args.get("focus") or None
    try:
        md = db.kanban_board_markdown(bu, focus=focus)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    if md is None:
        return jsonify({"ok": False, "error": "board not found"}), 404
    return Response(md, mimetype="text/markdown")


@app.route("/kanban/api/board/<board_uuid>/json")
def kanban_board_llm_json(board_uuid: str) -> tuple[Response, int] | Response:
    """The JSON twin of /markdown: columns→tasks nested, agent names resolved,
    no version token (a read snapshot, not a save payload). Pretty-printed —
    it is meant to be pasted into an LLM context. Accepts the same ?focus=."""
    import json as _json

    bu = _uuid_or_none(board_uuid)
    if bu is None:
        return jsonify({"ok": False, "error": "bad uuid"}), 400
    focus = request.args.get("focus") or None
    try:
        data = db.kanban_board_llm_json(bu, focus=focus)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    if data is None:
        return jsonify({"ok": False, "error": "board not found"}), 404
    return Response(_json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    mimetype="application/json")


def _task_op(task_uuid: str, fn) -> tuple[Response, int] | Response:
    tu = _uuid_or_none(task_uuid)
    if tu is None:
        return jsonify({"ok": False, "error": "bad uuid"}), 400
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "request body must be a JSON object"}), 400
    try:
        result = fn(tu, data)
    except db.KanbanConflict as exc:
        return jsonify({"ok": False, "error": str(exc)}), 409
    except db.KanbanError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if result is None:
        return jsonify({"ok": False, "error": "task not found"}), 404
    return jsonify({"ok": True, "task": result})


@app.route("/kanban/api/claim-next", methods=["POST"])
def kanban_claim_next() -> tuple[Response, int] | Response:
    """Atomically find and claim one eligible task for an agent (the DB picks:
    own tasks first, then unassigned, in runnable columns). `task` is null
    when nothing is eligible — a normal outcome, not an error. Body:
    {agentUuid, boardId?, includeUnassigned?}."""
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "request body must be a JSON object"}), 400
    agent = _uuid_or_none(str(data.get("agentUuid", "")))
    if agent is None:
        return jsonify({"ok": False, "error": "'agentUuid' must be a uuid"}), 400
    board = None
    if data.get("boardId") is not None:
        board = _uuid_or_none(str(data.get("boardId")))
        if board is None:
            return jsonify({"ok": False, "error": "'boardId' must be a uuid"}), 400
    include_unassigned = data.get("includeUnassigned", True)
    if not isinstance(include_unassigned, bool):
        return jsonify({"ok": False, "error": "'includeUnassigned' must be a boolean"}), 400
    task = db.kanban_claim_next(agent, board, include_unassigned=include_unassigned)
    return jsonify({"ok": True, "task": task})


@app.route("/kanban/api/tasks/<task_uuid>/claim", methods=["POST"])
def kanban_task_claim(task_uuid: str) -> tuple[Response, int] | Response:
    def fn(tu: UUID, data: dict):
        agent = _uuid_or_none(str(data.get("agentUuid", "")))
        if agent is None:
            raise db.KanbanError("'agentUuid' must be a uuid")
        return db.kanban_claim_task(tu, agent)
    return _task_op(task_uuid, fn)


@app.route("/kanban/api/tasks/<task_uuid>/enqueue", methods=["POST"])
def kanban_task_enqueue(task_uuid: str) -> tuple[Response, int] | Response:
    """Run this task now: enqueue its assigned agent with {task_uuid,
    board_uuid, source:"kanban"} via the supervisor machinery. 400 when
    unassigned/not runnable, 409 while a live lease holds it."""
    def fn(tu: UUID, data: dict):
        return db.kanban_enqueue_task(tu)
    return _task_op(task_uuid, fn)


@app.route("/kanban/api/tasks/<task_uuid>/release", methods=["POST"])
def kanban_task_release(task_uuid: str) -> tuple[Response, int] | Response:
    """Give a lease back so the task is immediately claimable. A live lease
    can only be released by its holder (409); an expired one by anyone."""
    def fn(tu: UUID, data: dict):
        agent = _uuid_or_none(str(data.get("agentUuid", "")))
        if agent is None:
            raise db.KanbanError("'agentUuid' must be a uuid")
        return db.kanban_release_task(tu, agent)
    return _task_op(task_uuid, fn)


@app.route("/kanban/api/tasks/<task_uuid>/renew", methods=["POST"])
def kanban_task_renew(task_uuid: str) -> tuple[Response, int] | Response:
    """Extend one's own lease (the long-running agent's heartbeat)."""
    def fn(tu: UUID, data: dict):
        agent = _uuid_or_none(str(data.get("agentUuid", "")))
        if agent is None:
            raise db.KanbanError("'agentUuid' must be a uuid")
        return db.kanban_renew_claim(tu, agent)
    return _task_op(task_uuid, fn)


@app.route("/kanban/api/tasks/<task_uuid>/move", methods=["POST"])
def kanban_task_move(task_uuid: str) -> tuple[Response, int] | Response:
    def fn(tu: UUID, data: dict):
        col = _uuid_or_none(str(data.get("columnUuid", "")))
        if col is None:
            raise db.KanbanError("'columnUuid' must be a uuid")
        return db.kanban_move_task(tu, col, actor=str(data.get("actor", "")),
                                   note=str(data.get("note", "")))
    return _task_op(task_uuid, fn)


@app.route("/kanban/api/tasks/<task_uuid>/complete", methods=["POST"])
def kanban_task_complete(task_uuid: str) -> tuple[Response, int] | Response:
    def fn(tu: UUID, data: dict):
        ok = data.get("ok")
        if not isinstance(ok, bool):
            raise db.KanbanError("'ok' must be a boolean")
        return db.kanban_complete_task(tu, ok, actor=str(data.get("actor", "")),
                                       detail=str(data.get("detail", "")))
    return _task_op(task_uuid, fn)


@app.route("/kanban/api/tasks/<task_uuid>/events", methods=["GET", "POST"])
def kanban_task_events(task_uuid: str) -> tuple[Response, int] | Response:
    tu = _uuid_or_none(task_uuid)
    if tu is None:
        return jsonify({"ok": False, "error": "bad uuid"}), 400
    if request.method == "POST":
        def fn(tu2: UUID, data: dict):
            return db.kanban_append_event(tu2, str(data.get("kind", "")),
                                          actor=str(data.get("actor", "")),
                                          detail=str(data.get("detail", "")))
        return _task_op(task_uuid, fn)
    events = db.kanban_task_events(tu)
    if events is None:
        return jsonify({"ok": False, "error": "task not found"}), 404
    return jsonify({"events": events})


# ---- folder tree (the left-panel hierarchy) ----

@app.route("/kanban/api/tree", methods=["GET", "PUT"])
def kanban_tree() -> tuple[Response, int] | Response:
    """Hydrate / placement-only save the folder tree (folders + board
    placement). The PUT echoes the version token GET returned; a stale token
    is a 409 and the page re-hydrates instead of clobbering another writer."""
    if request.method == "PUT":
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "request body must be a JSON object"}), 400
        version = data.get("version")
        if not isinstance(version, str) or not version:
            return jsonify({"ok": False, "error":
                            "missing tree 'version' (hydrate via GET first)"}), 400
        try:
            db.kanban_save_tree(data.get("folders", []), data.get("boards", []),
                                base_version=version)
        except db.KanbanConflict as exc:
            return jsonify({"ok": False, "error": str(exc),
                            "version": db.kanban_tree_version()}), 409
        except db.KanbanError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "version": db.kanban_tree_version()})
    return jsonify(db.kanban_load_tree())


@app.route("/kanban/api/folders", methods=["POST"])
def kanban_folders() -> tuple[Response, int] | Response:
    """Create a folder; returns it. Body: {name, parentId?, description?}."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "request body must be a JSON object"}), 400
    parent = None
    if data.get("parentId") is not None:
        parent = _uuid_or_none(str(data.get("parentId")))
        if parent is None:
            return jsonify({"ok": False, "error": "'parentId' must be a uuid"}), 400
    try:
        folder = db.kanban_create_folder(data.get("name", ""), parent,
                                         data.get("description", ""))
    except db.KanbanError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "folder": folder})


@app.route("/kanban/api/folders/<folder_uuid>", methods=["DELETE"])
def kanban_folder_delete(folder_uuid: str) -> tuple[Response, int] | Response:
    """Delete a folder; its child folders + boards reparent up one level
    (boards are never deleted). 404 if the folder doesn't exist."""
    fu = _uuid_or_none(folder_uuid)
    if fu is None:
        return jsonify({"ok": False, "error": "bad uuid"}), 400
    if not db.kanban_delete_folder(fu):
        return jsonify({"ok": False, "error": "folder not found"}), 404
    return jsonify({"ok": True})
