"""The recall filter's cross-encoder backend: client + keep/drop policy.

memory_query scores everything it recalls before any of it reaches the
assistant's prompt. The default scorer is an LLM (`_filter_recalled_candidates`
in agents/assistant.py) which reads the whole turn and answers with Likert
scales — thorough, and around twenty seconds of the operator's turn. A
cross-encoder reranker answers the narrower question "how well does this
candidate match this message" in one pass per candidate, in milliseconds.

Which one runs is the `memory.recall_filter_backend` setting: `llm`, or
`reranker:<model key>` for one of RERANKER_MODELS. The models themselves live
in the `reranker/` sidecar service (its own venv, torch + transformers), which
this module talks to over HTTP — nothing here imports a model.

The two backends are NOT interchangeable in what they return. The LLM scores
three named scales and explains itself; the reranker returns one relevance
number per candidate and nothing else. So the keep/drop policy here is its own
(`apply_rerank_scores`), and a reranked candidate's trace row carries its raw
score rather than empty Likert columns.
"""

import ast
import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: The reranker models the sidecar serves, by the key used in the setting
#: value, mapped to the Hugging Face repo the sidecar loads. MUST stay in sync
#: with `MODELS` in reranker/server.py (a test asserts it — the two live in
#: different venvs, so there is no shared import to keep them honest).
RERANKER_MODELS: dict[str, str] = {
    "mmarco-mMiniLMv2-L12-H384-v1": "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1",
    "jina-reranker-v2-base-multilingual": "jinaai/jina-reranker-v2-base-multilingual",
}

#: The `memory.recall_filter_backend` value that keeps the LLM scorer.
BACKEND_LLM: str = "llm"
#: Prefix marking a reranker backend; what follows is a RERANKER_MODELS key.
RERANKER_PREFIX: str = "reranker:"

DEFAULT_RERANKER_URL: str = "http://127.0.0.1:5008"

# Generous because the sidecar's FIRST call for a model downloads it from the
# Hugging Face Hub (hundreds of MB) and loads it; every later call is
# sub-second. A timeout here surfaces as a filter failure, which falls back to
# gated retrieval — so it costs the turn's recall quality, not the turn.
RERANK_TIMEOUT_S: float = float(os.environ.get("RERANKER_TIMEOUT", "300"))


def backend_choices() -> list[str]:
    """Every valid `memory.recall_filter_backend` value, LLM first."""
    return [BACKEND_LLM] + [f"{RERANKER_PREFIX}{key}" for key in RERANKER_MODELS]


def reranker_model(backend: object) -> str | None:
    """The reranker model key a backend value selects, or None for the LLM.

    An unknown model key raises: silently falling back to the LLM would hide a
    typo behind the very latency the operator switched away from."""
    value = str(backend or BACKEND_LLM).strip()
    if not value.startswith(RERANKER_PREFIX):
        return None
    key = value[len(RERANKER_PREFIX):]
    if key not in RERANKER_MODELS:
        raise ValueError(
            f"unknown reranker model {key!r}; known: {sorted(RERANKER_MODELS)}"
        )
    return key


def service_url() -> str:
    """Base URL of the reranker sidecar (RERANKER_URL, else the default)."""
    return (os.environ.get("RERANKER_URL") or DEFAULT_RERANKER_URL).rstrip("/")


def document_text(row: dict[str, Any]) -> str:
    """The plain text a cross-encoder should read for one candidate row.

    The rows are built for the filter LLM's prompt, where every value is
    `repr()`-quoted so a fact cannot forge a prompt zone. A cross-encoder reads
    a pair of natural-language strings, not a prompt — quotes and escapes in
    the text are noise it would have to spend attention on, so they come back
    off here. Only the fields that carry meaning are included; the path, kind
    and retrieval score describe where a candidate came from, which is not what
    is being matched."""
    parts: list[str] = []
    for field in ("matched_question", "answer", "text"):
        value = row.get(field)
        if value in (None, ""):
            continue
        parts.append(_unquote(str(value)))
    return "\n".join(parts).strip() or str(row.get("path") or "")


def _unquote(value: str) -> str:
    """`repr()`'d text back to the text itself; anything else unchanged."""
    try:
        parsed = ast.literal_eval(value)
    except (ValueError, SyntaxError):
        return value
    return parsed if isinstance(parsed, str) else value


