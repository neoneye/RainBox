"""Shared KB / vector-store / match plumbing used by `agents/query.py` and
`agents/query_filter_router.py`. Extracted to avoid `agents/query_filter_router`
importing underscore-prefixed names across module boundaries (the prior
pattern).

The names retain their leading underscores from the original `agents/query.py`
because the existing call sites already use them with that convention. Both
caller modules import them explicitly — that's the public API of this module.
"""

import json
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
import sqlalchemy as sa
from llama_index.core import Document, VectorStoreIndex
from llama_index.core.storage.storage_context import StorageContext
from llama_index.embeddings.openai import OpenAIEmbedding
from llama_index.vector_stores.postgres import PGVectorStore
from psycopg import sql

import db
from agents.query_handlers import HANDLERS, QueryContext

logger = logging.getLogger(__name__)

QA_JSONL_PATH: Path = Path(__file__).resolve().parent.parent / "data" / "question_answer.jsonl"
QA_TABLE_NAME: str = "seed_memory"   # PGVectorStore creates table "data_seed_memory"
QA_FULL_TABLE: str = f"data_{QA_TABLE_NAME}"
# Embeddings run on Ollama (the same server already used for chat at :11434),
# so Q&A retrieval depends on one local server. `embeddinggemma:300m` is 768-dim,
# matching the pgvector column; changing the embedder requires rebuilding the
# stored vectors (QUERY_AGENT_REBUILD_KB=1, or the "Repopulate Q&A memory" button)
# because rows are keyed by model_name and won't match across embedders. Override
# the host with OLLAMA_BASE_URL (matching providers/ollama.py) if Ollama isn't on
# localhost.
EMBED_MODEL_NAME: str = "embeddinggemma:300m"
EMBED_DIM: int = 768
OLLAMA_BASE: str = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/") + "/v1"
OLLAMA_KEY: str = "ollama"  # Ollama ignores the key but the OpenAI client requires one

# Retrieval / acceptance thresholds. Picked from observed scores against this
# JSONL + nomic embeddings (genuine matches >= ~0.66, unrelated <= ~0.47); the
# margin is checked between distinct qa_ids, not raw nodes, because multiple
# question alternates share the same qa_id and would otherwise look ambiguous.
TOP_K: int = 5
MIN_SCORE: float = 0.60
MIN_MARGIN: float = 0.05

REBUILD_ENV: str = "QUERY_AGENT_REBUILD_KB"

_lock = threading.Lock()
_vs: PGVectorStore | None = None
_embed: OpenAIEmbedding | None = None
_populated: bool = False
# In-memory registry built from the JSONL: qa_id -> entry, and normalized
# question -> qa_id (the exact-alias table).
_entries_by_id: dict[str, dict[str, Any]] = {}
_alias_table: dict[str, str] = {}


@dataclass
class Match:
    qa_id: str
    method: str                       # "exact" or "semantic"
    score: float
    matched_question: str | None = None
    second_qa_id: str | None = None
    second_score: float | None = None


# Similarity scores are 0.0–1.0 floats. For compact display and LLM prompts we
# rescale to an integer 0–1000 (a relevance "permille") so every value drops the
# wasteful leading "0." — e.g. 0.554 -> 554. Telemetry keeps the raw float.
SCORE_SCALE: int = 1000


def score_permille(score: float | None) -> int | None:
    """Rescale a 0.0–1.0 similarity score to an integer 0–1000 (None stays None)."""
    return None if score is None else round(score * SCORE_SCALE)


# --- Embedding / vector-store helpers ----------------------------------------


# Fail-fast tuning for the embedding client. The OpenAI client defaults
# (max_retries=10, timeout=60) mean a single query against a *down* Ollama
# hangs the agent for ~30s+ of exponential backoff before giving up, with no UI
# feedback. A short timeout + no retries surfaces the outage in ~1 connect
# attempt so handle() can post a graceful message. Embeddings here are a single
# short string, so a long timeout buys nothing even when the server is up.
EMBED_TIMEOUT: float = 10.0
EMBED_MAX_RETRIES: int = 0


def _embed_model() -> OpenAIEmbedding:
    global _embed
    if _embed is None:
        _embed = OpenAIEmbedding(
            model_name=EMBED_MODEL_NAME,
            api_base=OLLAMA_BASE,
            api_key=OLLAMA_KEY,
            timeout=EMBED_TIMEOUT,
            max_retries=EMBED_MAX_RETRIES,
        )
    return _embed


