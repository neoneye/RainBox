"""JSON endpoints for the /settings page's view of the launcher (see
docs/superpowers/specs/2026-09-10-launcher-design.md):

- `GET  /services/api/status`          observed state, from the control channel
- `POST /services/api/restart/<key>`   rewrite a restart nonce (Restart button)

The launcher itself is not an HTTP client: desired state and status travel
over the inherited control socket (`services.registry.CHANNEL`), so there is
no polling endpoint and nothing here to refuse.
"""
from flask import Response, jsonify

import db
from services import registry

from .core import app


@app.route("/services/api/status")
def services_status() -> Response:
    return jsonify(registry.CHANNEL.view())


@app.route("/services/api/restart/<service_key>", methods=["POST"])
def services_restart(service_key: str) -> Response | tuple[Response, int]:
    try:
        nonce = registry.bump_restart_nonce(service_key)
    except KeyError:
        return jsonify({"error": f"unknown service: {service_key}"}), 404
    db.session.commit()
    return jsonify({"ok": True, "key": service_key, "restart_nonce": nonce})
