import pytest
import db
import agents.query_kb_helpers as kb
from agents.query_kb_helpers import Match, SeedMemory


@pytest.fixture()
def app_ctx():
    app = db.make_app()
    ctx = app.app_context(); ctx.push()
    try:
        yield
    finally:
        db.db.session.rollback(); ctx.pop()


@pytest.fixture()
def registry(app_ctx, monkeypatch):
    # Seed the in-memory registry directly (no embeddings, no pgvector).
    entries = {
        "u-candy": {"id": "u-candy", "path": "food.candy", "kind": "static",
                    "answer": "Simon likes licorice.", "_source": "user-overlay"},
        "up-name": {"id": "up-name", "path": "identity.name", "kind": "static",
                    "answer": "EgonBot.", "_source": "upstream"},
        "dyn-git": {"id": "dyn-git", "path": "dev.git", "kind": "dynamic",
                    "handler": "git_status", "_source": "upstream"},
    }
    monkeypatch.setattr(kb, "_entries_by_id", entries)
    return entries


def test_retrieve_seed_memories_filters_static_and_tags(registry):
    ranked = [Match(qa_id="u-candy", method="semantic", score=0.81),
              Match(qa_id="dyn-git", method="semantic", score=0.79),   # dynamic → excluded
              Match(qa_id="up-name", method="semantic", score=0.70)]
    out = kb.retrieve_seed_memories("candy", _ranker=lambda q: ranked)
    assert [m.uuid for m in out] == ["u-candy", "up-name"]   # dynamic dropped, score order
    assert out[0].source == "user-overlay" and out[0].path == "food.candy"
    assert out[0].answer == "Simon likes licorice."


def test_retrieve_seed_memories_drops_below_min_score_and_caps(registry):
    ranked = [Match(qa_id="u-candy", method="semantic", score=0.50)]  # below MIN_SCORE (0.60)
    assert kb.retrieve_seed_memories("x", _ranker=lambda q: ranked) == []
    many = [Match(qa_id="up-name", method="semantic", score=0.9 - i*0.01) for i in range(10)]
    out = kb.retrieve_seed_memories("x", limit=2, _ranker=lambda q: many)
    assert len(out) == 2