def _vector_store() -> PGVectorStore:
    global _vs
    if _vs is None:
        with _lock:
            if _vs is None:
                url = sa.engine.url.make_url(
                    os.environ.get("DATABASE_URL", db.DEFAULT_DATABASE_URL)
                )
                _vs = PGVectorStore.from_params(
                    database=url.database,
                    host=url.host or "127.0.0.1",
                    port=str(url.port or 5432),
                    user=url.username or "",
                    password=url.password or "",
                    table_name=QA_TABLE_NAME,
                    embed_dim=EMBED_DIM,
                )
    return _vs


# --- JSONL load + in-memory registry -----------------------------------------


def _normalize_query(s: str) -> str:
    """Lower-case, strip trailing ?!. and collapse whitespace. Used for exact
    alias matching so trivial variations don't need a separate embedding pass."""
    return " ".join(s.lower().strip().rstrip("?!.").split())


def _overlay_path() -> Path | None:
    """The operator overlay file, resolved from the customize.dir setting
    (DB → RAINBOX_CUSTOMIZE_DIR → unset): <dir>/question_answer.jsonl, or
    None when the setting is empty. PII and instance-specific persona
    entries live THERE, not in the base file — the base stays publishable.
    A nonexistent path degrades to 'no overlay' at load time (the caller
    checks existence), never a crash. Requires an app context.

    Raises RuntimeError if called outside an app context."""
    value = db.get_setting("customize.dir")
    if not value:
        return None
    return Path(str(value)) / "question_answer.jsonl"


def _load_jsonl() -> list[dict[str, Any]]:
    """Base entries merged with the operator overlay (see _overlay_path),
    keyed by id — an overlay entry with the same id replaces the base entry
    wholesale (base order is kept; overlay-only entries append). Each entry is
    tagged with `_source` ("upstream" for the base data/ file, "user-overlay"
    for the customize overlay) so retrieval can tier by provenance. Id-less
    entries are dropped here."""
    overlay = _overlay_path()
    sources: list[tuple[Path, str]] = [(QA_JSONL_PATH, "upstream")]
    if overlay is not None and overlay.exists():
        sources.append((overlay, "user-overlay"))
    merged: dict[str, dict[str, Any]] = {}
    for path, source in sources:
        for lineno, raw in enumerate(path.read_text().splitlines(), 1):
            line = raw.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                # Name the file + 1-based line + column so the operator can fix
                # the offending entry. The /settings repopulate result shows this
                # str(); the endpoint also logs it.
                raise ValueError(
                    f"{path}:{lineno}: invalid JSON — {exc.msg} "
                    f"(column {exc.colno})"
                ) from exc
            if entry.get("id"):
                entry["_source"] = source
                merged[entry["id"]] = entry
    return list(merged.values())


def _load_kb() -> None:
    """Build the in-memory registry (qa_id → entry; normalized-question → qa_id)
    from the JSONL. Cheap, runs once per process."""
    global _entries_by_id, _alias_table
    if _entries_by_id and _alias_table:
        return
    with _lock:
        if _entries_by_id and _alias_table:
            return
        entries = _load_jsonl()
        _entries_by_id = {e["id"]: e for e in entries if e.get("id")}
        _alias_table = {
            _normalize_query(q): e["id"]
            for e in entries
            for q in (e.get("questions") or [])
            if e.get("id")
        }


def get_entry(qa_id: str) -> dict[str, Any] | None:
    """Look up a JSONL entry by qa_id from the in-memory registry. Callers must
    have triggered `_load_kb()` (or the agent's handle, which does)."""
    return _entries_by_id.get(qa_id)


def _build_documents(entries: list[dict[str, Any]]) -> list[Document]:
    docs: list[Document] = []
    for e in entries:
        kind = e.get("kind", "static")
        for q in e.get("questions") or []:
            md: dict[str, Any] = {"qa_id": e.get("id", ""), "kind": kind, "question": q}
            if kind == "static":
                md["answer"] = e.get("answer", "")
            elif kind == "dynamic":
                md["handler"] = e.get("handler", "")
            docs.append(Document(text=q, metadata=md))
    return docs


def _table_row_count() -> int:
    """Row count of `data_seed_memory`, 0 if the table doesn't exist yet."""
    try:
        with psycopg.connect(db.psycopg_dsn(), autocommit=True) as c, c.cursor() as cur:
            cur.execute(
                sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(QA_FULL_TABLE))
            )
            row = cur.fetchone()
            return int(row[0]) if row else 0
    except Exception:
        return 0


