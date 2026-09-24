"""The `diary_query` action: validate flat args, run one read, render it,
issue a cursor for what did not fit, record telemetry.

Returns a `DiaryObservation`; the assistant's capability wraps it in an
AssistantObservation (with the compact stand-in). Contract: proposal §7–§8.
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable
from uuid import UUID

import db
from diary.render import item_json, observation_budget, render_items, with_cursor
from diary.retrieval import (
    DiaryContext,
    DiaryRequest,
    DiaryResult,
    create_cursor,
    current_manifest,
    load_cursor,
    retrieve_diary,
)

MODES = ("search", "literal", "timeline", "read", "continue")
ALLOWED_KEYS = {"mode", "query", "date_from", "date_to", "source", "citation", "cursor"}
MODE_KEYS = {
    "search": {"query"}, "literal": {"query"}, "timeline": {"date_from", "date_to"},
    "read": {"citation"}, "continue": {"cursor"},
}
OPTIONAL_KEYS = {
    "search": {"date_from", "date_to", "source"},
    "literal": {"date_from", "date_to", "source"},
    "timeline": {"source"}, "read": set(), "continue": set(),
}
MAX_QUERY_CHARS = 512
MAX_TIMELINE_DAYS = 366


class DiaryRequestError(ValueError):
    pass


@dataclass
class DiaryObservation:
    ok: bool
    text: str
    compact: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


def validate_request(args: Any) -> DiaryRequest:
    """Strict: unknown keys, missing or conflicting fields, bad dates and
    oversized input are rejected before any retrieval."""
    if not isinstance(args, dict):
        raise DiaryRequestError("args must be an object")
    unknown = set(args) - ALLOWED_KEYS
    if unknown:
        raise DiaryRequestError(f"unknown keys {sorted(unknown)}")
    mode = args.get("mode")
    if mode not in MODES:
        raise DiaryRequestError(f"mode must be one of {list(MODES)}")
    required = MODE_KEYS[mode]
    allowed = required | OPTIONAL_KEYS[mode] | {"mode"}
    extra = set(args) - allowed
    if extra:
        raise DiaryRequestError(f"{sorted(extra)} not allowed with mode {mode}")
    missing = [k for k in required if args.get(k) in (None, "")]
    if missing:
        raise DiaryRequestError(f"mode {mode} requires {sorted(missing)}")

    query = args.get("query")
    if query is not None:
        if not isinstance(query, str) or not (1 <= len(query) <= MAX_QUERY_CHARS):
            raise DiaryRequestError(f"query must be 1-{MAX_QUERY_CHARS} characters")
    date_from = _date(args.get("date_from"), "date_from")
    date_to = _date(args.get("date_to"), "date_to")
    if (date_from is None) != (date_to is None):
        raise DiaryRequestError("date_from and date_to go together (both inclusive)")
    if date_from is not None and date_to is not None:
        if date_to < date_from:
            raise DiaryRequestError("date_to is before date_from")
        if mode == "timeline" and (date_to - date_from).days + 1 > MAX_TIMELINE_DAYS:
            raise DiaryRequestError(f"a timeline spans at most {MAX_TIMELINE_DAYS} days")
    source = args.get("source")
    if source is not None and (not isinstance(source, str) or not source):
        raise DiaryRequestError("source must be a source name")
    citation = args.get("citation")
    if citation is not None and not isinstance(citation, str):
        raise DiaryRequestError("citation must be a string")
    cursor = None
    if mode == "continue":
        try:
            cursor = UUID(str(args["cursor"]))
        except ValueError as exc:
            raise DiaryRequestError("cursor must be a cursor id") from exc
    return DiaryRequest(mode=mode, query=query, date_from=date_from, date_to=date_to,
                        source=source, citation=citation, cursor=cursor)


def _date(value: Any, name: str) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DiaryRequestError(f"{name} must be an ISO date (YYYY-MM-DD)")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise DiaryRequestError(f"{name} must be an ISO date (YYYY-MM-DD)") from exc


def _error(code: str, message: str = "") -> DiaryObservation:
    text = f"diary_query: {code}" + (f" — {message}" if message else "")
    return DiaryObservation(ok=False, text=text, data={"error": code})


def diary_query(args: Any, ctx: DiaryContext, *,
                embed_query: Callable[[str], list[float]] | None = None,
                budget: int | None = None,
                journal_id: UUID | None = None,
                record_telemetry: bool = True) -> DiaryObservation:
    t0 = _time.monotonic()
    try:
        req = validate_request(args)
    except DiaryRequestError as exc:
        return _error("invalid_request", str(exc))
    budget = budget if budget is not None else observation_budget()

    position = frozen = None
    if req.mode == "continue":
        err, cursor = load_cursor(req.cursor, ctx)
        if err is not None:
            return _error(err)
        req = DiaryRequest.from_json(cursor.request)
        position = dict(cursor.position)
        frozen = position.pop("candidates", None)

    result = _run_revalidated(req, ctx, embed_query, position, frozen)
    if not result.ok:
        return _error(result.error or "unavailable")
    rendered = render_items(result, budget)
    next_cursor = None
    if rendered.has_more:
        if rendered.first_unrendered is not None:
            pos = result.items[rendered.first_unrendered].resume
        else:
            pos = result.after_last
        if pos is not None:
            next_cursor = create_cursor(ctx, req, result.catalog_manifest, pos, result.candidates)
    text = with_cursor(rendered.text, str(next_cursor)) if next_cursor else rendered.text

    injected = [it.citation for it in rendered.rendered]
    data = {
        "mode": req.mode, "request": req.to_json(),
        "returned": [item_json(it) for it in result.items],
        "injected": injected,
        "next_cursor": str(next_cursor) if next_cursor else None,
        "degraded_routes": result.degraded_routes,
        "coverage": {"enumeration": result.enumeration, "metadata": result.metadata},
        "catalog_manifest": result.catalog_manifest,
        "timings_ms": {**result.timings_ms, "action": int((_time.monotonic() - t0) * 1000)},
        "budget": budget, "chars": len(text),
    }
    if record_telemetry:
        _telemetry(req, ctx, result, rendered.rendered, journal_id)
    return DiaryObservation(ok=True, text=text, compact=rendered.compact, data=data)


def _run_revalidated(req: DiaryRequest, ctx: DiaryContext, embed_query: Any,
                     position: Any, frozen: Any) -> DiaryResult:
    """Retrieve, then re-read the versions of every source it used. A policy
    or catalog change in between retries once, then fails source_changed."""
    for _attempt in range(2):
        result = retrieve_diary(req, ctx, embed_query=embed_query, position=position,
                                frozen=frozen)
        if not result.ok:
            return result
        if current_manifest(list(result.catalog_manifest)) == result.catalog_manifest:
            return result
        db.session.rollback()
    return DiaryResult(ok=False, mode=req.mode, error="source_changed")


def _telemetry(req: DiaryRequest, ctx: DiaryContext, result: DiaryResult,
               rendered: list, journal_id: UUID | None) -> None:
    source = f"diary.{req.mode}"
    injected = {it.citation for it in rendered}
    try:
        for rank, it in enumerate(result.items, start=1):
            meta = {"passage_uuid": str(it.passage_uuid) if it.passage_uuid else None,
                    "reasons": it.reasons, "catalog_manifest": result.catalog_manifest,
                    "degraded_routes": result.degraded_routes}
            common = dict(target_type="diary_citation", target_id=it.citation,
                          query=req.query, room_uuid=ctx.room_uuid, agent_uuid=ctx.agent_uuid,
                          journal_id=journal_id, source=source, retrieval_rank=rank,
                          metadata=meta, commit=False)
            db.record_retrieval_event(stage="retrieved", **common)
            if it.citation in injected:
                db.record_retrieval_event(stage="injected", **common)
        db.session.commit()
    except Exception:   # noqa: BLE001 — telemetry never breaks a read
        db.session.rollback()
