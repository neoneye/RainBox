"""Diary passage vectors (proposal §7, "Embeddings and HNSW").

A vector is keyed by (source, epoch, text_hash). The epoch names everything
that shapes it: model name and digest, dimension and input format. The
embedding endpoint must be loopback; its digest is pinned when a job starts
and checked again by the query adapter, so a silently re-pulled model
disables the vector route instead of mixing vector spaces.

Input format 1 is the bare passage text (how the seed and claim paths embed
today). Format 2 is EmbeddingGemma's task prompts. The pilot measures both.
"""

from __future__ import annotations

import ipaddress
import json
import math
import os
import time as _time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol
from urllib.parse import urlparse
from uuid import UUID

import sqlalchemy as sa

import db

DEFAULT_MODEL = "embeddinggemma:300m"
EMBED_DIM = 768
BATCH_SIZE = 32
QUERY_TIMEOUT_S = 2.0
BATCH_TIMEOUT_S = 30.0
BATCH_RETRIES = 2
INPUT_FORMATS = (1, 2)
HNSW_EF_SEARCH = 100
VECTOR_TOP = 20


class EmbeddingError(RuntimeError):
    pass


def is_loopback_url(url: str) -> bool:
    host = urlparse(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def default_base_url() -> str:
    return os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")


def epoch_of(spec: dict[str, Any]) -> str:
    return f"{spec['model']}@{spec['digest'][:12]}|{spec['dim']}|f{spec['input_format']}"


def format_document(text: str, input_format: int) -> str:
    return text if input_format == 1 else f"title: none | text: {text}"


def format_query(text: str, input_format: int) -> str:
    return text if input_format == 1 else f"task: search result | query: {text}"


def valid_vector(vec: Any, dim: int = EMBED_DIM) -> bool:
    if not isinstance(vec, (list, tuple)) or len(vec) != dim:
        return False
    if not all(isinstance(x, (int, float)) and math.isfinite(x) for x in vec):
        return False
    return any(x != 0 for x in vec)


class Embedder(Protocol):
    def digest(self) -> str: ...
    def embed(self, texts: list[str], *, timeout: float) -> list[list[float]]: ...


@dataclass
class OllamaEmbedder:
    """Ollama's /api/embed and /api/tags over a loopback base URL."""

    base_url: str
    model: str = DEFAULT_MODEL

    def __post_init__(self) -> None:
        if not is_loopback_url(self.base_url):
            raise EmbeddingError(f"embedding endpoint must be loopback, not {self.base_url!r}")

    def _request(self, path: str, body: dict | None, timeout: float) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:   # noqa: S310 — loopback only
            return json.loads(resp.read())

    def digest(self) -> str:
        tags = self._request("/api/tags", None, QUERY_TIMEOUT_S)
        for m in tags.get("models", []):
            if m.get("name") == self.model or m.get("model") == self.model:
                return m["digest"]
        raise EmbeddingError(f"model {self.model!r} is not served at {self.base_url}")

    def embed(self, texts: list[str], *, timeout: float) -> list[list[float]]:
        out = self._request("/api/embed", {"model": self.model, "input": texts}, timeout)
        vecs = out.get("embeddings")
        if not isinstance(vecs, list) or len(vecs) != len(texts):
            raise EmbeddingError("embedding response has the wrong shape")
        return vecs


def capture_spec(embedder: Embedder, *, base_url: str, model: str, input_format: int) -> dict[str, Any]:
    if input_format not in INPUT_FORMATS:
        raise EmbeddingError(f"input format must be one of {INPUT_FORMATS}")
    return {"base_url": base_url, "model": model, "digest": embedder.digest(),
            "dim": EMBED_DIM, "input_format": input_format}


# --- backfill ---------------------------------------------------------------------------


@dataclass
class EmbedReport:
    source_uuid: str
    epoch: str
    missing: int = 0
    embedded: int = 0
    batches: int = 0
    failed_batches: int = 0
    rejected_vectors: int = 0
    swept: int = 0
    aborted: str | None = None
    seconds: float = 0.0
    failures: list[str] = field(default_factory=list)


def _current_hashes(source_uuid: UUID) -> list[tuple[str, str]]:
    """(text_hash, text) of every distinct current, non-excluded passage."""
    rows = db.session.execute(sa.text(
        "SELECT DISTINCT ON (p.text_hash) p.text_hash, p.text FROM diary_passage p "
        "JOIN diary_entry e ON e.uuid = p.entry_uuid "
        "JOIN diary_file f ON f.current_generation_uuid = e.generation_uuid "
        "WHERE f.source_uuid = :s AND f.availability = 'ready' "
        "AND NOT EXISTS (SELECT 1 FROM diary_exclusion x WHERE x.source_uuid = f.source_uuid "
        "AND x.relative_path = f.relative_path) ORDER BY p.text_hash"), {"s": source_uuid}).all()
    return [(r[0], r[1]) for r in rows]


def embed_source(source_uuid: UUID, embedder: Embedder, spec: dict[str, Any]) -> EmbedReport:
    """Resume missing vectors for the source's current passages under
    `spec`'s epoch, then sweep vectors no current passage references. A
    changed spec switches vector mode off until the new epoch is complete.
    No lock or write transaction is held across a model call."""
    t0 = _time.monotonic()
    source = db.diary_get_source(source_uuid)
    if not is_loopback_url(spec["base_url"]):
        raise EmbeddingError("embedding endpoint must be loopback")
    epoch = epoch_of(spec)
    report = EmbedReport(str(source_uuid), epoch)
    if source.embedding_spec != spec:
        source.embedding_spec = spec
        source.vector_mode = "off"
        db.session.commit()
    policy = source.policy_version
    have = {r[0] for r in db.session.execute(sa.text(
        "SELECT text_hash FROM diary_embedding WHERE source_uuid = :s AND model_epoch = :e"),
        {"s": source_uuid, "e": epoch}).all()}
    todo = [(h, t) for h, t in _current_hashes(source_uuid) if h not in have]
    db.session.commit()
    report.missing = len(todo)
    for i in range(0, len(todo), BATCH_SIZE):
        batch = todo[i:i + BATCH_SIZE]
        inputs = [format_document(t, spec["input_format"]) for _, t in batch]
        vecs = None
        for attempt in range(BATCH_RETRIES + 1):
            try:
                vecs = embedder.embed(inputs, timeout=BATCH_TIMEOUT_S)
                break
            except Exception as exc:   # noqa: BLE001 — retried, then reported
                if attempt == BATCH_RETRIES:
                    report.failed_batches += 1
                    report.failures.append(type(exc).__name__)
        report.batches += 1
        if vecs is None:
            continue
        current = db.diary_get_source(source_uuid)
        if current.policy_version != policy or current.embedding_spec != spec:
            db.session.rollback()
            report.aborted = "policy_changed"
            break
        rows = []
        for (h, _), v in zip(batch, vecs):
            if not valid_vector(v):
                report.rejected_vectors += 1
                continue
            rows.append({"s": source_uuid, "e": epoch, "h": h, "v": str(list(map(float, v)))})
        for r in rows:
            db.session.execute(sa.text(
                "INSERT INTO diary_embedding (uuid, source_uuid, model_epoch, text_hash, embedding, created_at) "
                "VALUES (gen_random_uuid(), :s, :e, :h, CAST(:v AS vector), now()) "
                "ON CONFLICT (source_uuid, model_epoch, text_hash) DO NOTHING"), r)
        db.session.commit()
        report.embedded += len(rows)
    if report.aborted is None:
        report.swept = sweep_vectors(source_uuid, epoch)
    report.seconds = round(_time.monotonic() - t0, 3)
    return report


def sweep_vectors(source_uuid: UUID, epoch: str) -> int:
    """Delete this epoch's vectors no current passage references; and, once
    the epoch covers every current passage, every other epoch's vectors."""
    live = [h for h, _ in _current_hashes(source_uuid)]
    n = db.session.execute(sa.text(
        "DELETE FROM diary_embedding WHERE source_uuid = :s AND model_epoch = :e "
        "AND NOT (text_hash = ANY(:live))"), {"s": source_uuid, "e": epoch, "live": live}).rowcount
    covered = db.session.execute(sa.text(
        "SELECT count(*) FROM diary_embedding WHERE source_uuid = :s AND model_epoch = :e"),
        {"s": source_uuid, "e": epoch}).scalar()
    if covered == len(set(live)):
        n += db.session.execute(sa.text(
            "DELETE FROM diary_embedding WHERE source_uuid = :s AND model_epoch <> :e"),
            {"s": source_uuid, "e": epoch}).rowcount
    db.session.commit()
    return n or 0


def set_vector_mode(source_uuid: UUID, mode: str) -> Any:
    source = db.diary_get_source(source_uuid)
    if mode not in ("off", "exact", "hnsw"):
        raise db.DiaryError("mode must be off, exact or hnsw")
    if mode in ("exact", "hnsw") and not source.embedding_spec:
        raise db.DiaryError("vector mode needs a recorded embedding epoch; run embed first")
    if mode == "hnsw" and not (source.embedding_spec or {}).get("hnsw_passed"):
        raise db.DiaryError("hnsw needs a passing recall/latency report (probe --vectors hnsw)")
    source.vector_mode = mode
    db.session.commit()
    return source


# --- query side ---------------------------------------------------------------------------


class QueryEmbedder:
    """The request-path adapter: 2-second timeout, zero retries, digest
    checked against each source's stored spec, cache keyed by epoch+query."""

    def __init__(self, factory: Callable[[dict[str, Any]], Embedder] | None = None):
        self._factory = factory or (lambda spec: OllamaEmbedder(spec["base_url"], spec["model"]))
        self._digest_ok: dict[str, bool] = {}
        self._cache: dict[tuple[str, str], list[float]] = {}

    def __call__(self, text: str, spec: dict[str, Any]) -> list[float]:
        epoch = epoch_of(spec)
        key = (epoch, text)
        if key in self._cache:
            return self._cache[key]
        embedder = self._factory(spec)
        if epoch not in self._digest_ok:
            self._digest_ok[epoch] = embedder.digest() == spec["digest"]
        if not self._digest_ok[epoch]:
            raise EmbeddingError("served model digest differs from the recorded epoch")
        vec = embedder.embed([format_query(text, spec["input_format"])], timeout=QUERY_TIMEOUT_S)[0]
        if not valid_vector(vec, spec["dim"]):
            raise EmbeddingError("query vector rejected")
        if len(self._cache) > 256:
            self._cache.clear()
        self._cache[key] = vec
        return vec


def vector_route(req: Any, sources: list[Any], embed_query: Callable, params: dict[str, Any],
                 date_sql: str, *, mode_override: str | None = None) -> list[UUID]:
    """Top passages by cosine distance over eligible current passages joined
    to their vectors. Exact mode orders by an expression no index can serve;
    hnsw mode uses the index with iterative scans and fills any shortfall
    from the exact route."""
    from diary.retrieval import _ELIGIBLE

    by_epoch: dict[str, list[Any]] = {}
    for s in sources:
        if s.embedding_spec:
            by_epoch.setdefault(epoch_of(s.embedding_spec), []).append(s)
    scored: list[tuple[float, UUID]] = []
    for epoch, group in by_epoch.items():
        spec = group[0].embedding_spec
        qv = str(list(map(float, embed_query(req.query, spec))))
        gparams = {**params, "sources": [s.uuid for s in group], "epoch": epoch, "qv": qv}
        mode = mode_override or ("hnsw" if all(s.vector_mode == "hnsw" for s in group) else "exact")
        join = (" JOIN diary_embedding emb ON emb.source_uuid = s.uuid "
                "AND emb.model_epoch = :epoch AND emb.text_hash = p.text_hash ")
        base = _ELIGIBLE.replace("    WHERE s.uuid = ANY(:sources)", join + "    WHERE s.uuid = ANY(:sources)", 1)
        exact_sql = ("SELECT p.uuid, (emb.embedding <=> CAST(:qv AS vector)) + 0 AS dist "
                     + base + date_sql + f" ORDER BY dist, p.uuid LIMIT {VECTOR_TOP}")
        rows: list[Any]
        if mode == "hnsw":
            db.session.execute(sa.text(f"SET LOCAL hnsw.ef_search = {HNSW_EF_SEARCH}"))
            try:
                db.session.execute(sa.text("SET LOCAL hnsw.iterative_scan = strict_order"))
            except Exception:   # noqa: BLE001 — pgvector < 0.8: fill below still applies
                pass
            rows = list(db.session.execute(sa.text(
                "SELECT p.uuid, emb.embedding <=> CAST(:qv AS vector) AS dist " + base + date_sql
                + f" ORDER BY emb.embedding <=> CAST(:qv AS vector) LIMIT {VECTOR_TOP}"),
                gparams).all())
            eligible = db.session.execute(sa.text("SELECT count(*) " + base + date_sql), gparams).scalar()
            if len(rows) < min(VECTOR_TOP, eligible or 0):
                seen = {r[0] for r in rows}
                for r in db.session.execute(sa.text(exact_sql), gparams).all():
                    if r[0] not in seen:
                        rows.append(r)
        else:
            rows = list(db.session.execute(sa.text(exact_sql), gparams).all())
        scored += [(float(r[1]), r[0]) for r in rows]
    scored.sort()
    return [pid for _, pid in scored[:VECTOR_TOP]]


# --- indexes ----------------------------------------------------------------------------------


def build_hnsw_index() -> dict[str, Any]:
    db.session.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_diary_embedding_hnsw ON diary_embedding "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"))
    db.session.commit()
    version = db.session.execute(sa.text(
        "SELECT extversion FROM pg_extension WHERE extname = 'vector'")).scalar()
    return {"index": "ix_diary_embedding_hnsw", "pgvector": version}


TRGM_AUTO_BYTES = 25 * 1024 * 1024


def build_trgm_index() -> dict[str, Any]:
    """The literal route's optional accelerator. It cannot change a LIKE
    result, so it needs no recall gate."""
    db.session.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
    db.session.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_diary_entry_text_trgm ON diary_entry "
        "USING gin (text gin_trgm_ops)"))
    db.session.commit()
    return {"index": "ix_diary_entry_text_trgm"}


def maybe_build_trgm_index(source_uuid: UUID) -> bool:
    """Called at the end of sync: build the trigram index once a source's
    current entry text exceeds TRGM_AUTO_BYTES."""
    size = db.session.execute(sa.text(
        "SELECT coalesce(sum(octet_length(e.text)), 0) FROM diary_entry e "
        "JOIN diary_file f ON f.current_generation_uuid = e.generation_uuid WHERE f.source_uuid = :s"),
        {"s": source_uuid}).scalar()
    exists = db.session.execute(sa.text(
        "SELECT 1 FROM pg_indexes WHERE indexname = 'ix_diary_entry_text_trgm'")).first()
    db.session.commit()
    if size and size > TRGM_AUTO_BYTES and exists is None:
        build_trgm_index()
        return True
    return False
