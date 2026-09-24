"""The pilot probe (proposal §9): run a fixed private query set against one
source through a trusted operator context, score injected citations
against gold byte ranges, time the routes, and apply the release gate.

Reports hold ids, kinds, ranks, counts and timings — never diary text.

Cases file:
    {"cases": [{"id": "lit-1", "kind": "literal|topical|timeline|negative",
                "args": {<diary_query args>},
                "gold": [{"path": "...", "byte_start": 0, "byte_end": 10}]}]}
"""

from __future__ import annotations

import time as _time
from typing import Any, Callable

import db
from diary.action import diary_query
from diary.citations import parse_citation
from diary.retrieval import DiaryContext

KINDS = ("literal", "topical", "timeline", "negative")
MAX_PAGES = 40
WARM_PASSES = 3


def trusted_context(source: Any, *, vectors: str | None = None) -> DiaryContext:
    """The operator's probe context: the source's own room and agent, the
    selected source readable even while disabled. Exclusions and quarantine
    still apply. Not constructible from action args."""
    return DiaryContext(room_uuid=source.room_uuid, agent_uuid=source.agent_uuid,
                        models_local=True, trusted_source=source.uuid,
                        vector_mode_override=vectors)


def _locate(citation: str) -> tuple[str, int, int] | None:
    c = parse_citation(citation)
    if c is None:
        return None
    rev = db.session.get(db.DiaryRevision, c.revision_uuid)
    if rev is None:
        return None
    f = db.session.get(db.DiaryFile, rev.file_uuid)
    return f.relative_path, c.byte_start, c.byte_end


def _overlaps(hit: tuple[str, int, int], gold: dict[str, Any]) -> bool:
    return hit[0] == gold["path"] and hit[1] < gold["byte_end"] and gold["byte_start"] < hit[2]


def _pages(args: dict, ctx: DiaryContext, embed_query: Any) -> tuple[list[list[str]], bool, float]:
    """All injected citations page by page, following cursors."""
    pages: list[list[str]] = []
    t0 = _time.monotonic()
    obs = diary_query(args, ctx, embed_query=embed_query, record_telemetry=False)
    first_ms = (_time.monotonic() - t0) * 1000
    ok = obs.ok
    while obs.ok:
        pages.append(obs.data["injected"])
        cursor = obs.data.get("next_cursor")
        if not cursor or len(pages) >= MAX_PAGES:
            break
        obs = diary_query({"mode": "continue", "cursor": cursor}, ctx, embed_query=embed_query,
                          record_telemetry=False)
        ok = ok and obs.ok
    return pages, ok, first_ms


def score_case(case: dict[str, Any], ctx: DiaryContext, embed_query: Any) -> dict[str, Any]:
    kind = case["kind"]
    gold = case.get("gold", [])
    enumerate_all = kind in ("literal", "timeline", "negative")
    if enumerate_all:
        pages, ok, ms = _pages(case["args"], ctx, embed_query)
    else:
        obs = diary_query(case["args"], ctx, embed_query=embed_query, record_telemetry=False)
        pages, ok, ms = [obs.data.get("injected", [])] if obs.ok else [], obs.ok, 0.0
    flat = [c for page in pages for c in page]
    hits = [h for h in (_locate(c) for c in flat) if h is not None]
    first_rank = next((i + 1 for i, h in enumerate(hits) if any(_overlaps(h, g) for g in gold)), None)
    covered = [any(_overlaps(h, g) for h in hits) for g in gold]
    duplicates = len(flat) - len(set(flat))
    if kind == "negative":
        correct = ok and not flat
    elif kind == "topical":
        correct = ok and first_rank is not None
    else:   # literal, timeline: every gold range, nothing outside gold, no duplicates
        extra = [h for h in hits if not any(_overlaps(h, g) for g in gold)]
        correct = ok and all(covered) and not extra and duplicates == 0
    return {"id": case["id"], "kind": kind, "ok": ok, "correct": correct,
            "first_gold_rank": first_rank, "gold_covered": sum(covered), "gold": len(gold),
            "returned": len(flat), "pages": len(pages), "duplicates": duplicates,
            "first_page_ms": round(ms, 1)}


