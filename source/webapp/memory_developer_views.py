"""The /memory/developer page: retrieval inspection.

Type a query and see what the assistant's `memory_query` action
(`agents.assistant._action_query_memory`) returns for it: LLM-filtered seed
answers (degrading to the MIN_SCORE-gated retrieval when the assistant has no
model group or the filter LLM fails) + hybrid claim retrieval, rendered as the
exact observation text the assistant model would receive.

It runs read-only: no chat messages are posted and no RetrievalEvents are
recorded. There is no chatroom context unless one is selected, so room-scoped
claims are excluded and dynamic seed handlers that need a room degrade to an
error string.

HTML shell + CSS live here; logic lives in static/memory_developer.js. The API
is `POST /memory/api/developer/query`. Tuning parameters (top-K, limits,
weights) are meant to grow onto this endpoint over time.
"""

import logging
import time
from pathlib import Path
from typing import Any

from flask import Response, jsonify, render_template_string, request

from .core import app

logger = logging.getLogger(__name__)

_MEMORY_DEVELOPER_JS = (
    Path(__file__).resolve().parent.parent / "static" / "memory_developer.js"
)

# Static answers can be long; the candidate table only needs a scent.
_ANSWER_PREVIEW_CHARS: int = 300


def _memory_developer_js_version() -> int:
    """mtime of memory_developer.js as a cache-buster (same trick as /memory)."""
    try:
        return int(_MEMORY_DEVELOPER_JS.stat().st_mtime)
    except OSError:
        return 0


def _preview(text: str) -> str:
    if len(text) > _ANSWER_PREVIEW_CHARS:
        return text[:_ANSWER_PREVIEW_CHARS] + "…"
    return text


def _member_row(member_uuid) -> dict[str, Any]:
    """Display info for one model-group member, via the same resolver the
    model-group UI uses (db.resolve_member): provider, model, the parent
    config's friendly label, and for overrides the effective display name —
    the user-set name or the synthesized "t0.5 c32k struct" summary."""
    from db.model_config import resolve_member

    try:
        return resolve_member(member_uuid)
    except Exception as e:
        return {"uuid": str(member_uuid), "error": f"{type(e).__name__}: {e}"}


def _group_info(group_uuid, label: str | None) -> dict[str, Any]:
    import db

    if group_uuid is None:
        return {"bound": False}
    group = db.get_model_group(group_uuid)
    return {
        "bound": True,
        "from": label,
        "uuid": str(group_uuid),
        "name": group.name if group is not None else str(group_uuid),
        "members": [
            _member_row(m) for m in db.get_model_group_member_uuids(group_uuid)
        ],
    }


def _models_overview() -> dict[str, Any]:
    """What models the pipeline's stages run on: the embedding models (seed
    questions vs claims, which are separate models and can disagree) and the
    relevance scorer the assistant resolves through assistant.memory_filter.
    Members list provider/model plus override name and overridden argument
    keys."""
    from agents.config import ASSISTANT_MEMORY_FILTER_UUID
    from agents.model_groups import resolve_assistant_model_group
    from memory.embeddings import EMBED_MODEL_NAME as CLAIMS_EMBED
    from memory.seed_memory import EMBED_MODEL_NAME as SEED_EMBED, OLLAMA_BASE

    return {
        "embedding_seed": {"model": SEED_EMBED, "base": OLLAMA_BASE},
        "embedding_claims": {"model": CLAIMS_EMBED},
        "filter_assistant_panel": _group_info(
            *resolve_assistant_model_group(ASSISTANT_MEMORY_FILTER_UUID)),
    }