@dataclass(frozen=True)
class RerankResult:
    """One scoring pass, as the trace needs to read it back.

    `scores` is what the models answered. The rest is what the operator cannot
    reconstruct from the candidates alone: how long the service spent, and how
    long each pair actually was against the ceiling it was measured on — a
    pair over `max_length` was cut at the tokenizer, and a fact that scored low
    because half of it was dropped looks exactly like a fact that scored low.
    """

    scores: dict[str, float]
    service_ms: int = 0
    max_length: int = 0
    tokens: dict[str, int] = field(default_factory=dict)


def rerank(
    query: str, documents: list[dict[str, str]], *, model: str,
    url: str | None = None, timeout: float = RERANK_TIMEOUT_S,
) -> RerankResult:
    """Score `documents` (each `{"id", "text"}`) against `query` on the sidecar.

    `service_ms` is the sidecar's own measurement of the scoring pass, which is
    the number the operator is comparing against the LLM scorer's twenty
    seconds — this function's HTTP round trip is not in it. Any failure
    (service down, unknown model, bad response) raises; the caller falls back
    to gated retrieval."""
    import requests

    base = (url or service_url()).rstrip("/")
    response = requests.post(
        f"{base}/rerank",
        json={"model": model, "query": query, "documents": documents},
        timeout=timeout,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"reranker service {base} returned {response.status_code}: "
            f"{response.text[:200]}"
        )
    body = response.json()
    rows = body.get("scores") or []
    return RerankResult(
        scores={str(row["id"]): float(row["score"]) for row in rows},
        service_ms=int(body.get("ms") or 0),
        max_length=int(body.get("max_length") or 0),
        tokens={str(row["id"]): int(row["tokens"]) for row in rows
                if row.get("tokens") is not None},
    )


# The keep/drop policy over reranker scores. It is RELATIVE first, because the
# absolute scale is the one thing these models do not share: on the same
# candidate list the correct answer scored 0.21 on mmarco and 0.50 on jina,
# while jina's irrelevant candidates sat at 0.03 where mmarco's sat at 0.0001.
# A fixed threshold tuned on either model empties the list on the other.
#
# KEEP_RATIO: a candidate scoring at least this fraction of the best score is
# in the same league as the best one, whatever league that is.
# KEEP_TOP_N: the best candidates survive on rank alone, as in the LLM policy.
# NOISE_FLOOR: absolute, and the only absolute — a list where even the best
# candidate scores this low has nothing in it, and rank must not resurrect it.
RERANK_KEEP_RATIO: float = 0.5
RERANK_KEEP_TOP_N: int = 2
RERANK_NOISE_FLOOR: float = 0.05


def apply_rerank_scores(
    scores: dict[str, float], candidates: list, *, top_k: int,
) -> list:
    """Reranker scores → the same `ScoredCandidate` rows the LLM path produces.

    Policy, mirroring the LLM filter's shape (agents/query_filter_router.
    apply_filter_scores) with the constants above: fewer than `top_k`
    candidates is no competition, so all are kept; on a full list the top
    RERANK_KEEP_TOP_N by score are kept on rank, any candidate within
    RERANK_KEEP_RATIO of the best score is kept on merit, and nothing below
    RERANK_NOISE_FLOOR is kept at all.

    The three Likert fields stay 0: this backend did not score those scales,
    and writing its one number into three columns would put a reading in the
    trace that no model produced. The number it did produce rides along as
    `rerank_score`. A candidate the service omitted scores 0.0 and is dropped
    (kept only when the whole list is). Returns every candidate, best-first."""
    from agents.query_filter_router import ScoredCandidate

    keep_all = len(candidates) < top_k
    ranked = sorted(candidates, key=lambda c: -scores.get(c.qa_id, 0.0))
    best = max((scores.get(c.qa_id, 0.0) for c in candidates), default=0.0)
    scored = []
    for rank, cand in enumerate(ranked):
        score = scores.get(cand.qa_id, 0.0)
        kept = keep_all or (
            score >= RERANK_NOISE_FLOOR
            and (rank < RERANK_KEEP_TOP_N or score >= RERANK_KEEP_RATIO * best)
        )
        scored.append(ScoredCandidate(
            qa_id=cand.qa_id, direct=0, indirect=0, relevancy=0,
            kept=kept, rerank_score=score))
    return scored
