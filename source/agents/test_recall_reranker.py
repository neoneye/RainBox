"""Tests for the recall filter's cross-encoder backend (agents/recall_reranker.py).

No model and no service: the HTTP call is faked, so what is under test is the
setting parsing, the text a candidate row turns into, and the keep/drop policy
over scores.
"""
import importlib.util
import pathlib

import pytest

from agents.recall_reranker import (
    BACKEND_LLM,
    RERANK_KEEP_RATIO,
    RERANK_KEEP_TOP_N,
    RERANK_NOISE_FLOOR,
    RERANKER_MODELS,
    apply_rerank_scores,
    backend_choices,
    document_text,
    rerank,
    reranker_model,
)


class _Cand:
    """A stand-in for seed_memory.Match — apply_rerank_scores reads qa_id."""

    def __init__(self, qa_id):
        self.qa_id = qa_id


def _cands(*ids):
    return [_Cand(i) for i in ids]


# --- the setting ------------------------------------------------------------


def test_llm_is_the_backend_that_selects_no_reranker():
    assert reranker_model(BACKEND_LLM) is None
    assert reranker_model(None) is None
    assert reranker_model("") is None


def test_a_reranker_backend_names_its_model():
    assert (reranker_model("reranker:mmarco-mMiniLMv2-L12-H384-v1")
            == "mmarco-mMiniLMv2-L12-H384-v1")


def test_an_unknown_reranker_model_raises_rather_than_falling_back():
    """Falling back to the LLM would hide a typo behind the very twenty
    seconds the operator switched away from."""
    with pytest.raises(ValueError, match="unknown reranker model"):
        reranker_model("reranker:no-such-model")


def test_the_settings_registry_offers_exactly_these_backends():
    """The /settings dropdown and this module must not drift apart: the
    registry's choices are literal (it is the source of truth for settings),
    so a model added here has to appear there."""
    from db.settings import SETTINGS

    spec = SETTINGS["memory.recall_filter_backend"]
    assert list(spec.choices or ()) == backend_choices()
    assert spec.default == BACKEND_LLM


