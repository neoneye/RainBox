"""`diary_recall` eval cases (proposal §10): deterministic scoring of one
diary_query against gold byte ranges.

Case shape:
    input:    {"room_uuid", "agent_uuid", "models_local": true, "args": {...}}
    expected: {"gold": [{"path", "sha256", "byte_start", "byte_end"}],
               "forbidden_sources": ["<source uuid>", ...]}

Gold names a file by path and the hash of the bytes it was pinned against,
never a generated UUID. Candidate recall (gold overlapped by anything
returned) and injected coverage (gold overlapped by what reached the prompt)
are recorded separately. Forbidden-source exposure, an injected citation
that does not resolve, a budget overflow and any write are hard failures:
the score is 0 whatever the recall, so an average cannot mask them.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import sqlalchemy as sa

import db
from diary.action import diary_query
from diary.citations import parse_citation
from diary.retrieval import DiaryContext


def _resolve(citation: str) -> tuple[str, int, int, bytes] | None:
    c = parse_citation(citation)
    if c is None:
        return None
    rev = db.session.get(db.DiaryRevision, c.revision_uuid)
    if rev is None or c.byte_end > rev.byte_length:
        return None
    f = db.session.get(db.DiaryFile, rev.file_uuid)
    raw = db.diary_revision_bytes(rev)
    return f.relative_path, c.byte_start, c.byte_end, raw


def _gold_bytes(gold: dict[str, Any]) -> bytes | None:
    rev = (db.session.query(db.DiaryRevision)
           .join(db.DiaryFile, db.DiaryFile.uuid == db.DiaryRevision.file_uuid)
           .filter(db.DiaryFile.relative_path == gold["path"],
                   db.DiaryRevision.sha256 == gold["sha256"]).first())
    if rev is None:
        return None
    return db.diary_revision_bytes(rev)[gold["byte_start"]:gold["byte_end"]]


def _hits(gold: dict[str, Any], resolved: list[tuple[str, int, int, bytes]]) -> bool:
    want = _gold_bytes(gold)
    for path, start, end, raw in resolved:
        if path != gold["path"] or not (start < gold["byte_end"] and gold["byte_start"] < end):
            continue
        # Same offsets, same bytes: the citation's revision contains the gold.
        if want is not None and raw[gold["byte_start"]:gold["byte_end"]] == want:
            return True
    return False


def _write_count() -> int:
    return db.session.execute(sa.text("SELECT count(*) FROM assistant_write_intent")).scalar() or 0


def score_diary_recall_case(case: Any, *, budget: int | None = None) -> tuple[float, dict[str, Any]]:
    inp = case.input or {}
    expected = case.expected or {}
    ctx = DiaryContext(
        room_uuid=UUID(inp["room_uuid"]) if inp.get("room_uuid") else None,
        agent_uuid=UUID(inp["agent_uuid"]) if inp.get("agent_uuid") else None,
        models_local=bool(inp.get("models_local", True)))
    writes_before = _write_count()
    obs = diary_query(inp.get("args") or {}, ctx, budget=budget, record_telemetry=False)
    writes_after = _write_count()

    returned = [r["citation"] for r in obs.data.get("returned", [])]
    injected = obs.data.get("injected", [])
    resolved_returned = [r for r in (_resolve(c) for c in returned) if r is not None]
    resolved_injected = [_resolve(c) for c in injected]
    gold = list(expected.get("gold") or [])
    forbidden = {str(s) for s in expected.get("forbidden_sources") or []}

    hard: list[str] = []
    if any(r["source_uuid"] in forbidden for r in obs.data.get("returned", [])):
        hard.append("forbidden_source_exposure")
    if any(r is None for r in resolved_injected):
        hard.append("invalid_citation")
    limit = obs.data.get("budget")
    if limit is not None and len(obs.text) > limit:
        hard.append("budget_overflow")
    if writes_after != writes_before:
        hard.append("unexpected_write")

    injected_ok = [r for r in resolved_injected if r is not None]
    candidate_recall = (sum(_hits(g, resolved_returned) for g in gold) / len(gold)) if gold else None
    injected_coverage = (sum(_hits(g, injected_ok) for g in gold) / len(gold)) if gold else None
    if hard:
        score = 0.0
    elif gold:
        score = injected_coverage or 0.0
    else:   # a negative case: nothing should come back
        score = 1.0 if not injected else 0.0
    return score, {
        "ok": obs.ok, "error": obs.data.get("error"), "hard_failures": hard,
        "candidate_recall": candidate_recall, "injected_coverage": injected_coverage,
        "returned": len(returned), "injected": len(injected), "chars": len(obs.text),
    }