def _truncate_table() -> None:
    """Empty the pgvector table before a rebuild. No-op if it doesn't exist yet —
    PGVectorStore creates it on the first insert, so there's nothing to clear."""
    with psycopg.connect(db.psycopg_dsn(), autocommit=True) as c, c.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (QA_FULL_TABLE,))
        row = cur.fetchone()
        if row is None or row[0] is None:
            return
        cur.execute(sql.SQL("TRUNCATE {}").format(sql.Identifier(QA_FULL_TABLE)))


def _ensure_populated(vs: PGVectorStore) -> None:
    """Embed and insert the JSONL knowledge base into the pgvector table if it's
    empty. Skipped per process after the first successful run. Setting
    QUERY_AGENT_REBUILD_KB=1 forces a TRUNCATE + repopulate on the first call,
    so an operator can pick up JSONL edits without writing SQL."""
    global _populated
    with _lock:
        if _populated:
            return
        if os.environ.get(REBUILD_ENV) == "1":
            logger.info("%s=1 set; truncating %s and repopulating", REBUILD_ENV, QA_FULL_TABLE)
            try:
                _truncate_table()
            except Exception as e:
                logger.warning("truncate %s failed (%s); falling through to populate", QA_FULL_TABLE, e)
        count = _table_row_count()
        if count > 0:
            logger.info("seed memory kb already populated (%d rows); skipping", count)
            _populated = True
            return
        entries = _load_jsonl()
        docs = _build_documents(entries)
        storage = StorageContext.from_defaults(vector_store=vs)
        VectorStoreIndex.from_documents(
            docs, storage_context=storage, embed_model=_embed_model()
        )
        logger.info(
            "seed memory kb populated with %d question alternates from %s",
            len(docs),
            QA_JSONL_PATH,
        )
        _populated = True


def rebuild_kb() -> dict[str, int]:
    """Reset the in-process registry caches, TRUNCATE data_seed_memory,
    and eagerly re-embed the merged JSONL (the /settings 'Repopulate Q&A
    memory' button; same semantics as QUERY_AGENT_REBUILD_KB=1 but without
    a restart). Returns {"entries": N, "documents": M} (M = embedded
    question variants). Raises on embedding failure (e.g. Ollama down) —
    the table may then be empty or partially populated (PGVectorStore inserts
    row-by-row, no wrapping transaction), but _populated stays False, so the
    next successful call truncates and repopulates from scratch.

    Lock order matters: _vector_store() and _load_kb() both take _lock,
    which is non-reentrant — so the store is resolved BEFORE the locked
    section and the registry is rebuilt AFTER it."""
    global _populated, _entries_by_id, _alias_table
    vs = _vector_store()
    with _lock:
        _populated = False
        _entries_by_id = {}
        _alias_table = {}
        _truncate_table()
        entries = _load_jsonl()
        docs = _build_documents(entries)
        storage = StorageContext.from_defaults(vector_store=vs)
        VectorStoreIndex.from_documents(
            docs, storage_context=storage, embed_model=_embed_model()
        )
        _populated = True
        logger.info("rebuild_kb: re-embedded %d entries (%d question variants)",
                    len(entries), len(docs))
    # While the lock was held, _alias_table/_entries_by_id were empty: concurrent
    # readers see a cache miss (semantic fallback), not corruption — do NOT "fix"
    # this with a lock here (deadlock; _load_kb takes _lock).
    _load_kb()
    return {"entries": len(entries), "documents": len(docs)}


# --- Matching -----------------------------------------------------------------


def _exact_match(query: str) -> Match | None:
    norm = _normalize_query(query)
    qa_id = _alias_table.get(norm)
    if qa_id is None:
        return None
    return Match(qa_id=qa_id, method="exact", score=1.0, matched_question=norm)


def _semantic_ranked(query: str, vs: PGVectorStore) -> list[Match]:
    """Top-K retrieve, aggregate by qa_id (max score per qa_id), return them
    ranked descending by score. **No** MIN_SCORE/MIN_MARGIN gating — for the
    caller to apply (QueryAgent gates, QueryRouterAgent uses raw top-1 as a
    hint for the LLM)."""
    index = VectorStoreIndex.from_vector_store(vs, embed_model=_embed_model())
    nodes = index.as_retriever(similarity_top_k=TOP_K).retrieve(query)
    if not nodes:
        return []
    by_qa: dict[str, tuple[float, str]] = {}   # qa_id -> (best_score, matched_question)
    for n in nodes:
        md = n.metadata or {}
        qa_id = md.get("qa_id") or ""
        if not qa_id:
            continue
        score = float(n.score) if n.score is not None else 0.0
        cur = by_qa.get(qa_id)
        if cur is None or score > cur[0]:
            by_qa[qa_id] = (score, md.get("question") or "")
    ranked = sorted(by_qa.items(), key=lambda kv: kv[1][0], reverse=True)
    return [
        Match(qa_id=qa, method="semantic", score=s, matched_question=q)
        for qa, (s, q) in ranked
    ]


