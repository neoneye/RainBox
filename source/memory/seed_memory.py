"""Shared KB / vector-store / match plumbing used by `agents/query.py` and
`agents/query_filter_router.py`. Extracted to avoid `agents/query_filter_router`
importing underscore-prefixed names across module boundaries (the prior
pattern).

The names retain their leading underscores from the original `agents/query.py`
because the existing call sites already use them with that convention. Both
caller modules import them explicitly — that's the public API of this module.
"""

import hashlib
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
from llama_index.core.schema import BaseNode, TextNode
from llama_index.core.storage.storage_context import StorageContext
from llama_index.core.vector_stores import (
    FilterCondition,
    FilterOperator,
    MetadataFilter,
    MetadataFilters,
)
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
# Bump when the node-metadata shape written by _build_documents changes
# (new/renamed keys). Folded into KB_EPOCH, so every stored row goes stale and
# re-embeds on the next sync — no manual full rebuild after an upgrade.
KB_SCHEMA_VERSION: int = 1
# Stored verbatim in every node's metadata next to row_sha256. A mismatch
# (embed-model swap or schema bump) marks the row dirty for sync_kb().
KB_EPOCH: str = f"{EMBED_MODEL_NAME}|{KB_SCHEMA_VERSION}"
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
# (mtime_ns, size) per source file at the last successful sync — the cheap
# has-anything-moved guard in _ensure_populated.
_sync_snapshot: dict[str, tuple[int, int]] | None = None
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
        # Track ids and paths seen *within this file* (value → first 1-based
        # line). Duplicates within one file are an operator mistake: a repeated
        # id silently overwrites the earlier entry in `merged` (dropping its
        # answer), and a repeated path means two entries claim the same logical
        # slot. Refuse either. (Cross-file reuse is intentional: an overlay entry
        # with a base entry's id/path overrides it — see this function's
        # docstring — so the trackers reset per file, not across `sources`.)
        seen_ids: dict[str, int] = {}
        seen_paths: dict[str, int] = {}
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
            # A shield is a name matched against the qa.unlocked_shields string
            # list; a non-string value can never be unlocked and is a data error.
            # Reject it here so repopulate fails hard with a file:line, rather
            # than silently embedding an entry the operator can never reveal.
            shield = entry.get("shield")
            if shield is not None and not isinstance(shield, str):
                raise ValueError(
                    f"{path}:{lineno}: 'shield' must be a string "
                    f"(a name matched against qa.unlocked_shields), got "
                    f"{type(shield).__name__}"
                )
            entry_id = entry.get("id")
            if entry_id:
                first = seen_ids.get(entry_id)
                if first is not None:
                    raise ValueError(
                        f"{path}:{lineno}: duplicate id {entry_id!r} "
                        f"(first seen at line {first}) — ids must be unique "
                        f"within a file"
                    )
                seen_ids[entry_id] = lineno
                entry_path = entry.get("path")
                if entry_path:
                    first_path = seen_paths.get(entry_path)
                    if first_path is not None:
                        raise ValueError(
                            f"{path}:{lineno}: duplicate path {entry_path!r} "
                            f"(first seen at line {first_path}) — paths must be "
                            f"unique within a file"
                        )
                    seen_paths[entry_path] = lineno
                entry["_source"] = source
                # Dirty detector for sync_kb(): the whole raw line (stripped),
                # so ANY edit — answer, questions, shield, path, kind — changes
                # the hash, with zero schema knowledge here. Overlay overrides
                # replace the base entry wholesale, so the winning file's line
                # is the one hashed.
                entry["_row_sha256"] = hashlib.sha256(line.encode("utf-8")).hexdigest()
                merged[entry_id] = entry
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


def available_qa_shields() -> list[str]:
    """Sorted, distinct shield names present in the loaded registry (base +
    overlay). Drives the Settings checklist. Loads the KB first so it is
    correct before any retrieval has run."""
    _load_kb()
    shields = {
        s for e in _entries_by_id.values()
        if (s := e.get("shield")) and isinstance(s, str)
    }
    return sorted(shields)


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
            shield = e.get("shield")
            if shield:
                md["shield"] = shield
            # The sync stamp: a node whose (row_sha256, kb_epoch) doesn't match
            # the current source line + epoch is stale — see sync_kb().
            md["row_sha256"] = e.get("_row_sha256", "")
            md["kb_epoch"] = KB_EPOCH
            # Embed the QUESTION alone. LlamaIndex otherwise folds every metadata
            # value into the embedded text (MetadataMode.EMBED) and, before
            # embedding, guards the metadata length against the chunk size — a
            # long `answer` in metadata then both pollutes the question-only
            # vector and trips "Metadata length (N) is longer than chunk size".
            # Excluding all keys keeps them retrievable from node.metadata while
            # the vector stays derived from `q` only.
            keys = list(md.keys())
            docs.append(Document(
                text=q,
                metadata=md,
                excluded_embed_metadata_keys=keys,
                excluded_llm_metadata_keys=keys,
            ))
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


