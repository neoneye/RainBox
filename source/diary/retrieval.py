"""Diary reads: eligibility, the literal/identifier/FTS/vector routes, rank
fusion with grouping, timeline, the citation reader and cursors.

Every route applies eligibility and explicit date/source constraints in SQL
before its LIMIT. Items come back in rank/source/timeline order with a
`resume` position each; the renderer packs as many as fit and the caller
turns the first unrendered item's position into a cursor (proposal §7).
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Callable
from uuid import UUID, uuid4

import sqlalchemy as sa

import db
from diary.citations import format_citation, parse_citation
from diary.parsing import find_identifiers, sha256_hex

ROUTE_CAP = 20
TIMELINE_BATCH = 30
LITERAL_BATCH = 30
SEARCH_CANDIDATES = 60
MAX_GROUPS = 4
MAX_PER_ENTRY = 2
OCCURRENCE_DATES = 8
RRF_K = 60
LITERAL_WINDOW = 600
LITERAL_LEAD = 160
READ_WINDOW = 600
LITERAL_TIMEOUT_MS = 2000
CURSOR_TTL = timedelta(minutes=30)
MAX_IDENTIFIER_TOKENS = 8
MIN_HASH_PREFIX = 7
MAX_FTS_LEXEMES = 32


@dataclass(frozen=True)
class DiaryContext:
    room_uuid: UUID | None
    agent_uuid: UUID | None
    models_local: bool = False
    # The operator's pilot probe: permits one named source even while it is
    # disabled. Never reachable from action args.
    trusted_source: UUID | None = None


@dataclass(frozen=True)
class DiaryRequest:
    mode: str
    query: str | None = None
    date_from: date | None = None     # inclusive
    date_to: date | None = None       # inclusive
    source: str | None = None
    citation: str | None = None
    cursor: UUID | None = None

    def to_json(self) -> dict[str, Any]:
        return {"mode": self.mode, "query": self.query,
                "date_from": self.date_from.isoformat() if self.date_from else None,
                "date_to": self.date_to.isoformat() if self.date_to else None,
                "source": self.source, "citation": self.citation}

    @staticmethod
    def from_json(raw: dict[str, Any]) -> "DiaryRequest":
        return DiaryRequest(
            mode=raw["mode"], query=raw.get("query"),
            date_from=date.fromisoformat(raw["date_from"]) if raw.get("date_from") else None,
            date_to=date.fromisoformat(raw["date_to"]) if raw.get("date_to") else None,
            source=raw.get("source"), citation=raw.get("citation"))


@dataclass
class DiaryItem:
    source_uuid: UUID
    source_name: str
    snapshot_at: datetime | None
    path: str
    entry_uuid: UUID | None
    passage_uuid: UUID | None
    cite_revision: UUID
    byte_start: int
    byte_end: int
    text: str
    date_local: date | None
    clock_start: time | None
    clock_end: time | None
    time_status: str
    date_basis: str
    author_token: str | None = None
    superseded: bool = False
    match_count: int = 0
    reasons: list[str] = field(default_factory=list)
    occurrence_dates: list[date] = field(default_factory=list)
    occurrence_more: int = 0
    resume: dict[str, Any] | None = None

    @property
    def citation(self) -> str:
        return format_citation(self.cite_revision, self.byte_start, self.byte_end)


@dataclass
class DiaryResult:
    ok: bool
    mode: str
    error: str | None = None
    items: list[DiaryItem] = field(default_factory=list)
    after_last: dict[str, Any] | None = None   # continuation past the last item, if more exist
    candidates: list[str] | None = None        # ranked search: the frozen candidate list
    degraded_routes: list[str] = field(default_factory=list)
    route_ranks: dict[str, dict[str, int]] = field(default_factory=dict)
    enumeration: str = "not_applicable"
    metadata: str = "complete"
    catalog_manifest: dict[str, list[int]] = field(default_factory=dict)
    timings_ms: dict[str, int] = field(default_factory=dict)
    total_candidates: int = 0


# --- eligibility -------------------------------------------------------------------


def eligible_sources(ctx: DiaryContext, source_name: str | None = None) -> list[Any]:
    """Sources this context may read: the room's enabled private sources
    (agent-restricted ones only for that agent), narrowed to those allowing
    remote models when the turn's models are not all local. The trusted
    probe context may add its one selected source even while disabled."""
    q = db.session.query(db.DiarySource).filter(db.DiarySource.sensitivity == "private")
    rows = []
    for s in q.all():
        trusted = ctx.trusted_source is not None and s.uuid == ctx.trusted_source
        if not trusted:
            if ctx.room_uuid is None or s.room_uuid != ctx.room_uuid or not s.enabled:
                continue
            if s.agent_uuid is not None and s.agent_uuid != ctx.agent_uuid:
                continue
            if not ctx.models_local and not s.allow_remote_models:
                continue
        if source_name is not None and s.name != source_name:
            continue
        rows.append(s)
    return sorted(rows, key=lambda s: str(s.uuid))


def diary_available(room_uuid: UUID, agent_uuid: UUID | None, models_local: bool) -> tuple[bool, str]:
    """Whether `diary_query` should be offered this turn, and if not, why
    (for the trace): no_source, no_ready_source or remote_models."""
    sources = [s for s in db.session.query(db.DiarySource)
               .filter(db.DiarySource.room_uuid == room_uuid,
                       db.DiarySource.sensitivity == "private").all()
               if s.agent_uuid is None or s.agent_uuid == agent_uuid]
    if not sources:
        return False, "no_source"
    enabled = [s for s in sources if s.enabled]
    if not enabled:
        return False, "no_enabled_source"
    usable = enabled if models_local else [s for s in enabled if s.allow_remote_models]
    if not usable:
        return False, "remote_models"
    ready = db.session.query(db.DiaryFile.uuid).filter(
        db.DiaryFile.source_uuid.in_([s.uuid for s in usable]),
        db.DiaryFile.availability == "ready").first()
    if ready is None:
        return False, "no_ready_file"
    return True, "available"


def catalog_manifest(sources: list[Any]) -> dict[str, list[int]]:
    return {str(s.uuid): [s.policy_version, s.catalog_version] for s in sources}


def current_manifest(source_ids: list[str]) -> dict[str, list[int]]:
    if not source_ids:
        return {}
    rows = db.session.query(db.DiarySource).filter(
        db.DiarySource.uuid.in_([UUID(x) for x in source_ids])).all()
    return {str(s.uuid): [s.policy_version, s.catalog_version] for s in rows}


_ELIGIBLE = """
    FROM diary_passage p
    JOIN diary_entry e ON e.uuid = p.entry_uuid
    JOIN diary_generation g ON g.uuid = e.generation_uuid
    JOIN diary_file f ON f.current_generation_uuid = g.uuid
    JOIN diary_source s ON s.uuid = f.source_uuid
    WHERE s.uuid = ANY(:sources)
      AND f.availability = 'ready'
      AND g.parser_fingerprint = s.parser_fingerprint
      AND NOT EXISTS (SELECT 1 FROM diary_exclusion x
                      WHERE x.source_uuid = s.uuid AND x.relative_path = f.relative_path)