def _run_assistant_memory_query(query: str, top_k_vector: int,
                                top_k_fulltext: int,
                                room_uuid, all_rooms: bool = False) -> dict[str, Any]:
    """The assistant's memory_query action, exactly as a run would execute it —
    but with telemetry off. `room_uuid` (None = no room) sets the probe's
    chatroom context: with a room selected, that room's room-scoped claims
    become reachable and room-dependent handlers resolve. `all_rooms=True` is
    the operator-inspection view: room-scoped claims from EVERY room become
    candidates (a reach no live turn has)."""
    from agents.assistant import AssistantActionContext, _action_query_memory
    from agents.config import ASSISTANT_UUID

    started = time.monotonic()
    out: dict[str, Any] = {"ok": False, "text": "", "data": {}, "error": None}
    try:
        ctx = AssistantActionContext(
            journal_id=None,
            room_uuid=room_uuid,  # type: ignore[arg-type]  — None = no room
            agent_uuid=ASSISTANT_UUID,
            step_index=0,
        )
        obs = _action_query_memory(ctx, {"query": query}, record_telemetry=False,
                                   top_k_vector=top_k_vector,
                                   top_k_fulltext=top_k_fulltext,
                                   any_room=all_rooms)
        out.update(ok=obs.ok, text=obs.text, data=obs.data or {})
    except Exception as e:
        logger.warning("memory developer: assistant memory_query failed", exc_info=True)
        out["error"] = f"{type(e).__name__}: {e}"
    out["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    return out


# Bounds for the page's per-signal candidate budgets. Defaults mirror the
# live pipelines' TOP_K_VECTOR/TOP_K_FULLTEXT; 0 disables a signal; the
# ceiling keeps a fat-fingered value from turning one probe into a huge
# filter prompt.
TOP_K_MIN: int = 0
TOP_K_MAX: int = 20


def _parse_budget(body: dict[str, Any], key: str, default: int) -> int:
    raw = body.get(key)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(TOP_K_MIN, min(TOP_K_MAX, value))


def _parse_signal_budgets(body: dict[str, Any]) -> tuple[int, int]:
    from memory.seed_memory import TOP_K_FULLTEXT, TOP_K_VECTOR

    return (_parse_budget(body, "top_k_vector", TOP_K_VECTOR),
            _parse_budget(body, "top_k_fulltext", TOP_K_FULLTEXT))


def _parse_room_uuid(body: dict[str, Any]) -> tuple[Any, bool]:
    """The probe's chatroom context as `(room_uuid, all_rooms)`.

    `"*"` → (None, True): the operator-inspection "(all rooms)" view —
    room-scoped claims from every room become candidates. `""`/absent →
    (None, False): "no room", mirroring what any fresh room recalls (global +
    agent-scoped only). A uuid → (uuid, False): probe as that room. Garbage
    parses as no room rather than failing the whole probe."""
    from uuid import UUID

    raw = str(body.get("room_uuid") or "").strip()
    if raw == "*":
        return None, True
    if not raw:
        return None, False
    try:
        return UUID(raw), False
    except ValueError:
        return None, False


@app.route("/memory/api/developer/query", methods=["POST"])
def memory_developer_query() -> Response | tuple[Response, int]:
    body = request.get_json(silent=True) or {}
    query = str(body.get("query", "")).strip()
    if not query:
        return jsonify({"error": "query is required"}), 400
    top_k_vector, top_k_fulltext = _parse_signal_budgets(body)
    room_uuid, all_rooms = _parse_room_uuid(body)
    try:
        models = _models_overview()
    except Exception as e:
        logger.warning("memory developer: models overview failed", exc_info=True)
        models = {"error": f"{type(e).__name__}: {e}"}
    return jsonify({
        "query": query,
        "top_k_vector": top_k_vector,
        "top_k_fulltext": top_k_fulltext,
        "room_uuid": str(room_uuid) if room_uuid else None,
        "all_rooms": all_rooms,
        "models": models,
        "assistant": _run_assistant_memory_query(
            query, top_k_vector, top_k_fulltext, room_uuid, all_rooms),
    })


MEMORY_DEVELOPER_TEMPLATE = """
<!doctype html>
<title>Memory developer &mdash; rainbox</title>
<style>
  body{font-family:system-ui,sans-serif;margin:0;padding:0}
  .muted{color:#6b7280;font-size:0.85rem}
  code{font-family:ui-monospace,monospace;background:#eef;padding:1px 6px;border-radius:3px}
  .err{color:#991b1b;font-weight:600}
  .memdev-content{padding:0 24px 2em;max-width:1600px}
  .memdev-queryrow{display:flex;gap:10px;margin:0 0 1em}
  .memdev-queryrow input{flex:1 1 auto;font:inherit;font-size:1rem;padding:8px 12px;
    border:1px solid #d1d5db;border-radius:8px}
  .memdev-queryrow button{padding:8px 20px;border:none;border-radius:8px;background:#2563eb;
    color:#fff;cursor:pointer;font-size:0.95rem}
  .memdev-queryrow button:hover{background:#1d4ed8}
  .memdev-queryrow button:disabled{background:#93c5fd;cursor:default}
  .memdev-topk{display:flex;align-items:center;gap:6px;white-space:nowrap}
  .memdev-topk input{width:4.5em;font:inherit;font-size:1rem;padding:8px 6px;
    border:1px solid #d1d5db;border-radius:8px}
  .memdev-topk select{max-width:14em;font:inherit;font-size:0.95rem;padding:8px 6px;
    border:1px solid #d1d5db;border-radius:8px}
  .memdev-cols{display:grid;grid-template-columns:1fr 1fr;gap:16px;align-items:start}
  .memdev-models{margin:0 0 14px}
  .memdev-models[hidden]{display:none}
  .memdev-models summary{cursor:pointer;-webkit-user-select:none;user-select:none}
  .memdev-models .memdev-table{margin-top:8px}
  @media (max-width:1000px){.memdev-cols{grid-template-columns:1fr}}
  .memdev-panel{border:1px solid #e5e7eb;border-radius:10px;background:#fbfbfb;
    padding:14px 16px;min-width:0;overflow-x:auto}
  .memdev-panel h2{margin:0 0 2px;font-size:1.05rem}
  .memdev-panel .sub{margin:0 0 10px}
  .memdev-meta{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 10px}
  .memdev-badge{font-size:0.75rem;font-weight:700;padding:2px 8px;border-radius:999px;
    background:#e5e7eb;color:#374151}
  .memdev-badge.good{background:#dcfce7;color:#166534}
  .memdev-badge.bad{background:#fee2e2;color:#991b1b}
  .memdev-badge.warn{background:#fef9c3;color:#854d0e}
  .memdev-section{margin:0 0 12px}
  .memdev-section-label{font-weight:700;font-size:0.78rem;text-transform:uppercase;
    letter-spacing:0.03em;color:#6b7280;margin-bottom:4px}
  pre.memdev-pre{margin:0;padding:10px 12px;background:#fff;border:1px solid #e5e7eb;
    border-radius:8px;font-size:0.82rem;white-space:pre-wrap;overflow-wrap:anywhere;
    max-height:480px;overflow:auto}
  table.memdev-table{border-collapse:collapse;width:100%;background:#fff;font-size:0.85rem}
  .memdev-table th,.memdev-table td{border:1px solid #e5e7eb;padding:5px 8px;
    text-align:left;vertical-align:top}
  .memdev-table th{background:#f3f4f6;font-size:0.78rem;text-transform:uppercase;
    letter-spacing:0.03em;color:#6b7280}
  .memdev-table tr.kept td{background:#f0fdf4}
  .memdev-table td.num{text-align:right;font-variant-numeric:tabular-nums}
  .memdev-empty{color:#6b7280;font-style:italic;font-size:0.88rem}
</style>
{% include "_nav.html" %}
<div class="memdev-content">
  <p class="muted">Run one query through both retrieval pipelines and compare what
  each returns. Read-only: nothing is posted, no telemetry is recorded, and
  memory commands are detected but never executed. The room selector sets the
  probe's reach: "(all rooms)" is the operator view — room-scoped claims from
  every room are candidates (no live turn has this reach); a specific room
  mirrors recall inside that chatroom; "(no room)" mirrors what any fresh room
  recalls (global + agent-scoped only).</p>
  <div class="memdev-queryrow">
    <input type="text" id="memdev-query" placeholder="type a query, e.g. &quot;what is the git status&quot;"
           autocomplete="off" autofocus>
    <label class="memdev-topk muted" for="memdev-topk-vector">vector
      <input type="number" id="memdev-topk-vector" min="0" max="20" value="5"></label>
    <label class="memdev-topk muted" for="memdev-topk-fulltext">fulltext
      <input type="number" id="memdev-topk-fulltext" min="0" max="20" value="5"></label>
    <label class="memdev-topk muted" for="memdev-room">room
      <select id="memdev-room">
        <option value="*" selected>(all rooms)</option>
        <option value="">(no room)</option>
      </select></label>
    <button id="memdev-run" onclick="memdevRun()">Run</button>
  </div>
  <details class="memdev-panel memdev-models" id="memdev-models" open hidden>
    <summary class="memdev-section-label">models in play (per pipeline stage)</summary>
    <div id="memdev-models-out"></div>
  </details>
  <div class="memdev-cols">
    <section class="memdev-panel" id="memdev-assistant">
      <h2>assistant &middot; memory_query</h2>
      <p class="muted sub">LLM-filtered seed answers (gated fallback) + hybrid claim
      retrieval, as the observation text the assistant model receives</p>
      <div id="memdev-assistant-out"><p class="memdev-empty">No query run yet.</p></div>
    </section>
  </div>
</div>
<script src="/static/memory_developer.js?v={{ memory_developer_js_v }}"></script>
"""


@app.route("/memory/developer")
def memory_developer_page() -> str:
    return render_template_string(
        MEMORY_DEVELOPER_TEMPLATE,
        memory_developer_js_v=_memory_developer_js_version(),
    )