# --- Incremental sync ----------------------------------------------------------

# Metadata keys PGVectorStore's node serialization owns; the metadata-only
# update preserves them verbatim and replaces every other key with the row's
# fresh metadata.
_NODE_BOOKKEEPING_KEYS: tuple[str, ...] = (
    "_node_content", "_node_type", "document_id", "doc_id", "ref_doc_id",
)


def _embed_texts(texts: list[str]) -> list[list[float]]:
    """Batch-embed question strings. Module-level so tests can monkeypatch a
    fake embedder under both sync_kb() and _ensure_populated()."""
    return _embed_model().get_text_embedding_batch(texts)


def _table_exists(cur: Any) -> bool:
    cur.execute("SELECT to_regclass(%s)", (QA_FULL_TABLE,))
    row = cur.fetchone()
    return row is not None and row[0] is not None


def _table_stamps() -> dict[str, str | None]:
    """qa_id -> "row_sha256|kb_epoch" for every row in the vector table. A
    qa_id whose nodes disagree (a past partial write) maps to None so the
    differ treats it as dirty. {} when the table doesn't exist yet."""
    out: dict[str, str | None] = {}
    with psycopg.connect(db.psycopg_dsn(), autocommit=True) as c, c.cursor() as cur:
        if not _table_exists(cur):
            return out
        cur.execute(sql.SQL(
            "SELECT DISTINCT metadata_->>'qa_id', metadata_->>'row_sha256', "
            "metadata_->>'kb_epoch' FROM {}"
        ).format(sql.Identifier(QA_FULL_TABLE)))
        for qa_id, row_sha, epoch in cur.fetchall():
            if not qa_id:
                continue
            stamp = f"{row_sha}|{epoch}"
            out[qa_id] = None if (qa_id in out and out[qa_id] != stamp) else stamp
    return out


def _row_nodes(qa_id: str) -> list[tuple[str, str, dict[str, Any], list[float] | None]]:
    """(node_id, question text, metadata dict, embedding vector) for every node
    of one row. The vector comes back in pgvector's text form ('[0.1,...]'),
    which is valid JSON."""
    with psycopg.connect(db.psycopg_dsn(), autocommit=True) as c, c.cursor() as cur:
        if not _table_exists(cur):
            return []
        cur.execute(sql.SQL(
            "SELECT node_id, text, metadata_, embedding::text FROM {} "
            "WHERE metadata_->>'qa_id' = %s"
        ).format(sql.Identifier(QA_FULL_TABLE)), (qa_id,))
        return [
            (node_id, text, meta if isinstance(meta, dict) else json.loads(meta),
             json.loads(emb) if emb else None)
            for node_id, text, meta, emb in cur.fetchall()
        ]


def _delete_nodes(node_ids: list[str]) -> None:
    if not node_ids:
        return
    with psycopg.connect(db.psycopg_dsn(), autocommit=True) as c, c.cursor() as cur:
        cur.execute(sql.SQL("DELETE FROM {} WHERE node_id = ANY(%s)")
                    .format(sql.Identifier(QA_FULL_TABLE)), (node_ids,))


def _delete_qa_rows(qa_ids: list[str]) -> None:
    if not qa_ids:
        return
    with psycopg.connect(db.psycopg_dsn(), autocommit=True) as c, c.cursor() as cur:
        if not _table_exists(cur):
            return
        cur.execute(sql.SQL("DELETE FROM {} WHERE metadata_->>'qa_id' = ANY(%s)")
                    .format(sql.Identifier(QA_FULL_TABLE)), (qa_ids,))


def _update_node_metadata(node_id: str, old_meta: dict[str, Any], md: dict[str, Any]) -> None:
    """Rewrite one node's stored metadata in place, keeping its vector: the
    top-level keys (what the shield SQL filter reads) and the copies nested in
    _node_content (what retrieval deserializes into the returned node)."""
    new_meta: dict[str, Any] = {
        k: old_meta[k] for k in _NODE_BOOKKEEPING_KEYS if k in old_meta
    }
    new_meta.update(md)
    content = json.loads(old_meta["_node_content"])
    keys = list(md.keys())
    content["metadata"] = md
    content["excluded_embed_metadata_keys"] = keys
    content["excluded_llm_metadata_keys"] = keys
    for rel in content.get("relationships", {}).values():
        if isinstance(rel, dict) and "metadata" in rel:
            rel["metadata"] = md
    new_meta["_node_content"] = json.dumps(content)
    with psycopg.connect(db.psycopg_dsn(), autocommit=True) as c, c.cursor() as cur:
        cur.execute(sql.SQL("UPDATE {} SET metadata_ = %s WHERE node_id = %s")
                    .format(sql.Identifier(QA_FULL_TABLE)),
                    (json.dumps(new_meta), node_id))