def _percentile(samples: list[float], q: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    k = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return round(ordered[k], 1)


def run_probe(source: Any, cases: list[dict[str, Any]], *, vectors: str | None = None,
              embed_query: Callable | None = None, timing_passes: int = WARM_PASSES) -> dict[str, Any]:
    for case in cases:
        if case.get("kind") not in KINDS or "id" not in case or "args" not in case:
            raise ValueError("each case needs id, kind (literal|topical|timeline|negative) and args")
    ctx = trusted_context(source, vectors=vectors)
    results = [score_case(c, ctx, embed_query) for c in cases]
    timings: dict[str, list[float]] = {}
    if timing_passes:
        for c in cases:   # one warm-up pass
            diary_query(c["args"], ctx, embed_query=embed_query, record_telemetry=False)
        for _ in range(timing_passes):
            for c in cases:
                t0 = _time.monotonic()
                diary_query(c["args"], ctx, embed_query=embed_query, record_telemetry=False)
                mode = c["args"].get("mode", "?")
                timings.setdefault(mode, []).append((_time.monotonic() - t0) * 1000)
    latency = {mode: {"p50_ms": _percentile(s, 0.5), "p95_ms": _percentile(s, 0.95),
                      "samples": [round(x, 1) for x in s]} for mode, s in timings.items()}
    return {"source_uuid": str(source.uuid), "vectors": vectors or source.vector_mode,
            "cases": results, "latency": latency, "gate": release_gate(results)}


def release_gate(results: list[dict[str, Any]]) -> dict[str, Any]:
    """The executable part of §9's gate: every literal exact, at least 7 of
    8 topical (scaled to the set size), every timeline complete, every
    negative empty, and the full inventory present."""
    by_kind = {k: [r for r in results if r["kind"] == k] for k in KINDS}
    topical = by_kind["topical"]
    need_topical = -(-7 * len(topical) // 8) if topical else 0
    reasons = []
    if not all(r["correct"] for r in by_kind["literal"]):
        reasons.append("literal")
    if sum(r["correct"] for r in topical) < need_topical:
        reasons.append("topical")
    if not all(r["correct"] for r in by_kind["timeline"]):
        reasons.append("timeline")
    if not all(r["correct"] for r in by_kind["negative"]):
        reasons.append("negative")
    return {"passed": not reasons, "failed": reasons,
            "topical_correct": sum(r["correct"] for r in topical), "topical_needed": need_topical}


def hnsw_gate(source: Any, cases: list[dict[str, Any]], embed_query: Callable) -> dict[str, Any]:
    """Filtered Recall@20 of the HNSW vector route against exact, over the
    topical queries, plus the latency and exact-lookup conditions (§7).
    Records the pass on the source's embedding spec only when all hold."""
    from diary.embeddings import vector_route
    from diary.retrieval import DiaryRequest, _date_sql, _params, eligible_sources

    ctx = trusted_context(source)
    sources = [s for s in eligible_sources(ctx) if s.uuid == source.uuid]
    recalls, exact_ms, hnsw_ms = [], [], []
    for c in cases:
        if c["kind"] != "topical":
            continue
        req = DiaryRequest(mode="search", query=c["args"]["query"])
        params, dsql = _params(sources, req), _date_sql(req)
        t0 = _time.monotonic()
        exact = vector_route(req, sources, embed_query, params, dsql, mode_override="exact")
        exact_ms.append((_time.monotonic() - t0) * 1000)
        t0 = _time.monotonic()
        approx = vector_route(req, sources, embed_query, params, dsql, mode_override="hnsw")
        hnsw_ms.append((_time.monotonic() - t0) * 1000)
        db.session.rollback()
        if exact:
            recalls.append(len(set(exact) & set(approx)) / len(exact))
    lookups = run_probe(source, [c for c in cases if c["kind"] == "literal"],
                        vectors="hnsw", embed_query=embed_query, timing_passes=0)
    recall = min(recalls) if recalls else 0.0
    p95_exact, p95_hnsw = _percentile(exact_ms, 0.95), _percentile(hnsw_ms, 0.95)
    improved = p95_exact > 0 and p95_hnsw <= 0.8 * p95_exact
    passed = bool(recalls) and recall >= 0.95 and improved and lookups["gate"]["passed"]
    if passed:
        spec = dict(source.embedding_spec or {})
        spec["hnsw_passed"] = True
        source.embedding_spec = spec
        db.session.commit()
    return {"recall_at_20_min": round(recall, 3), "p95_exact_ms": p95_exact,
            "p95_hnsw_ms": p95_hnsw, "latency_improved": improved,
            "exact_lookups_ok": lookups["gate"]["passed"], "passed": passed}