def test_the_sidecar_serves_the_models_this_module_names():
    """The service runs in its own venv, so nothing imports across the seam —
    this reads its module directly to catch the two lists drifting."""
    path = pathlib.Path(__file__).resolve().parent.parent / "reranker" / "server.py"
    spec = importlib.util.spec_from_file_location("reranker_server_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.MODELS == RERANKER_MODELS


# --- what the cross-encoder reads -------------------------------------------


def test_document_text_is_the_fact_itself_not_the_prompt_row():
    """Candidate rows are built for the filter LLM's prompt, where every value
    is repr()-quoted. A cross-encoder matches natural language."""
    row = {"id": "qa-1", "source": "seed", "path": "deploy.host",
           "similarity score": 812, "kind": "static",
           "matched_question": repr("where do we deploy?"),
           "answer": repr("To prod-web-01.")}
    assert document_text(row) == "where do we deploy?\nTo prod-web-01."


def test_document_text_takes_a_remembered_facts_text():
    row = {"id": "c-1", "source": "remembered fact", "kind": "fact",
           "text": repr("the deploy host is prod-web-01")}
    assert document_text(row) == "the deploy host is prod-web-01"


def test_document_text_leaves_unquoted_text_alone():
    row = {"id": "c-1", "text": "plain text, never repr'd"}
    assert document_text(row) == "plain text, never repr'd"


def test_document_text_falls_back_to_the_path_when_there_is_no_text():
    """A dynamic seed entry carries a handler, not an answer; its path is the
    only thing left to match on, and an empty document would score as noise."""
    row = {"id": "qa-1", "path": "system.uptime_host", "kind": "dynamic",
           "handler": "uptime"}
    assert document_text(row) == "system.uptime_host"


# --- the keep/drop policy ---------------------------------------------------


def test_a_short_list_is_kept_whole():
    """Fewer than top_k candidates is no competition — the same rule the LLM
    path applies, so an over-aggressive scorer cannot empty a small set."""
    scored = apply_rerank_scores(
        {"a": 0.9, "b": 0.0001}, _cands("a", "b"), top_k=5)
    assert [s.kept for s in scored] == [True, True]


def test_a_full_list_keeps_the_top_ranked_and_the_close_seconds():
    scores = {"a": 0.80, "b": 0.50, "c": 0.30, "d": 0.02, "e": 0.001}
    scored = apply_rerank_scores(scores, _cands("a", "b", "c", "d", "e"), top_k=5)
    kept = {s.qa_id for s in scored if s.kept}
    # a: best. b: top-N on rank AND within RERANK_KEEP_RATIO of the best.
    # c: 0.30 is below half of 0.80 and outside the top N.
    assert kept == {"a", "b"}
    assert RERANK_KEEP_TOP_N == 2 and RERANK_KEEP_RATIO == 0.5


def test_merit_keeps_a_lower_ranked_candidate_the_rank_rule_would_not():
    scores = {"a": 0.80, "b": 0.79, "c": 0.78, "d": 0.01, "e": 0.001}
    kept = {s.qa_id for s in apply_rerank_scores(
        scores, _cands("a", "b", "c", "d", "e"), top_k=5) if s.kept}
    assert kept == {"a", "b", "c"}


def test_a_list_of_noise_is_dropped_whole():
    """The rank rule must not resurrect a list where even the best candidate
    scored as noise — the absolute floor is what makes 'nothing matched' a
    reachable answer."""
    scores = {"a": 0.01, "b": 0.004, "c": 0.001, "d": 0.0, "e": 0.0}
    scored = apply_rerank_scores(scores, _cands("a", "b", "c", "d", "e"), top_k=5)
    assert not any(s.kept for s in scored)
    assert RERANK_NOISE_FLOOR == 0.05


def test_an_unscored_candidate_scores_zero_and_is_dropped():
    scores = {"a": 0.9}
    scored = apply_rerank_scores(scores, _cands("a", "b", "c", "d", "e"), top_k=5)
    by_id = {s.qa_id: s for s in scored}
    assert by_id["a"].kept and not by_id["b"].kept
    assert by_id["b"].rerank_score == 0.0


def test_scored_rows_are_best_first_and_carry_the_score_not_likert_scales():
    """The reranker produced one number; writing it into three Likert columns
    would put a reading in the trace that no model made."""
    scored = apply_rerank_scores(
        {"a": 0.2, "b": 0.9, "c": 0.5}, _cands("a", "b", "c"), top_k=5)
    assert [s.qa_id for s in scored] == ["b", "c", "a"]
    assert [s.rerank_score for s in scored] == [0.9, 0.5, 0.2]
    assert all(s.direct == 0 and s.indirect == 0 and s.relevancy == 0
               for s in scored)


# --- the HTTP seam ----------------------------------------------------------


class _Response:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


def test_rerank_posts_the_documents_and_returns_scores_by_id(monkeypatch):
    import requests

    seen = {}

    def fake_post(url, json=None, timeout=None):
        seen.update(url=url, body=json, timeout=timeout)
        return _Response(200, {
            "model": json["model"], "ms": 41, "max_length": 512, "scores": [
                {"id": "a", "score": 0.9, "tokens": 12},
                {"id": "b", "score": 0.01, "tokens": 700}]})

    monkeypatch.setattr(requests, "post", fake_post)
    result = rerank(
        "where do we deploy?",
        [{"id": "a", "text": "prod-web-01"}, {"id": "b", "text": "Paris"}],
        model="mmarco-mMiniLMv2-L12-H384-v1", url="http://127.0.0.1:5008")
    assert result.scores == {"a": 0.9, "b": 0.01}
    assert result.service_ms == 41
    # What the caller cannot see for itself: how long each pair was, and the
    # ceiling it was measured against (here 'b' was cut at the tokenizer).
    assert result.tokens == {"a": 12, "b": 700}
    assert result.max_length == 512
    assert seen["url"] == "http://127.0.0.1:5008/rerank"
    assert seen["body"]["query"] == "where do we deploy?"
    assert [d["id"] for d in seen["body"]["documents"]] == ["a", "b"]


def test_rerank_survives_a_service_that_reports_no_token_counts(monkeypatch):
    """The counts are extra detail for the trace, not the contract."""
    import requests

    monkeypatch.setattr(requests, "post", lambda *a, **k: _Response(
        200, {"ms": 9, "scores": [{"id": "a", "score": 0.5}]}))
    result = rerank("q", [{"id": "a", "text": "t"}],
                    model="mmarco-mMiniLMv2-L12-H384-v1")
    assert result.scores == {"a": 0.5}
    assert result.tokens == {}


def test_rerank_raises_on_a_service_error(monkeypatch):
    """A failure has to reach the caller: memory_query catches it and falls
    back to gated retrieval, which is a different outcome from 'nothing was
    relevant'."""
    import requests

    monkeypatch.setattr(requests, "post", lambda *a, **k: _Response(
        500, None, "rerank failed: out of memory"))
    with pytest.raises(RuntimeError, match="returned 500"):
        rerank("q", [{"id": "a", "text": "t"}],
               model="mmarco-mMiniLMv2-L12-H384-v1")
