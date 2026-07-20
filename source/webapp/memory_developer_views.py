"""The /memory/developer page: side-by-side retrieval inspection.

Type a query and see what each of the two retrieval pipelines returns for it:

- **assistant memory_query** — the assistant's `memory_query` action
  (`agents.assistant._action_query_memory`): LLM-filtered seed answers
  (degrading to the MIN_SCORE-gated retrieval when the assistant has no model
  group or the filter LLM fails) + hybrid claim retrieval, rendered as the
  exact observation text the assistant model would receive.
- **query_filter_router** — the chat route's pipeline
  (`agents.query_filter_router`), run stage by stage: exact alias → top-K
  semantic candidates → LLM relevance filter → resolve kept candidates →
  route LLM reply. Intermediate stages are all returned, so the operator can
  see *why* a reply came out.

Both run read-only: no chat messages are posted, no RetrievalEvents are
recorded, and memory commands ("remember …") are detected but never executed.
There is no chatroom context, so room-scoped claims are excluded and dynamic
seed handlers that need a room degrade to an error string.

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
    """What models each pipeline stage runs on, so a comparison on this page is
    apples-to-apples: the embedding models (seed questions vs claims), the
    shared relevance scorer as each panel resolves it, and the router's reply
    group. Members list provider/model plus override name and overridden
    argument keys."""
    import db
    from agents.config import ASSISTANT_UUID, QUERY_FILTER_ROUTER_UUID
    from agents.query_filter_router import resolve_filter_model_group
    from memory.embeddings import EMBED_MODEL_NAME as CLAIMS_EMBED
    from memory.seed_memory import EMBED_MODEL_NAME as SEED_EMBED, OLLAMA_BASE

    router_binding = db.get_agent_model_binding(QUERY_FILTER_ROUTER_UUID)
    route_group_uuid = (router_binding.model_group_uuid
                        if router_binding is not None else None)
    filter_router = resolve_filter_model_group(
        [(QUERY_FILTER_ROUTER_UUID, "own")])
    filter_assistant = resolve_filter_model_group(
        [(QUERY_FILTER_ROUTER_UUID, "query_filter_router"),
         (ASSISTANT_UUID, "own")])
    return {
        "embedding_seed": {"model": SEED_EMBED, "base": OLLAMA_BASE},
        "embedding_claims": {"model": CLAIMS_EMBED},
        "filter_router_panel": _group_info(*filter_router),
        "filter_assistant_panel": _group_info(*filter_assistant),
        "route": _group_info(route_group_uuid, "query_filter_router"),
    }


def _run_assistant_memory_query(query: str) -> dict[str, Any]:
    """The assistant's memory_query action, exactly as a run would execute it —
    but with telemetry off and no room context (see module docstring)."""
    from agents.assistant import AssistantActionContext, _action_query_memory
    from agents.config import ASSISTANT_UUID

    started = time.monotonic()
    out: dict[str, Any] = {"ok": False, "text": "", "data": {}, "error": None}
    try:
        ctx = AssistantActionContext(
            journal_id=None,
            room_uuid=None,  # type: ignore[arg-type]  — no room on the dev page
            agent_uuid=ASSISTANT_UUID,
            step_index=0,
        )
        obs = _action_query_memory(ctx, {"query": query}, record_telemetry=False)
        out.update(ok=obs.ok, text=obs.text, data=obs.data or {})
    except Exception as e:
        logger.warning("memory developer: assistant memory_query failed", exc_info=True)
        out["error"] = f"{type(e).__name__}: {e}"
    out["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    return out


def _run_query_filter_router(query: str) -> dict[str, Any]:
    """The query_filter_router pipeline, stage by stage, read-only.

    Mirrors QueryFilterRouterAgent.handle() minus its side effects: nothing is
    posted to a chatroom, no RetrievalEvents are written, and a query that
    parses as a memory command is only *reported* (running it would mutate the
    memory store). The route stage uses a synthetic one-message transcript
    (there is no room), so its reply shows how the router answers the query
    cold — without chat history."""
    from agents.config import QUERY_FILTER_ROUTER_UUID
    from agents.query_filter_router import (
        FILTER_SYSTEM_PROMPT,
        QUERY_FILTER_ROUTER_SYSTEM_PROMPT,
        TOP_K_FILTER,
        FilterDecision,
        QueryFilterRouterAgent,
        apply_filter_scores,
        build_filter_prompt,
        resolve_filter_model_uuids,
        structured_llm_call,
    )
    from agents.query_handlers import QueryContext
    from agents.router import RouterResponse
    from chat.transcript import format_history
    from memory import seed_memory as qkb
    from memory.ops import parse_memory_command

    started = time.monotonic()
    out: dict[str, Any] = {
        "memory_command": None,
        "exact": None,
        "candidates": [],
        "filter_kept": [],
        "filter_group": None,
        "filter_error": None,
        "resolved": {},
        "route": None,
        "route_error": None,
        "error": None,
    }
    try:
        cmd = parse_memory_command(query)
        if cmd is not None:
            # The real agent dispatches this and returns without touching the
            # Q&A KB. Report it, then run the Q&A stages anyway so the operator
            # still sees what retrieval would have surfaced.
            out["memory_command"] = cmd.kind

        qkb._load_kb()
        vs = qkb._vector_store()
        qkb._ensure_populated(vs)

        qctx = QueryContext(
            room_uuid=None,  # type: ignore[arg-type]  — no room on the dev page
            query=query,
            payload={},
            agent_uuid=QUERY_FILTER_ROUTER_UUID,
        )

        exact = qkb._exact_match(query)
        if exact is not None:
            out["exact"] = {
                "qa_id": exact.qa_id,
                "score": qkb.score_permille(exact.score),
                "matched_question": exact.matched_question,
                "reply": qkb._resolve_match(exact, qctx),
            }
            # The real agent answers from the exact hit alone — no LLM stages.
            out["elapsed_ms"] = round((time.monotonic() - started) * 1000)
            return out

        candidates = qkb._semantic_ranked(query, vs)[:TOP_K_FILTER]
        for c in candidates:
            entry = qkb.get_entry(c.qa_id) or {}
            kind = str(entry.get("kind", "?"))
            row: dict[str, Any] = {
                "qa_id": c.qa_id,
                "path": str(entry.get("path", "")),
                "kind": kind,
                "score": qkb.score_permille(c.score),
                "matched_question": c.matched_question,
            }
            if kind == "static":
                row["answer_preview"] = _preview(str(entry.get("answer", "")))
            elif kind == "dynamic":
                row["handler"] = str(entry.get("handler", ""))
            out["candidates"].append(row)

        agent = QueryFilterRouterAgent(
            agent_uuid=QUERY_FILTER_ROUTER_UUID,
            name="query_filter_router",
            send=lambda _msg: None,
        )
        agent.setup()

        relevant_qa_ids: list[str] = []
        if candidates:
            try:
                filter_prompt = build_filter_prompt(query, candidates)
                # Same scorer resolution as the live pipelines: the dedicated
                # memory_filter binding when set, else the router's own group.
                scorer_uuids, scorer_src = resolve_filter_model_uuids(
                    [(QUERY_FILTER_ROUTER_UUID, "own")])
                out["filter_group"] = scorer_src
                decision, scorer_model_uuid = structured_llm_call(
                    "memory_developer.filter", scorer_uuids or [],
                    FILTER_SYSTEM_PROMPT, filter_prompt, FilterDecision,
                )
                out["filter_model"] = _member_row(scorer_model_uuid).get("model_name")
                # LLM scores → code-side keep/drop; merge each candidate's
                # scores into its table row so the page can show why.
                scored = apply_filter_scores(decision, candidates)
                relevant_qa_ids = [s.qa_id for s in scored if s.kept]
                out["filter_kept"] = relevant_qa_ids
                by_qa_id = {s.qa_id: s for s in scored}
                for row in out["candidates"]:
                    s = by_qa_id.get(row["qa_id"])
                    if s is not None:
                        row.update(direct=s.direct, indirect=s.indirect,
                                   relevancy=s.relevancy)
            except Exception as e:
                logger.warning(
                    "memory developer: filter LLM failed", exc_info=True
                )
                out["filter_error"] = f"{type(e).__name__}: {e}"

        for qa_id in relevant_qa_ids:
            cand = next((c for c in candidates if c.qa_id == qa_id), None)
            if cand is not None:
                out["resolved"][qa_id] = qkb._resolve_match(cand, qctx)

        if out["filter_error"] is None:
            try:
                transcript = format_history(
                    [{"sender_name": "operator", "text": query}]
                )
                if relevant_qa_ids:
                    lines = ["", "Relevant candidates:"]
                    for qa_id in relevant_qa_ids:
                        lines.append(f"  - qa_id: {qa_id}")
                        lines.append(f"    reply: {out['resolved'].get(qa_id, '')!r}")
                    route_prompt = transcript + "\n" + "\n".join(lines)
                else:
                    route_prompt = transcript + "\n\nRelevant candidates: (none)"
                route = agent._llm_structured(
                    QUERY_FILTER_ROUTER_SYSTEM_PROMPT, route_prompt, RouterResponse
                )
                route_model = None
                if agent._active_model_uuid is not None:
                    route_model = _member_row(agent._active_model_uuid).get("model_name")
                out["route"] = {
                    "subject": route.subject,
                    "action": route.action,
                    "reply": route.reply,
                    "model": route_model,
                }
            except Exception as e:
                logger.warning(
                    "memory developer: route LLM failed", exc_info=True
                )
                out["route_error"] = f"{type(e).__name__}: {e}"
    except Exception as e:
        logger.warning("memory developer: query_filter_router side failed",
                       exc_info=True)
        out["error"] = f"{type(e).__name__}: {e}"
    out["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    return out


@app.route("/memory/api/developer/query", methods=["POST"])
def memory_developer_query() -> Response | tuple[Response, int]:
    body = request.get_json(silent=True) or {}
    query = str(body.get("query", "")).strip()
    if not query:
        return jsonify({"error": "query is required"}), 400
    try:
        models = _models_overview()
    except Exception as e:
        logger.warning("memory developer: models overview failed", exc_info=True)
        models = {"error": f"{type(e).__name__}: {e}"}
    return jsonify({
        "query": query,
        "models": models,
        "assistant": _run_assistant_memory_query(query),
        "filter_router": _run_query_filter_router(query),
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
  .memdev-cols{display:grid;grid-template-columns:1fr 1fr;gap:16px;align-items:start}
  .memdev-models{margin:0 0 14px}
  .memdev-models[hidden]{display:none}
  .memdev-models summary{cursor:pointer;-webkit-user-select:none;user-select:none}
  .memdev-models .memdev-table{margin-top:8px}
  @media (max-width:1000px){.memdev-cols{grid-template-columns:1fr}}
  .memdev-panel{border:1px solid #e5e7eb;border-radius:10px;background:#fbfbfb;
    padding:14px 16px;min-width:0}
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
  memory commands are detected but never executed. There is no chatroom context
  here, so room-scoped claims and room-dependent handlers are out of reach.</p>
  <div class="memdev-queryrow">
    <input type="text" id="memdev-query" placeholder="type a query, e.g. &quot;what is the git status&quot;"
           autocomplete="off" autofocus>
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
    <section class="memdev-panel" id="memdev-router">
      <h2>query_filter_router</h2>
      <p class="muted sub">exact alias &rarr; top-K semantic candidates &rarr; LLM filter
      &rarr; resolve &rarr; route LLM reply</p>
      <div id="memdev-router-out"><p class="memdev-empty">No query run yet.</p></div>
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
