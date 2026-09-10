"""JSON endpoints between the launcher and the core (see
docs/superpowers/specs/2026-09-10-launcher-design.md):

- `GET  /services/api/desired`         what should run (launcher polls it)
- `POST /services/api/status`          what is running (launcher reports it)
- `GET  /services/api/status`          the /settings page's view of the above
- `POST /services/api/restart/<key>`   rewrite a restart nonce (Restart button)

The first two refuse an unmanaged core (no launcher markers) with 409, so a
core started by hand or by tools.serve_ui can never be mistaken for a
supervisor instance.
"""
from flask import Response, jsonify, request

import db
from services import registry

from .core import app


@app.route("/services/api/desired")
def services_desired() -> Response | tuple[Response, int]:
    if registry.instance_markers() is None:
        return jsonify({"error": "unmanaged core: not started by the launcher"}), 409
    return jsonify(registry.desired_snapshot())


@app.route("/services/api/status", methods=["GET", "POST"])
def services_status() -> Response | tuple[Response, int]:
    if request.method == "GET":
        return jsonify(registry.STATUS.view())
    try:
        registry.STATUS.accept(request.get_json(silent=True))
    except PermissionError as exc:
        return jsonify({"error": str(exc)}), 409
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True})


@app.route("/services/api/restart/<service_key>", methods=["POST"])
def services_restart(service_key: str) -> Response | tuple[Response, int]:
    try:
        nonce = registry.bump_restart_nonce(service_key)
    except KeyError:
        return jsonify({"error": f"unknown service: {service_key}"}), 404
    db.session.commit()
    return jsonify({"ok": True, "key": service_key, "restart_nonce": nonce})