"""
_ELIGIBLE_ENTRIES = _ELIGIBLE.replace(
    "FROM diary_passage p\n    JOIN diary_entry e ON e.uuid = p.entry_uuid",
    "FROM diary_entry e")
_DATE_FILTER = " AND e.date_local >= :date_from AND e.date_local < :date_until"

_ITEM_COLUMNS = """
    s.uuid AS source_uuid, s.name AS source_name, f.relative_path AS path,
    f.last_seen_at AS seen_at, e.uuid AS entry_uuid, e.ordinal, e.date_local,
    e.clock_start, e.clock_end, e.time_status, e.date_basis, e.author_token,
    e.byte_start AS entry_start, e.byte_end AS entry_end,
    e.cite_revision_uuid AS entry_cite
"""


def _params(sources: list[Any], req: DiaryRequest) -> dict[str, Any]:
    p: dict[str, Any] = {"sources": [s.uuid for s in sources]}
    if req.date_from is not None:
        p["date_from"] = req.date_from
        p["date_until"] = (req.date_to or req.date_from) + timedelta(days=1)
    return p


def _date_sql(req: DiaryRequest) -> str:
    return _DATE_FILTER if req.date_from is not None else ""


def _snapshot_times(sources: list[Any]) -> dict[UUID, datetime | None]:
    rows = db.session.execute(sa.text(
        "SELECT f.source_uuid, max(r.first_ingested_at) FROM diary_file f "
        "JOIN diary_generation g ON g.uuid = f.current_generation_uuid "
        "JOIN diary_revision r ON r.uuid = g.revision_uuid "
        "WHERE f.source_uuid = ANY(:s) GROUP BY f.source_uuid"),
        {"s": [s.uuid for s in sources]}).all()
    return {r[0]: r[1] for r in rows}


def _metadata_coverage(sources: list[Any], req: DiaryRequest) -> str:
    """partial when quarantined files, unresolved diagnostics, or (under a
    date filter) undated entries exist within the requested sources."""
    ids = [s.uuid for s in sources]
    if not ids:
        return "complete"
    bad = db.session.execute(sa.text(
        "SELECT 1 FROM diary_file WHERE source_uuid = ANY(:s) AND "
        "(availability = 'quarantined' OR jsonb_array_length(diagnostics) > 0) LIMIT 1"),
        {"s": ids}).first()
    if bad is not None:
        return "partial"
    if req.date_from is not None:
        undated = db.session.execute(sa.text(
            "SELECT 1 " + _ELIGIBLE_ENTRIES + " AND e.date_local IS NULL LIMIT 1"),
            {"sources": ids}).first()
        if undated is not None:
            return "partial"
    return "complete"


# --- entry point -----------------------------------------------------------------------


def retrieve_diary(req: DiaryRequest, ctx: DiaryContext, *,
                   embed_query: Callable[[str], list[float]] | None = None,
                   position: dict[str, Any] | None = None,
                   frozen: list[str] | None = None) -> DiaryResult:
    """Run one request. `position`/`frozen` come from a cursor."""
    t0 = _time.monotonic()
    if req.mode == "read" and position is None:
        result = read_citation(req, ctx)
    else:
        sources = eligible_sources(ctx, req.source)
        if not sources:
            return DiaryResult(ok=False, mode=req.mode, error="unavailable")
        if req.mode == "literal":
            result = _literal(req, sources, position)
        elif req.mode == "timeline":
            result = _timeline(req, sources, position)
        elif req.mode == "search":
            result = _search(req, sources, embed_query, position, frozen)
        elif req.mode == "read":
            result = _read_forward(sources, position or {})
        else:
            return DiaryResult(ok=False, mode=req.mode, error="invalid_request")
        if result.ok:
            result.catalog_manifest = catalog_manifest(sources)
            result.metadata = _metadata_coverage(sources, req)
            snaps = _snapshot_times(sources)
            for it in result.items:
                it.snapshot_at = snaps.get(it.source_uuid)
    result.timings_ms["total"] = int((_time.monotonic() - t0) * 1000)
    return result


# --- literal -----------------------------------------------------------------------------


def _like_pattern(needle: str) -> str:
    escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def literal_needles(query: str) -> list[str]:
    """The query as written, plus the spelling with the prompt fence's
    ‹ › reverted to < >, when that differs (§7)."""
    needles = [query]
    reverted = query.replace("‹", "<").replace("›", ">")
    if reverted != query:
        needles.append(reverted)
    return needles


_ENTRY_ORDER = "s.uuid, f.relative_path, e.ordinal"


def _literal(req: DiaryRequest, sources: list[Any], position: dict | None) -> DiaryResult:
    assert req.query is not None
    needles = literal_needles(req.query)
    params = _params(sources, req)
    likes = []
    for i, n in enumerate(needles):
        params[f"pat{i}"] = _like_pattern(n)
        likes.append(f"e.text LIKE :pat{i} ESCAPE '\\'")
    keyset = ""
    skip_before = 0
    if position:
        params.update({"ps": UUID(position["source"]), "pp": position["path"],
                       "po": position["ordinal"]})
        keyset = " AND (s.uuid, f.relative_path, e.ordinal) >= (:ps, :pp, :po)"
        skip_before = position.get("offset", 0)
    sql = ("SELECT " + _ITEM_COLUMNS + ", e.text AS entry_text " + _ELIGIBLE_ENTRIES
           + _date_sql(req) + " AND (" + " OR ".join(likes) + ")" + keyset
           + f" ORDER BY {_ENTRY_ORDER} LIMIT {LITERAL_BATCH + 1}")
    try:
        db.session.execute(sa.text(f"SET LOCAL statement_timeout = {LITERAL_TIMEOUT_MS}"))
        rows = db.session.execute(sa.text(sql), params).mappings().all()
    except sa.exc.OperationalError:
        db.session.rollback()
        return DiaryResult(ok=True, mode="literal", degraded_routes=["literal_timeout"],
                           enumeration="partial")
    more = len(rows) > LITERAL_BATCH
    rows = rows[:LITERAL_BATCH]
    items: list[DiaryItem] = []
    for idx, r in enumerate(rows):
        text = r["entry_text"]
        hits: list[tuple[int, int]] = []
        for n in needles:
            start = 0
            while True:
                k = text.find(n, start)
                if k < 0:
                    break
                hits.append((k, k + len(n)))
                start = k + 1
        hits = sorted(set(hits))
        first = (position is not None and idx == 0
                 and str(r["source_uuid"]) == position["source"]
                 and r["path"] == position["path"] and r["ordinal"] == position["ordinal"])
        for a, b in hits:
            if first and a < skip_before:
                continue
            ws, we = _literal_window(len(text), a, b)
            items.append(_entry_window_item(r, text, ws, we, resume={
                "source": str(r["source_uuid"]), "path": r["path"],
                "ordinal": r["ordinal"], "offset": a}))
    after = None
    if more:
        nxt = rows[-1]
        after = {"source": str(nxt["source_uuid"]), "path": nxt["path"],
                 "ordinal": nxt["ordinal"] + 1, "offset": 0}
    return DiaryResult(ok=True, mode="literal", items=items, after_last=after,
                       enumeration="partial" if more else "complete")


def _literal_window(length: int, a: int, b: int) -> tuple[int, int]:
    if b - a >= LITERAL_WINDOW:
        return a, a + LITERAL_WINDOW
    start = max(0, a - LITERAL_LEAD)
    end = min(length, start + LITERAL_WINDOW)
    if end < b:
        start = max(0, b - LITERAL_WINDOW)
        end = b
    return start, end


def _char_to_byte(text: str, char: int) -> int:
    return len(text[:char].encode("utf-8"))


def _entry_window_item(r: Any, text: str, ws: int, we: int, *, resume: dict) -> DiaryItem:
    base = r["entry_start"]
    return DiaryItem(
        source_uuid=r["source_uuid"], source_name=r["source_name"], snapshot_at=None,
        path=r["path"], entry_uuid=r["entry_uuid"], passage_uuid=None,
        cite_revision=r["entry_cite"],
        byte_start=base + _char_to_byte(text, ws), byte_end=base + _char_to_byte(text, we),
        text=text[ws:we], date_local=r["date_local"], clock_start=r["clock_start"],
        clock_end=r["clock_end"], time_status=r["time_status"], date_basis=r["date_basis"],
        author_token=r["author_token"], match_count=1, reasons=["literal"], resume=resume)


# --- timeline ------------------------------------------------------------------------------


_TIMELINE_KEY = ("(e.date_local, e.clock_start IS NOT NULL, COALESCE(e.clock_start, '00:00'::time), "
                 "s.uuid, f.relative_path, e.ordinal)")


def _passage_item(r: Any, *, reasons: list[str] | None = None, resume: dict | None = None) -> DiaryItem:
    return DiaryItem(
        source_uuid=r["source_uuid"], source_name=r["source_name"], snapshot_at=None,
        path=r["path"], entry_uuid=r["entry_uuid"], passage_uuid=r["passage_uuid"],
        cite_revision=r["passage_cite"], byte_start=r["p_start"], byte_end=r["p_end"],
        text=r["p_text"], date_local=r["date_local"], clock_start=r["clock_start"],
        clock_end=r["clock_end"], time_status=r["time_status"], date_basis=r["date_basis"],
        author_token=r["author_token"], reasons=reasons or [], resume=resume)


_PASSAGE_COLUMNS = _ITEM_COLUMNS + """,
    p.uuid AS passage_uuid, p.part_index, p.byte_start AS p_start, p.byte_end AS p_end,
    p.text AS p_text, p.text_hash, p.cite_revision_uuid AS passage_cite
