"""Standalone cross-encoder reranker REST service.

Same shape as the sibling sidecars (`voice_stt_whisper/`, `voice_tts_kokoro/`):
kept separate so its heavy dependency (torch + transformers) never enters the
main project's venv. The main app calls it over HTTP from the assistant's
recall filter (`agents/recall_reranker.py`) and never imports this code.

A cross-encoder reads the (query, document) pair in one pass and emits a single
relevance score. That is the whole point of running it here: the recall filter's
LLM alternative sends a full prompt (request, history, identity, every
candidate) and waits ~20s for a JSON answer, while these models score the same
candidate list in well under a second.

Run it from inside this directory (with the local venv active):
`python server.py` serves on port 5008.
`create_app(score_fn=...)` lets tests inject a fake scorer so the endpoints can
be exercised without torch installed.

API:
  GET  /health   -> {"status":"ok","models":{<key>:<repo>},"loaded":[<key>,...],
                     "device":str}
  POST /rerank   -> {"model":<key>,"query":str,"documents":[{"id","text"},...]}
                    => {"model","scores":[{"id","score"},...],"ms"}
                       (or {"error"} 4xx/5xx)
"""

import logging
import os
import threading
import time
from typing import Callable

from flask import Flask, Response, jsonify, request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# The models this service will serve, by the short key the main app sends.
# Adding one here is all it takes — the key is what the operator picks in the
# `memory.recall_filter_backend` setting (as "reranker:<key>"), so keep the key
# equal to the repo's model name.
MODELS: dict[str, str] = {
    # Multilingual MiniLM cross-encoder distilled on mMARCO. Small (L12/H384),
    # CPU-friendly, English-strong and usable across the mMARCO languages.
    "mmarco-mMiniLMv2-L12-H384-v1": "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1",
    # Jina's multilingual reranker v2. Bigger (XLM-RoBERTa base), stronger
    # cross-lingual behaviour, and ships its own scoring code — hence
    # trust_remote_code below.
    "jina-reranker-v2-base-multilingual": "jinaai/jina-reranker-v2-base-multilingual",
}

#: Repos whose scoring path lives in the repo itself rather than in
#: transformers. Loading one executes code downloaded from the Hub, so the set
#: is explicit rather than a blanket flag.
TRUST_REMOTE_CODE: frozenset[str] = frozenset({"jinaai/jina-reranker-v2-base-multilingual"})

# Pair length in tokens. 512 is the mMARCO cross-encoder's ceiling; the recall
# filter sends short candidates (a question + a capped answer), so nothing real
# gets truncated at this length.
MAX_LENGTH = int(os.environ.get("RERANKER_MAX_LENGTH", "512"))
BATCH_SIZE = int(os.environ.get("RERANKER_BATCH_SIZE", "16"))

# "auto" = Apple-Silicon MPS when torch offers it, else CPU. Override with
# RERANKER_DEVICE=cpu to compare, or when a model's remote code misbehaves on
# MPS.
DEVICE = os.environ.get("RERANKER_DEVICE", "auto")

# score signature: (model_key: str, query: str, texts: list[str]) -> list[float]
# Scores are relevance probabilities in 0..1 (higher = more relevant), one per
# text, in the order given.
ScoreFn = Callable[[str, str, list], list]


