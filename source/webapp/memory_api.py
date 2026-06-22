"""JSON API backing the /memory review page.

Unlike /cron (a draggable folder tree saved as one version-guarded whole-tree
PUT), memories have no folders and no ordering — they are grouped by intrinsic
facets (status/scope/kind) computed read-only. So this API is per-claim:
`GET /claims` lists everything with derived fields, `GET /claims/<uuid>` returns
the detail pane's data, and a handful of `POST /claims/<uuid>/<action>` run the
provenance-safe lifecycle actions. Each mutating POST carries the
`expected_updated_at` it last read; the server refuses a stale write with 409
(the /cron version guard, at single-row granularity).

Secret claims are masked in the list (`GET /claims`) and revealed only by the
detail endpoint, so the list view is shoulder-surf-safe.
"""

from datetime import datetime
from uuid import UUID

from flask import Response, jsonify, request

import db
from memory.embeddings import EMBED_MODEL_NAME, _text_hash, embedding_text

from .core import app

_SECRET_MASK = "•••••• (secret)"


def _embedding_state(claim: db.MemoryClaim) -> str:
    """`fresh` (a current embedding exists), `stale` (an embedding exists but for
    older text), or `absent` (none) — derived, not stored. Non-active claims are
    pruned, so they read `absent`."""
    row = db.get_memory_embedding(claim.uuid, EMBED_MODEL_NAME)
    if row is None:
        return "absent"
    return "fresh" if row.text_hash == _text_hash(embedding_text(claim)) else "stale"


def _used_recently_ids() -> set[str]:
    """target_ids of memory claims that have any `used` retrieval event — one
    query, so the list endpoint stays O(1) per claim."""
    rows = (
        db.db.session.query(db.RetrievalEvent.target_id)
        .filter(db.RetrievalEvent.target_type == "memory_claim",
                db.RetrievalEvent.stage == "used")
        .distinct()
        .all()
    )
    return {r[0] for r in rows}


def _room_name(room_uuid) -> str | None:
    if room_uuid is None:
        return None
    room = db.get_chatroom(room_uuid)
    return room.name if room is not None else None


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _claim_row(claim: db.MemoryClaim, used_ids: set[str]) -> dict:
    """List-row shape: derived fields + secret-masked text."""
    secret = claim.sensitivity == "secret"
    return {
        "uuid": str(claim.uuid),
        "text": _SECRET_MASK if secret else claim.text,
        "secret": secret,
        "status": claim.status,
        "scope": claim.scope,
        "kind": claim.kind,
        "sensitivity": claim.sensitivity,
        "confidence": claim.confidence,
        "room_uuid": str(claim.room_uuid) if claim.room_uuid else None,
        "room_name": _room_name(claim.room_uuid),
        "created_at": _iso(claim.created_at),
        "updated_at": _iso(claim.updated_at),
        "expires_at": _iso(claim.expires_at),
        "stale": db.claim_stale(claim),
        "evidence_count": db.db.session.query(db.MemoryEvidence)
        .filter_by(memory_uuid=claim.uuid).count(),
        "embedding_state": _embedding_state(claim),
        "supersedes_uuid": str(claim.supersedes_uuid) if claim.supersedes_uuid else None,
        "used_recently": str(claim.uuid) in used_ids,
    }


def _lineage_short(claim: db.MemoryClaim | None) -> dict | None:
    if claim is None:
        return None
    return {"uuid": str(claim.uuid), "text": claim.text, "status": claim.status}


@app.route("/memory/api/claims")
def memory_list_claims() -> Response:
    used = _used_recently_ids()
    claims = db.list_memory_claims()
    return jsonify({"claims": [_claim_row(c, used) for c in claims]})