def _entry_stamp(e: dict[str, Any]) -> str:
    """The stamp an entry's nodes must carry to count as up to date."""
    return f"{e.get('_row_sha256', '')}|{KB_EPOCH}"


def _diff_rows(
    entries: list[dict[str, Any]], stamps: dict[str, str | None],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str], int]:
    """Classify merged JSONL entries against the table's stamps: (new entries,
    dirty entries, deleted qa_ids, unchanged count). A `None` stamp means the
    row's nodes disagree (partial write) — always dirty."""
    new: list[dict[str, Any]] = []
    dirty: list[dict[str, Any]] = []
    unchanged = 0
    ids: set[str] = set()
    for e in entries:
        qa_id = e.get("id")
        if not qa_id:
            continue
        ids.add(qa_id)
        if qa_id not in stamps:
            new.append(e)
        elif stamps[qa_id] != _entry_stamp(e):
            dirty.append(e)
        else:
            unchanged += 1
    deleted = [qa_id for qa_id in stamps if qa_id not in ids]
    return new, dirty, deleted, unchanged


def _sync_row(vs: PGVectorStore, entry: dict[str, Any]) -> tuple[int, bool]:
    """Reconcile one new/dirty row. Returns (embedded question count,
    metadata_only). The vector derives from the question text alone, so when
    the question multiset is unchanged the nodes' metadata is rewritten in
    place — zero embed calls. Otherwise new nodes are inserted BEFORE the old
    ones are deleted, so retrieval sees old-or-new, never an absent row;
    unchanged question strings keep their stored vectors."""
    docs = _build_documents([entry])
    old = _row_nodes(entry["id"])
    old_questions = sorted(q for _, q, _, _ in old)
    new_questions = sorted(d.text for d in docs)
    # Stored vectors are only reusable when they were produced under the
    # current epoch — an embed-model swap or schema bump invalidates them even
    # though the question text is identical.
    epoch_ok = all(meta.get("kb_epoch") == KB_EPOCH for _, _, meta, _ in old)
    if (old and epoch_ok and old_questions == new_questions
            and all(e is not None for *_, e in old)):
        by_q: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for node_id, q, meta, _ in old:
            by_q.setdefault(q, []).append((node_id, meta))
        for d in docs:
            node_id, meta = by_q[d.text].pop()
            _update_node_metadata(node_id, meta, dict(d.metadata))
        return 0, True
    vectors = {
        q: emb for _, q, meta, emb in old
        if emb is not None and meta.get("kb_epoch") == KB_EPOCH
    }
    need = sorted({d.text for d in docs} - set(vectors))
    if need:
        for q, vec in zip(need, _embed_texts(need), strict=True):
            vectors[q] = vec
    nodes: list[BaseNode] = [
        TextNode(
            text=d.text,
            metadata=dict(d.metadata),
            excluded_embed_metadata_keys=list(d.metadata.keys()),
            excluded_llm_metadata_keys=list(d.metadata.keys()),
            embedding=vectors[d.text],
        )
        for d in docs
    ]
    if nodes:
        vs.add(nodes)
    _delete_nodes([node_id for node_id, *_ in old])
    return len(need), False


