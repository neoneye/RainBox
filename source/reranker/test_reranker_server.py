"""Tests for the reranker sidecar's HTTP surface.

A fake scorer is injected, so these run in any venv with Flask — no torch, no
model download. They also run inside the main project's pytest sweep, which is
why the module loads `server.py` by path: three sidecars ship a module named
`server`, and only one of them can own that name in a shared run.
"""
import importlib.util
import pathlib

_spec = importlib.util.spec_from_file_location(
    "reranker_server", pathlib.Path(__file__).resolve().parent / "server.py")
server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server)

MODEL = "mmarco-mMiniLMv2-L12-H384-v1"


def _fake_score(model, query, texts):
    """Deterministic: a document containing the query scores high."""
    return {"scores": [0.99 if query in text else 0.01 for text in texts],
            "tokens": [len(text.split()) for text in texts]}


def _client(score=_fake_score):
    app = server.create_app(score_fn=score)
    app.config.update(TESTING=True)
    return app.test_client()


def _docs(*texts):
    return [{"id": f"d{i}", "text": t} for i, t in enumerate(texts)]


def test_health_lists_the_models_it_serves():
    body = _client().get("/health").get_json()
    assert body["status"] == "ok"
    assert set(body["models"]) == {
        "mmarco-mMiniLMv2-L12-H384-v1", "jina-reranker-v2-base-multilingual"}
    assert body["loaded"] == []   # an injected scorer loads nothing


def test_rerank_scores_every_document_in_order():
    resp = _client().post("/rerank", json={
        "model": MODEL, "query": "alma", "documents": _docs("alma is 10", "paris")})
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["model"] == MODEL
    assert body["scores"] == [{"id": "d0", "score": 0.99, "tokens": 3},
                              {"id": "d1", "score": 0.01, "tokens": 1}]
    assert body["ms"] >= 0


def test_rerank_reports_the_pair_length_and_the_ceiling_it_was_measured_on():
    """A pair longer than max_length is cut at the tokenizer, and a fact that
    scored low because half of it was dropped looks exactly like a fact that
    scored low. The caller can only tell the two apart from these numbers."""
    body = _client().post("/rerank", json={
        "model": MODEL, "query": "alma",
        "documents": _docs("alma is 10")}).get_json()
    assert body["max_length"] == server.MAX_LENGTH
    assert body["scores"][0]["tokens"] == 3


def test_a_scorer_that_counts_no_tokens_still_answers():
    """The token count is reported where it is known, not required."""
    body = _client(lambda *_a: {"scores": [0.5]}).post("/rerank", json={
        "model": MODEL, "query": "q", "documents": _docs("t")}).get_json()
    assert body["scores"] == [{"id": "d0", "score": 0.5, "tokens": None}]


def test_an_empty_document_list_is_answered_without_loading_a_model():
    """The caller found nothing to score. Loading a model to say so would put
    the whole download on a request that has no work in it."""
    def never(*_a):
        raise AssertionError("the scorer must not run")

    body = _client(never).post(
        "/rerank", json={"model": MODEL, "query": "q", "documents": []}).get_json()
    assert body["scores"] == []


def test_an_unknown_model_is_a_400_naming_the_known_ones():
    resp = _client().post("/rerank", json={
        "model": "gpt-9", "query": "q", "documents": _docs("t")})
    assert resp.status_code == 400
    assert "unknown model" in resp.get_json()["error"]
    assert MODEL in resp.get_json()["error"]


def test_a_document_without_an_id_is_a_400():
    """Scores come back keyed by id; a document without one could not be
    matched to the candidate it came from."""
    resp = _client().post("/rerank", json={
        "model": MODEL, "query": "q", "documents": [{"text": "no id here"}]})
    assert resp.status_code == 400
    assert "id" in resp.get_json()["error"]


def test_a_non_object_body_is_a_400():
    resp = _client().post("/rerank", json=["not", "an", "object"])
    assert resp.status_code == 400


def test_documents_must_be_a_list():
    resp = _client().post("/rerank", json={
        "model": MODEL, "query": "q", "documents": {"id": "d0"}})
    assert resp.status_code == 400


def test_a_scorer_failure_is_a_500_carrying_the_reason():
    def boom(*_a):
        raise RuntimeError("model not found on the hub")

    resp = _client(boom).post("/rerank", json={
        "model": MODEL, "query": "q", "documents": _docs("t")})
    assert resp.status_code == 500
    assert "model not found on the hub" in resp.get_json()["error"]