@app.route("/memory/api/claims/<claim_uuid>")
def memory_claim_detail(claim_uuid: str) -> tuple[Response, int] | Response:
    try:
        cu = UUID(claim_uuid)
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "bad uuid"}), 400
    detail = db.memory_claim_detail(cu)
    if detail is None:
        return jsonify({"ok": False, "error": "memory claim not found"}), 404
    claim = detail["claim"]
    superseded_by = db.db.session.query(db.MemoryClaim).filter_by(
        supersedes_uuid=cu).first()
    return jsonify({
        "uuid": str(claim.uuid),
        "text": claim.text,                       # detail reveals secret text
        "secret": claim.sensitivity == "secret",
        "status": claim.status,
        "scope": claim.scope,
        "kind": claim.kind,
        "sensitivity": claim.sensitivity,
        "confidence": claim.confidence,
        "subject": claim.subject,
        "predicate": claim.predicate,
        "object": claim.object,
        "room_uuid": str(claim.room_uuid) if claim.room_uuid else None,
        "room_name": _room_name(claim.room_uuid),
        "created_at": _iso(claim.created_at),
        "updated_at": _iso(claim.updated_at),
        "expires_at": _iso(claim.expires_at),
        "stale": db.claim_stale(claim),
        "embedding_state": _embedding_state(claim),
        "supersedes": _lineage_short(detail["supersedes"]),
        "superseded_by": _lineage_short(superseded_by),
        "evidence": [
            {"provenance": e.provenance, "source_type": e.source_type,
             "source_id": e.source_id, "excerpt": e.excerpt,
             "created_at": _iso(e.created_at)}
            for e in detail["evidence"]
        ],
        "retrieval": [
            {"stage": r.stage, "source": r.source, "query": r.query,
             "created_at": _iso(r.created_at)}
            for r in db.db.session.query(db.RetrievalEvent)
            .filter(db.RetrievalEvent.target_type == "memory_claim",
                    db.RetrievalEvent.target_id == str(cu))
            .order_by(db.RetrievalEvent.id.desc()).limit(10).all()
        ],
    })


def _parse_expected(data: dict) -> datetime | None:
    raw = data.get("expected_updated_at")
    if raw in (None, ""):
        return None
    return datetime.fromisoformat(raw)


def _refresh_embedding(claim: db.MemoryClaim) -> None:
    from memory.embeddings import refresh_claim_embedding
    refresh_claim_embedding(claim)  # embed when active, prune otherwise


@app.route("/memory/api/claims/<claim_uuid>/<action>", methods=["POST"])
def memory_claim_action(claim_uuid: str, action: str) -> tuple[Response, int] | Response:
    """Run one provenance-safe lifecycle action. StaleWriteError -> 409, bad
    input/illegal transition -> 400, missing claim -> 404."""
    try:
        cu = UUID(claim_uuid)
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "bad uuid"}), 400
    if action not in ("activate", "reject", "reactivate", "correct",
                      "sensitivity", "expiry"):
        return jsonify({"ok": False, "error": f"unknown action {action!r}"}), 404
    data = request.get_json(silent=True) or {}

    claim = db.get_memory_claim(cu)
    if claim is None:
        return jsonify({"ok": False, "error": "memory claim not found"}), 404

    try:
        expected = _parse_expected(data)
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "bad expected_updated_at"}), 400

    try:
        result = _dispatch_action(claim, action, data, expected)
    except db.StaleWriteError as exc:
        return jsonify({"ok": False, "error": str(exc), "stale": True}), 409
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, **result})


def _dispatch_action(claim, action, data, expected) -> dict:
    """The mutation behind each action. Embeddings are refreshed on status
    transitions (embed when active, prune otherwise), mirroring the assistant
    write path."""
    cu = claim.uuid
    if action == "activate":
        if claim.status not in ("candidate", "expired"):
            raise ValueError(f"cannot activate a {claim.status} claim")
        db.assert_claim_unchanged(claim, expected)
        updated = db.activate_memory_claim(cu)
        _refresh_embedding(updated)
        return {}
    if action == "reject":
        db.assert_claim_unchanged(claim, expected)
        db.reject_memory(cu, {"provenance": "confirmed_by_user", "source_type": "manual"})
        _refresh_embedding(db.get_memory_claim(cu))
        return {}
    if action == "reactivate":
        updated = db.reactivate_memory_claim(cu, expected_updated_at=expected)
        _refresh_embedding(updated)
        return {}
    if action == "correct":
        new_text = str(data.get("new_text", "")).strip()
        if not new_text:
            raise ValueError("correct needs new_text")
        db.assert_claim_unchanged(claim, expected)
        new = db.supersede_memory(
            cu,
            {"scope": claim.scope, "kind": claim.kind, "text": new_text,
             "confidence": claim.confidence, "sensitivity": claim.sensitivity,
             "subject": claim.subject, "predicate": claim.predicate,
             "object": claim.object, "agent_uuid": claim.agent_uuid,
             "room_uuid": claim.room_uuid},
            {"provenance": "confirmed_by_user", "source_type": "manual"})
        _refresh_embedding(new)
        _refresh_embedding(db.get_memory_claim(cu))  # old is now superseded -> prune
        return {"new_uuid": str(new.uuid)}
    if action == "sensitivity":
        db.set_memory_sensitivity(cu, str(data.get("sensitivity", "")),
                                  expected_updated_at=expected)
        return {}
    if action == "expiry":
        raw = data.get("expires_at")
        when = datetime.fromisoformat(raw) if raw not in (None, "") else None
        db.set_memory_expiry(cu, when, expected_updated_at=expected)
        return {}
    raise ValueError(f"unhandled action {action!r}")  # pragma: no cover