def create_app(score_fn: ScoreFn | None = None) -> Flask:
    """Build the Flask app. With `score_fn` None the real transformers models
    are loaded lazily on the first /rerank call for that model (so importing
    this module never requires torch), and each stays in memory afterwards."""
    app = Flask(__name__)
    state: dict[str, object] = {"score": score_fn, "loaded": []}
    # Neither the models nor the lazy load are thread-safe, and the assistant
    # can have two turns in flight, so serialize load + scoring.
    lock = threading.Lock()

    def get_score() -> ScoreFn:
        if state["score"] is None:
            state["score"] = _build_transformers_score(state["loaded"])  # type: ignore[arg-type]
        return state["score"]  # type: ignore[return-value]

    @app.route("/health")
    def health() -> Response:
        return jsonify({
            "status": "ok",
            "models": MODELS,
            "loaded": list(state["loaded"]),  # type: ignore[arg-type]
            "device": DEVICE,
        })

    @app.route("/rerank", methods=["POST"])
    def rerank() -> Response | tuple[Response, int]:
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "body must be a JSON object"}), 400
        model = str(body.get("model") or "").strip()
        if model not in MODELS:
            return jsonify({
                "error": f"unknown model {model!r}; known: {sorted(MODELS)}"
            }), 400
        query = str(body.get("query") or "")
        documents = body.get("documents")
        if not isinstance(documents, list):
            return jsonify({"error": "'documents' must be a list"}), 400
        for doc in documents:
            if not isinstance(doc, dict) or "id" not in doc:
                return jsonify({"error": "each document needs an 'id'"}), 400
        # An empty candidate list is a legitimate call (the caller found
        # nothing to score), and answering it without loading a model keeps a
        # cold service cheap for the case where there is no work.
        if not documents:
            return jsonify({"model": model, "scores": [], "ms": 0})

        texts = [str(d.get("text") or "") for d in documents]
        t0 = time.monotonic()
        try:
            with lock:
                scores = get_score()(model, query, texts)
        except Exception as e:  # pragma: no cover - real-model failure path
            logger.exception("rerank failed")
            return jsonify({"error": f"rerank failed: {e}"}), 500
        ms = int((time.monotonic() - t0) * 1000)
        return jsonify({
            "model": model,
            "ms": ms,
            "scores": [
                {"id": doc["id"], "score": float(score)}
                for doc, score in zip(documents, scores)
            ],
        })

    return app


def _resolve_device(torch) -> str:  # pragma: no cover - needs torch
    if DEVICE != "auto":
        return DEVICE
    if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def _build_transformers_score(loaded: list) -> ScoreFn:  # pragma: no cover - needs torch
    """Load models on demand and return a scoring function over them.

    Each model is loaded once, on its first request, and cached — the first
    call for a model also downloads it from the Hugging Face Hub, so it is slow
    in a way no later call is.
    """
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    device = _resolve_device(torch)
    cache: dict[str, tuple] = {}

    def load(key: str) -> tuple:
        if key in cache:
            return cache[key]
        repo = MODELS[key]
        trust = repo in TRUST_REMOTE_CODE
        logger.info("loading reranker %r (%s, device=%s) ...", key, repo, device)
        t0 = time.monotonic()
        tokenizer = AutoTokenizer.from_pretrained(repo, trust_remote_code=trust)
        model = AutoModelForSequenceClassification.from_pretrained(
            repo, trust_remote_code=trust, torch_dtype="auto")
        model.to(device)
        model.eval()
        cache[key] = (tokenizer, model)
        loaded.append(key)
        logger.info("loaded %r in %.1fs", key, time.monotonic() - t0)
        return cache[key]

    def score(key: str, query: str, texts: list) -> list:
        tokenizer, model = load(key)
        pairs = [(query, text) for text in texts]
        # A model that ships its own scoring code knows how to run itself
        # (jina's `compute_score` applies its own pooling and sigmoid); the
        # generic path below is for the plain cross-encoders.
        compute = getattr(model, "compute_score", None)
        if callable(compute):
            with torch.inference_mode():
                out = compute(pairs, max_length=MAX_LENGTH, batch_size=BATCH_SIZE)
            return [float(x) for x in (out if isinstance(out, list) else [out])]
        scores: list[float] = []
        for start in range(0, len(pairs), BATCH_SIZE):
            batch = pairs[start:start + BATCH_SIZE]
            encoded = tokenizer(
                [p[0] for p in batch], [p[1] for p in batch],
                padding=True, truncation=True, max_length=MAX_LENGTH,
                return_tensors="pt",
            ).to(device)
            with torch.inference_mode():
                logits = model(**encoded).logits
            # One logit per pair, which is a relevance score on an unbounded
            # scale; sigmoid puts every model's output on the same 0..1 scale
            # so the keep/drop thresholds in the main app mean one thing.
            scores.extend(torch.sigmoid(logits[:, 0]).float().cpu().tolist())
        return scores

    return score


if __name__ == "__main__":  # pragma: no cover
    create_app().run(host="127.0.0.1", port=5008)