"""


def _timeline(req: DiaryRequest, sources: list[Any], position: dict | None) -> DiaryResult:
    params = _params(sources, req)
    keyset = ""
    if position:
        params.update({
            "kd": date.fromisoformat(position["date"]), "kh": position["has_clock"],
            "kc": time.fromisoformat(position["clock"]), "ks": UUID(position["source"]),
            "kp": position["path"], "ko": position["ordinal"]})
        keyset = f" AND {_TIMELINE_KEY} >= (:kd, :kh, :kc, :ks, :kp, :ko)"
    entry_sql = ("SELECT e.uuid " + _ELIGIBLE_ENTRIES + _date_sql(req) + keyset
                 + f" ORDER BY {_TIMELINE_KEY} LIMIT {TIMELINE_BATCH + 1}")
    entry_ids = [r[0] for r in db.session.execute(sa.text(entry_sql), params).all()]
    more = len(entry_ids) > TIMELINE_BATCH
    batch = entry_ids[:TIMELINE_BATCH]
    if not batch:
        return DiaryResult(ok=True, mode="timeline", enumeration="complete")
    rows = db.session.execute(sa.text(
        "SELECT " + _PASSAGE_COLUMNS + _ELIGIBLE + " AND e.uuid = ANY(:ids)"
        f" ORDER BY {_TIMELINE_KEY}, p.part_index"),
        {**params, "ids": batch}).mappings().all()
    items = []
    for r in rows:
        key = _timeline_resume(r)
        if position and _same_entry(key, position) and r["part_index"] < position.get("part", 0):
            continue
        items.append(_passage_item(r, reasons=["timeline"], resume={**key, "part": r["part_index"]}))
    after = None
    if more:
        nxt = db.session.execute(sa.text(
            "SELECT " + _PASSAGE_COLUMNS + _ELIGIBLE + " AND e.uuid = :id LIMIT 1"),
            {**params, "id": entry_ids[TIMELINE_BATCH]}).mappings().first()
        after = {**_timeline_resume(nxt), "part": 0}
    return DiaryResult(ok=True, mode="timeline", items=items, after_last=after,
                       enumeration="partial" if more else "complete")


def _timeline_resume(r: Any) -> dict[str, Any]:
    return {"date": r["date_local"].isoformat(), "has_clock": r["clock_start"] is not None,
            "clock": (r["clock_start"] or time(0, 0)).isoformat(),
            "source": str(r["source_uuid"]), "path": r["path"], "ordinal": r["ordinal"]}


def _same_entry(key: dict, position: dict) -> bool:
    return all(key[k] == position.get(k) for k in ("date", "has_clock", "clock", "source", "path", "ordinal"))


# --- search -------------------------------------------------------------------------------


def _search(req: DiaryRequest, sources: list[Any], embed_query: Callable | None,
            position: dict | None, frozen: list[str] | None) -> DiaryResult:
    assert req.query is not None
    result = DiaryResult(ok=True, mode="search")
    if frozen is None:
        ranks: dict[str, dict[str, int]] = {}
        for name, route in (("identifier", _identifier_route), ("fts", _fts_route)):
            t0 = _time.monotonic()
            ranks[name] = {str(pid): i + 1 for i, pid in enumerate(route(req, sources))}
            result.timings_ms[name] = int((_time.monotonic() - t0) * 1000)
        vector_sources = [s for s in sources if s.vector_mode in ("exact", "hnsw")]
        if vector_sources:
            t0 = _time.monotonic()
            if embed_query is None:
                result.degraded_routes.append("vector_unavailable")
            else:
                try:
                    from diary.embeddings import vector_route
                    ids = vector_route(req, vector_sources, embed_query, _params(sources, req),
                                       _date_sql(req))
                    ranks["vector"] = {str(pid): i + 1 for i, pid in enumerate(ids)}
                except Exception:   # noqa: BLE001 — a vector failure never removes lexical results
                    db.session.rollback()
                    result.degraded_routes.append("vector_failed")
            result.timings_ms["vector"] = int((_time.monotonic() - t0) * 1000)
        frozen = _fuse(ranks, sources)
        result.route_ranks = ranks
    result.candidates = frozen
    start = (position or {}).get("index", 0)
    result.total_candidates = len(frozen)
    groups = _load_groups(frozen, sources, _params(sources, req), req)
    for i in range(start, len(groups)):
        groups[i].resume = {"index": i}
    result.items = groups[start:]
    return result


def _identifier_route(req: DiaryRequest, sources: list[Any]) -> list[UUID]:
    tokens = [(sub, val) for sub, val, _, _ in find_identifiers(req.query or "")]
    seen: list[tuple[str, str]] = []
    for t in tokens:
        if t not in seen:
            seen.append(t)
    seen = seen[:MAX_IDENTIFIER_TOKENS]
    if not seen:
        return []
    params = _params(sources, req)
    clauses = []
    for i, (sub, val) in enumerate(seen):
        params[f"sub{i}"] = sub
        params[f"val{i}"] = val
        clauses.append(f"(a.subtype = :sub{i} AND a.value = :val{i})")
        if sub == "hash" and len(val) >= MIN_HASH_PREFIX:
            params[f"pre{i}"] = val + "%"
            clauses.append(f"(a.subtype = 'hash' AND a.value LIKE :pre{i})")
    exact = " OR ".join(f"(a.subtype = :sub{i} AND a.value = :val{i})" for i in range(len(seen)))
    # The tier is recomputed per passage from its matching annotations.
    sql = ("SELECT p.uuid, (SELECT min(CASE WHEN " + exact + " THEN 0 ELSE 1 END) "
           "FROM diary_annotation a WHERE a.entry_uuid = e.uuid AND a.kind = 'identifier' "
           "AND a.byte_start < p.byte_end AND a.byte_end > p.byte_start "
           "AND (" + " OR ".join(clauses) + ")) AS tier, s.uuid AS su, f.relative_path AS fp, "
           "e.ordinal AS eo, p.part_index AS pi "
           + _ELIGIBLE + _date_sql(req))
    sql = f"SELECT uuid FROM ({sql}) t WHERE tier IS NOT NULL ORDER BY tier, su, fp, eo, pi LIMIT {ROUTE_CAP}"
    return [r[0] for r in db.session.execute(sa.text(sql), params).all()]


def _fts_route(req: DiaryRequest, sources: list[Any]) -> list[UUID]:
    lexemes = [r[0] for r in db.session.execute(sa.text(
        "SELECT unnest(tsvector_to_array(to_tsvector('simple', :q)))"), {"q": req.query}).all()]
    lexemes = lexemes[:MAX_FTS_LEXEMES]
    if not lexemes:
        return []
    params = _params(sources, req)
    parts = []
    for i, lx in enumerate(lexemes):
        params[f"lx{i}"] = lx
        parts.append(f"plainto_tsquery('simple', :lx{i})")
    tsq = " || ".join(parts)
    sql = (f"SELECT p.uuid, ts_rank(p.search_vector, ({tsq})) AS rank "
           + _ELIGIBLE + _date_sql(req) + f" AND p.search_vector @@ ({tsq})"
           f" ORDER BY rank DESC, s.uuid, f.relative_path, e.ordinal, p.part_index LIMIT {ROUTE_CAP}")
    return [r[0] for r in db.session.execute(sa.text(sql), params).all()]


def _fuse(ranks: dict[str, dict[str, int]], sources: list[Any]) -> list[str]:
    """Reciprocal-rank fusion over passage IDs, deterministic tie-break by
    source order. Returns up to SEARCH_CANDIDATES passage IDs."""
    scores: dict[str, float] = {}
    for route in ranks.values():
        for pid, rank in route.items():
            scores[pid] = scores.get(pid, 0.0) + 1.0 / (RRF_K + rank)
    if not scores:
        return []
    order = {str(r[0]): (str(r[1]), r[2], r[3], r[4]) for r in db.session.execute(sa.text(
        "SELECT p.uuid, f.source_uuid, f.relative_path, e.ordinal, p.part_index "
        "FROM diary_passage p JOIN diary_entry e ON e.uuid = p.entry_uuid "
        "JOIN diary_file f ON f.current_generation_uuid = e.generation_uuid "
        "WHERE p.uuid = ANY(:ids)"), {"ids": [UUID(x) for x in scores]}).all()}
    ranked = sorted((pid for pid in scores if pid in order),
                    key=lambda pid: (-scores[pid], order[pid]))
    return ranked[:SEARCH_CANDIDATES]


def _load_groups(candidate_ids: list[str], sources: list[Any], params: dict,
                 req: DiaryRequest) -> list[DiaryItem]:
    """Rank-ordered groups: identical text within a source renders once,
    with its most recent occurrence's citation and an occurrence list;
    at most MAX_PER_ENTRY passages from one entry. Candidates that are no
    longer eligible are dropped."""
    if not candidate_ids:
        return []
    rows = {str(r["passage_uuid"]): r for r in db.session.execute(sa.text(
        "SELECT " + _PASSAGE_COLUMNS + _ELIGIBLE + " AND p.uuid = ANY(:ids)"),
        {**params, "ids": [UUID(x) for x in candidate_ids]}).mappings().all()}
    groups: list[DiaryItem] = []
    by_key: dict[tuple, DiaryItem] = {}
    per_entry: dict[UUID, int] = {}
    for pid in candidate_ids:
        r = rows.get(pid)
        if r is None:
            continue
        key = (r["source_uuid"], r["text_hash"])
        if key in by_key:
            continue
        if per_entry.get(r["entry_uuid"], 0) >= MAX_PER_ENTRY:
            continue
        per_entry[r["entry_uuid"]] = per_entry.get(r["entry_uuid"], 0) + 1
        item = _passage_item(r, reasons=["search"])
        by_key[key] = item
        groups.append(item)
    _attach_occurrences(groups, params, req)
    return groups


def _attach_occurrences(groups: list[DiaryItem], params: dict, req: DiaryRequest) -> None:
    for g in groups:
        text_hash = sha256_hex(g.text.encode("utf-8"))
        rows = db.session.execute(sa.text(
            "SELECT e.date_local, p.cite_revision_uuid, p.byte_start, p.byte_end, f.relative_path, "
            "e.uuid AS entry_uuid, p.uuid AS passage_uuid, e.clock_start, e.clock_end, "
            "e.time_status, e.date_basis, e.author_token "
            + _ELIGIBLE + _date_sql(req) + " AND s.uuid = :src AND p.text_hash = :h "
            "ORDER BY e.date_local DESC NULLS LAST, f.relative_path, e.ordinal"),
            {**params, "src": g.source_uuid, "h": text_hash}).mappings().all()
        if len(rows) <= 1:
            continue
        latest = rows[0]
        g.cite_revision, g.byte_start, g.byte_end = (latest["cite_revision_uuid"],
                                                     latest["byte_start"], latest["byte_end"])
        g.path, g.entry_uuid, g.passage_uuid = latest["relative_path"], latest["entry_uuid"], latest["passage_uuid"]
        g.date_local, g.clock_start, g.clock_end = latest["date_local"], latest["clock_start"], latest["clock_end"]
        g.time_status, g.date_basis, g.author_token = latest["time_status"], latest["date_basis"], latest["author_token"]
        others = sorted({r["date_local"] for r in rows[1:] if r["date_local"] is not None}, reverse=True)
        g.occurrence_dates = others[:OCCURRENCE_DATES]
        g.occurrence_more = (len(rows) - 1) - len(g.occurrence_dates)


# --- citation reader ------------------------------------------------------------------------


def read_citation(req: DiaryRequest, ctx: DiaryContext) -> DiaryResult:
    """Open a citation as a reader (§7). Unknown, denied and unavailable
    citations are all not_found; syntax errors are invalid_request."""
    c = parse_citation(req.citation or "")
    if c is None:
        return DiaryResult(ok=False, mode="read", error="invalid_request")
    not_found = DiaryResult(ok=False, mode="read", error="not_found")
    rev = db.session.get(db.DiaryRevision, c.revision_uuid)
    if rev is None:
        return not_found
    f = db.session.get(db.DiaryFile, rev.file_uuid)
    sources = {s.uuid: s for s in eligible_sources(ctx)}
    source = sources.get(f.source_uuid) if f else None
    if source is None or f.availability != "ready" or f.current_generation_uuid is None:
        return not_found
    if f.relative_path in db.diary_exclusions(source.uuid):
        return not_found
    if c.byte_end > rev.byte_length:
        return not_found
    raw = db.diary_revision_bytes(rev)
    start = c.byte_start
    if raw.startswith(b"\xef\xbb\xbf") and start < 3:
        start = 3
    try:
        raw[start:c.byte_end].decode("utf-8")
    except UnicodeDecodeError:
        return not_found
    gen = db.session.get(db.DiaryGeneration, f.current_generation_uuid)
    cur = db.session.get(db.DiaryRevision, gen.revision_uuid)
    on_chain = cur.uuid == rev.uuid
    if not on_chain and rev.byte_length <= cur.byte_length:
        on_chain = sha256_hex(db.diary_revision_bytes(cur)[: rev.byte_length]) == rev.sha256
    manifest = catalog_manifest([source])
    if on_chain:
        entry = db.session.execute(sa.text(
            "SELECT ordinal FROM diary_entry WHERE generation_uuid = :g AND byte_end > :b "
            "ORDER BY ordinal LIMIT 1"), {"g": gen.uuid, "b": start}).first()
        if entry is None:
            return DiaryResult(ok=True, mode="read", enumeration="complete", catalog_manifest=manifest)
        result = _read_forward([source], {"file": str(f.uuid), "ordinal": entry[0], "part": 0})
    else:
        result = _read_superseded(source, f, rev, raw, start)
    result.catalog_manifest = manifest
    snaps = _snapshot_times([source])
    for it in result.items:
        if it.snapshot_at is None:
            it.snapshot_at = snaps.get(it.source_uuid)
    return result


def _read_forward(sources: list[Any], position: dict[str, Any]) -> DiaryResult:
    """Entries of one file in source order from (ordinal, part)."""
    if position.get("superseded"):
        rev = db.session.get(db.DiaryRevision, UUID(position["revision"]))
        f = db.session.get(db.DiaryFile, rev.file_uuid) if rev else None
        source = next((s for s in sources if f and s.uuid == f.source_uuid), None)
        if source is None:
            return DiaryResult(ok=False, mode="read", error="not_found")
        return _read_superseded(source, f, rev, db.diary_revision_bytes(rev), position["byte"])
    params = {"sources": [s.uuid for s in sources], "file": UUID(position["file"]),
              "ord": position["ordinal"]}
    rows = db.session.execute(sa.text(
        "SELECT " + _PASSAGE_COLUMNS + _ELIGIBLE + " AND f.uuid = :file AND e.ordinal >= :ord "
        f"ORDER BY e.ordinal, p.part_index LIMIT {TIMELINE_BATCH * 4 + 1}"), params).mappings().all()
    more = len(rows) > TIMELINE_BATCH * 4
    rows = rows[:TIMELINE_BATCH * 4]
    items = []
    for r in rows:
        if r["ordinal"] == position["ordinal"] and r["part_index"] < position.get("part", 0):
            continue
        items.append(_passage_item(r, reasons=["read"], resume={
            "file": position["file"], "ordinal": r["ordinal"], "part": r["part_index"]}))
    after = None
    if more and rows:
        last = rows[-1]
        after = {"file": position["file"], "ordinal": last["ordinal"] + 1, "part": 0}
    return DiaryResult(ok=True, mode="read", items=items, after_last=after,
                       enumeration="partial" if more else "complete")


def _read_superseded(source: Any, f: Any, rev: Any, raw: bytes, start: int) -> DiaryResult:
    """A citation into a snapshot the current file no longer contains: raw
    windows from the start, labeled superseded, no date metadata."""
    text = raw[start:].decode("utf-8", errors="strict") if start < len(raw) else ""
    items = []
    pos_char = 0
    byte = start
    while pos_char < len(text) and len(items) < 8:
        piece = text[pos_char:pos_char + READ_WINDOW]
        nb = len(piece.encode("utf-8"))
        items.append(DiaryItem(
            source_uuid=source.uuid, source_name=source.name, snapshot_at=rev.first_ingested_at,
            path=f.relative_path, entry_uuid=None, passage_uuid=None, cite_revision=rev.uuid,
            byte_start=byte, byte_end=byte + nb, text=piece, date_local=None, clock_start=None,
            clock_end=None, time_status="unknown", date_basis="unknown", superseded=True,
            reasons=["read"], resume={"superseded": True, "revision": str(rev.uuid), "byte": byte}))
        pos_char += len(piece)
        byte += nb
    more = pos_char < len(text)
    after = {"superseded": True, "revision": str(rev.uuid), "byte": byte} if more else None
    return DiaryResult(ok=True, mode="read", items=items, after_last=after,
                       enumeration="partial" if more else "complete", metadata="partial")


# --- cursors ------------------------------------------------------------------------------


def create_cursor(ctx: DiaryContext, req: DiaryRequest, manifest: dict, position: dict,
                  frozen: list[str] | None = None) -> UUID:
    now = datetime.now(UTC)
    db.session.execute(sa.delete(db.DiaryCursor).where(db.DiaryCursor.expires_at < now))
    cid = uuid4()
    pos = dict(position)
    if frozen is not None:
        pos["candidates"] = frozen
    db.session.add(db.DiaryCursor(
        uuid=cid, room_uuid=ctx.room_uuid, agent_uuid=ctx.agent_uuid,
        request=req.to_json(), catalog_manifest=manifest, position=pos,
        expires_at=now + CURSOR_TTL))
    db.session.commit()
    return cid


def load_cursor(cursor_uuid: UUID, ctx: DiaryContext) -> tuple[str | None, Any]:
    """(error, cursor): error is not_found, cursor_expired or cursor_stale."""
    cur = db.session.get(db.DiaryCursor, cursor_uuid)
    if cur is None or cur.room_uuid != ctx.room_uuid or cur.agent_uuid != ctx.agent_uuid:
        return "not_found", None
    if cur.expires_at <= datetime.now(UTC):
        return "cursor_expired", None
    if current_manifest(list(cur.catalog_manifest)) != cur.catalog_manifest:
        return "cursor_stale", None
    return None, cur