def _semantic_match(query: str, vs: PGVectorStore) -> Match | None:
    """Gated semantic match: require best score >= MIN_SCORE and best -
    second >= MIN_MARGIN (between distinct qa_ids). Returns None when the best
    is too weak or too ambiguous — a clean "no" beats a confident wrong answer."""
    candidates = _semantic_ranked(query, vs)
    if not candidates:
        return None
    best = candidates[0]
    if best.score < MIN_SCORE:
        return None
    second_qa: str | None = None
    second_score: float | None = None
    if len(candidates) > 1:
        second = candidates[1]
        if best.score - second.score < MIN_MARGIN:
            return None
        second_qa, second_score = second.qa_id, second.score
    return Match(
        qa_id=best.qa_id,
        method="semantic",
        score=best.score,
        matched_question=best.matched_question,
        second_qa_id=second_qa,
        second_score=second_score,
    )


def _resolve_match(match: Match, ctx: QueryContext) -> str:
    entry = _entries_by_id.get(match.qa_id)
    if entry is None:
        return f"(unknown qa_id: {match.qa_id})"
    kind = entry.get("kind")
    if kind == "static":
        return entry.get("answer") or "(no answer)"
    if kind == "dynamic":
        handler_name = entry.get("handler") or ""
        fn = HANDLERS.get(handler_name)
        if fn is None:
            return f"(no handler implementation: {handler_name!r})"
        try:
            return fn(ctx)
        except Exception as e:
            return f"(handler {handler_name!r} raised {type(e).__name__}: {e})"
    return "(unknown kind in match)"


# --- Payload helpers (lifted from agent classes) ------------------------------


def room_uuid_from_payload(payload: dict[str, Any]) -> UUID:
    """Extract the chatroom uuid from an agent payload. Lifted from
    the `_room_uuid` static methods that previously existed (identically)
    on both `QueryAgent` and `QueryFilterRouterAgent`."""
    raw = payload.get("room_uuid")
    if not raw:
        raise ValueError("query agent payload missing 'room_uuid'")
    return raw if isinstance(raw, UUID) else UUID(str(raw))


def command_from_payload(room_uuid: UUID, payload: dict[str, Any]) -> str | None:
    """Extract the most recent human-typed query from a payload, or
    None if none is present. Lifted from the `_command_from_payload`
    static methods that previously existed (identically) on both
    `QueryAgent` and `QueryFilterRouterAgent`."""
    msgs = db.list_room_messages(room_uuid)
    msg_uuid = payload.get("message_uuid")
    if msg_uuid:
        for m in msgs:
            if m.get("uuid") == str(msg_uuid) and m.get("sender_type") == "human":
                return (m.get("text") or "").strip()
        return None
    for m in reversed(msgs):
        if m.get("sender_type") == "human":
            return (m.get("text") or "").strip()
    return None


@dataclass
class SeedMemory:
    """A curated Q&A entry surfaced as a memory. `uuid` is the jsonl `id`."""
    uuid: str
    path: str
    source: str   # "user-overlay" | "upstream"
    answer: str
    score: float


def retrieve_seed_memories(
    query: str, *, limit: int = 5,
    _ranker: Callable[[str], list[Match]] | None = None,
) -> list[SeedMemory]:
    """Curated static Q&A entries relevant to `query`, as memories. Ranked by the
    seed store's question-embedding similarity (>= MIN_SCORE), deduped by uuid
    (the ranker aggregates per qa_id), capped at `limit`. Dynamic/handler entries
    are excluded — they are computed answers, not facts. `_ranker` is injected by
    tests; in production it runs the LlamaIndex semantic ranker."""
    rank = _ranker or (lambda q: _semantic_ranked(q, _vector_store()))
    out: list[SeedMemory] = []
    for m in rank(query):
        if m.score < MIN_SCORE:
            continue
        entry = _entries_by_id.get(m.qa_id)
        if entry is None or entry.get("kind") != "static":
            continue
        out.append(SeedMemory(
            uuid=m.qa_id,
            path=str(entry.get("path", "")),
            source=str(entry.get("_source", "upstream")),
            answer=str(entry.get("answer", "")),
            score=m.score,
        ))
        if len(out) >= limit:
            break
    return out