def _sync_locked(vs: PGVectorStore) -> tuple[dict[str, int], bool]:
    """The reconcile body; assumes _lock is held. Returns (counts, whether the
    table already had stamped rows). Loader validation errors raise before any
    write — the table is left untouched, not emptied. A row that fails to
    embed is skipped (its old nodes stay; it remains dirty and retries next
    sync); after all rows are attempted, any failures raise one RuntimeError."""
    global _populated, _entries_by_id, _alias_table
    entries = _load_jsonl()
    stamps = _table_stamps()
    new, dirty, deleted, unchanged = _diff_rows(entries, stamps)
    counts = {"unchanged": unchanged, "updated": 0, "embedded": 0,
              "deleted": len(deleted)}
    _delete_qa_rows(deleted)
    errors: list[str] = []
    embedded_questions = 0
    for e in new + dirty:
        try:
            n_embedded, metadata_only = _sync_row(vs, e)
        except Exception as exc:  # noqa: BLE001 — isolate per row; report below
            errors.append(f"{e['id']}: {exc}")
            continue
        embedded_questions += n_embedded
        counts["updated" if metadata_only else "embedded"] += 1
    if counts["updated"] or counts["embedded"] or counts["deleted"]:
        # Invalidate the registry caches even on partial failure — whatever
        # succeeded is live and _load_kb() rebuilds from the same file.
        _entries_by_id = {}
        _alias_table = {}
        logger.info("sync_kb: %s (%d question embeds)", counts, embedded_questions)
    if errors:
        raise RuntimeError(
            f"sync_kb: {len(errors)} row(s) failed to embed "
            f"(they stay stale and retry next sync) — first: {errors[0]}"
        )
    _populated = True
    return counts, bool(stamps)


def _stamp_facts_if_changed(counts: dict[str, int]) -> None:
    """Post the one-time re-check-facts signal, but only when the sync actually
    changed something — a clean reconcile stays silent."""
    if not (counts["updated"] or counts["embedded"] or counts["deleted"]):
        return
    try:
        db.mark_facts_invalidated()
    except Exception:  # pragma: no cover — no app context; the sync succeeded
        logger.warning("sync_kb: could not stamp qa.facts_invalidated_at", exc_info=True)


def sync_kb() -> dict[str, int]:
    """Reconcile data_seed_memory with the merged JSONL instead of wiping it:
    unchanged rows are skipped, metadata-only edits (answer/shield/path/handler)
    update nodes in place with zero embed calls, question edits embed only the
    changed strings, and vanished ids are deleted. Returns row counts
    {"unchanged", "updated", "embedded", "deleted"}. Stamps
    qa.facts_invalidated_at only when something changed. Raises on loader
    validation errors (table untouched) and on embed failures (other rows
    intact; failed rows stay stale and retry on the next sync).

    Lock order matters: _vector_store() and _load_kb() both take _lock, which
    is non-reentrant — the store is resolved BEFORE the locked section and the
    registry is rebuilt AFTER it (same pattern as rebuild_kb)."""
    vs = _vector_store()
    with _lock:
        counts, _ = _sync_locked(vs)
    _load_kb()
    _stamp_facts_if_changed(counts)
    return counts


# --- Matching -----------------------------------------------------------------


def _unlocked_shields() -> set[str]:
    """Shields the operator has unlocked (the qa.unlocked_shields setting).
    Empty when unset or when called outside a Flask app context — the safe
    default, which keeps every shielded entry hidden."""
    try:
        val = db.get_setting("qa.unlocked_shields")
    except Exception:
        return set()
    return set(val) if isinstance(val, list) else set()


def _entry_locked(entry: dict[str, Any], unlocked: set[str]) -> bool:
    """True if `entry` is hidden from the LLM: it carries a shield that is not
    in `unlocked`. An entry with no shield is always visible. A malformed
    (non-string) shield is treated as locked — fail closed, never revealed."""
    shield = entry.get("shield")
    if not shield:
        return False
    if not isinstance(shield, str):
        return True
    return shield not in unlocked


def _drop_locked(matches: list[Match], unlocked: set[str]) -> list[Match]:
    """Layer-2 backstop: drop matches whose current in-memory entry is locked,
    order preserved. Pure over `_entries_by_id`, so unit-testable with no DB.
    Its real value is cross-process staleness (another process's `_entries_by_id`
    still carries an old shield after this process's pgvector table has been
    repopulated, or vice versa) and that a lock toggle in Settings takes effect
    on the next query with no repopulate needed."""
    return [
        m for m in matches
        if not _entry_locked(_entries_by_id.get(m.qa_id) or {}, unlocked)
    ]


def _shield_filters(unlocked: set[str]) -> MetadataFilters:
    """pgvector metadata filter that keeps only retrievable nodes: unshielded
    ones (no `shield` metadata key -> IS_EMPTY) plus any whose shield is
    unlocked. Locked shields are excluded in SQL, so they never occupy a top-K
    slot."""
    keep: list[MetadataFilter | MetadataFilters] = [
        MetadataFilter(key="shield", value=None, operator=FilterOperator.IS_EMPTY),
    ]
    if unlocked:
        keep.append(MetadataFilter(
            key="shield", value=sorted(unlocked), operator=FilterOperator.IN))
    return MetadataFilters(filters=keep, condition=FilterCondition.OR)


def _exact_match(query: str, *, unlocked_shields: set[str] | None = None) -> Match | None:
    norm = _normalize_query(query)
    qa_id = _alias_table.get(norm)
    if qa_id is None:
        return None
    unlocked = _unlocked_shields() if unlocked_shields is None else unlocked_shields
    if _entry_locked(_entries_by_id.get(qa_id) or {}, unlocked):
        return None
    return Match(qa_id=qa_id, method="exact", score=1.0, matched_question=norm)


def _semantic_ranked(query: str, vs: PGVectorStore, *,
                     unlocked_shields: set[str] | None = None) -> list[Match]:
    """Top-K retrieve, aggregate by qa_id (max score per qa_id), return them
    ranked descending by score. Locked shields are excluded at the vector query
    (so they never occupy a top-K slot) and again as an in-memory backstop.
    **No** MIN_SCORE/MIN_MARGIN gating — for the caller to apply."""
    unlocked = _unlocked_shields() if unlocked_shields is None else unlocked_shields
    index = VectorStoreIndex.from_vector_store(vs, embed_model=_embed_model())
    nodes = index.as_retriever(
        similarity_top_k=TOP_K, filters=_shield_filters(unlocked),
    ).retrieve(query)
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
    matches = [
        Match(qa_id=qa, method="semantic", score=s, matched_question=q)
        for qa, (s, q) in ranked
    ]
    return _drop_locked(matches, unlocked)


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
    """A curated Q&A entry surfaced as a memory. `uuid` is the jsonl `id`.
    `answer` holds the static answer text, or a dynamic handler's resolved
    output. `kind` is "static" or "dynamic"."""
    uuid: str
    path: str
    source: str   # "user-overlay" | "upstream"
    answer: str
    score: float
    kind: str = "static"


def retrieve_seed_memories(
    query: str, *, limit: int = 5,
    _ranker: Callable[[str], list[Match]] | None = None,
    unlocked_shields: set[str] | None = None,
) -> list[SeedMemory]:
    """Curated static Q&A entries relevant to `query`, as memories. Ranked by the
    seed store's question-embedding similarity (>= MIN_SCORE), deduped by uuid,
    capped at `limit`. Dynamic/handler entries and locked-shield entries are
    excluded. `_ranker` is injected by tests; in production it runs the
    semantic ranker (which itself applies the shield filter)."""
    unlocked = _unlocked_shields() if unlocked_shields is None else unlocked_shields
    rank = _ranker or (lambda q: _semantic_ranked(q, _vector_store(),
                                                   unlocked_shields=unlocked))
    out: list[SeedMemory] = []
    for m in rank(query):
        if m.score < MIN_SCORE:
            continue
        entry = _entries_by_id.get(m.qa_id)
        if entry is None or entry.get("kind") != "static":
            continue
        if _entry_locked(entry, unlocked):
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


def retrieve_seed_answers(
    query: str, *, qctx: QueryContext, limit: int = 5,
    _ranker: Callable[[str], list[Match]] | None = None,
    unlocked_shields: set[str] | None = None,
) -> list[SeedMemory]:
    """Top-N curated Q&A entries (static AND dynamic) relevant to `query`, as
    SeedMemory. Static entries carry their answer text; dynamic entries carry
    their handler's resolved output (handlers are read-only, resolved via
    `_resolve_match`). Ranked by question-embedding similarity (>= MIN_SCORE, no
    margin gate), capped at `limit`; the ranker aggregates by qa_id, so entries
    are unique per qa_id. Locked-shield entries are excluded. `_ranker` is
    injected by tests; in production it runs the semantic ranker (which itself
    applies the shield filter).

    Unlike `retrieve_seed_memories` (static-only, for the always-on chat block),
    this resolves dynamic handlers on demand for the assistant's `query_memory`
    action."""
    unlocked = _unlocked_shields() if unlocked_shields is None else unlocked_shields
    rank = _ranker or (lambda q: _semantic_ranked(q, _vector_store(),
                                                   unlocked_shields=unlocked))
    out: list[SeedMemory] = []
    for m in rank(query):
        if m.score < MIN_SCORE:
            continue
        entry = _entries_by_id.get(m.qa_id)
        if entry is None or _entry_locked(entry, unlocked):
            continue
        kind = str(entry.get("kind", "static"))
        answer = (str(entry.get("answer", "")) if kind == "static"
                  else _resolve_match(m, qctx))
        out.append(SeedMemory(
            uuid=m.qa_id,
            path=str(entry.get("path", "")),
            source=str(entry.get("_source", "upstream")),
            answer=answer,
            score=m.score,
            kind=kind,
        ))
        if len(out) >= limit:
            break
    return out
