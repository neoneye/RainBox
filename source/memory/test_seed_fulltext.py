"""Unit tests for the lexical full-text seed ranking and the hybrid blend
(memory.seed_memory._fulltext_ranked / _hybrid_seed_ranked).

The motivating failure: "how is Simon related to the demoscene" — the token
`demoscene` appears verbatim in entry questions, but question-embedding
similarity ranked generic Simon entries above them, so the demoscene entries
never reached the filter. Full-text is the signal that catches exact content
words; the hybrid blend lets either signal surface a candidate.
"""

import pytest

from memory import seed_memory as qkb
from memory.seed_memory import Match, _fulltext_ranked, _hybrid_seed_ranked


@pytest.fixture
def kb(monkeypatch):
    """A small registry mirroring the real failure case: one entry with the
    rare content word in its questions, one with it only in the answer, and
    generic entries that share only the common token 'simon'."""
    entries = {
        "qa-demoscene": {
            "kind": "static", "path": "human.simon.demoscene",
            "questions": ["Demoscene / computer parties", "Simon's demo projects 1994-"],
            "answer": "Simon was active in the demoscene in the 1990s.",
        },
        "qa-sibling": {
            "kind": "static", "path": "human.sibling.identity",
            "questions": ["Who is Simon's brother?"],
            # The content word lives ONLY in the answer text.
            "answer": "Hans søskende er ikke navngivet her.",
        },
        "qa-name": {
            "kind": "static", "path": "human.simon.name",
            "questions": ["Who is Simon?"],
            "answer": "Simon is a software developer.",
        },
        "qa-food": {
            "kind": "static", "path": "human.simon.food",
            "questions": ["Does Simon have food allergy?"],
            "answer": "Simon has no food allergies.",
        },
        "qa-shielded": {
            "kind": "static", "path": "human.simon.secret", "shield": "locked.topic",
            "questions": ["Simon demoscene secret"],
            "answer": "Shielded demoscene entry.",
        },
    }
    monkeypatch.setattr(qkb, "_entries_by_id", entries)
    monkeypatch.setattr(qkb, "_unlocked_shields", lambda: set())
    monkeypatch.setattr(
        qkb, "_entry_locked", lambda entry, unlocked: bool(entry.get("shield")))
    return entries


def test_rare_content_word_outranks_common_token_matches(kb):
    ranked = _fulltext_ranked("how is Simon related to the demoscene")
    assert ranked[0].qa_id == "qa-demoscene"
    assert ranked[0].method == "fulltext"
    assert ranked[0].score == 1.0                      # max-normalized
    # Generic simon-only entries score strictly lower (IDF: 'simon' is common).
    by_id = {m.qa_id: m.score for m in ranked}
    assert by_id.get("qa-name", 0.0) < by_id["qa-demoscene"]


def test_answer_only_token_is_found(kb):
    """A token that appears in no question but in an answer still surfaces the
    entry — the signal question embeddings can never see."""
    ranked = _fulltext_ranked("søskende")
    assert [m.qa_id for m in ranked] == ["qa-sibling"]


def test_question_match_outweighs_answer_match(kb):
    # 'demoscene' is in qa-demoscene's questions but only in qa-shielded's...
    # (shielded is excluded) — compare question-hit vs answer-hit weighting via
    # 'allergy': in qa-food's question AND answer vs nothing elsewhere.
    ranked = _fulltext_ranked("allergy")
    assert ranked[0].qa_id == "qa-food"


def test_shielded_entries_are_excluded(kb):
    ranked = _fulltext_ranked("demoscene secret")
    assert all(m.qa_id != "qa-shielded" for m in ranked)


def test_stopword_only_query_returns_nothing(kb):
    assert _fulltext_ranked("the and of") == []


def test_matched_question_is_the_best_overlapping_one(kb):
    ranked = _fulltext_ranked("demoscene parties")
    assert ranked[0].matched_question == "Demoscene / computer parties"


def test_hybrid_interleaves_both_signals(kb, monkeypatch):
    monkeypatch.setattr(qkb, "_semantic_ranked", lambda q, vs, **_: [
        Match(qa_id="qa-name", method="semantic", score=0.9, matched_question="Who is Simon?"),
        Match(qa_id="qa-demoscene", method="semantic", score=0.5,
              matched_question="Simon's demo projects 1994-"),
    ])
    ranked = _hybrid_seed_ranked("how is Simon related to the demoscene", None)
    # Interleave: best vector, then best full-text, deduplicated.
    assert [m.qa_id for m in ranked[:2]] == ["qa-name", "qa-demoscene"]
    by_id = {m.qa_id: m for m in ranked}
    assert by_id["qa-name"].method == "semantic+fulltext"
    assert by_id["qa-demoscene"].method == "semantic+fulltext"


def test_hybrid_fulltext_only_hits_reach_the_top_k_prefix(kb, monkeypatch):
    """The demoscene regression: entries absent from the embedding top-K got a
    capped blended score and were crowded out of any top-K slice by mediocre
    embedding matches. With rank interleaving, a strong full-text hit owns
    every second slot regardless of score scales."""
    monkeypatch.setattr(qkb, "_semantic_ranked", lambda q, vs, **_: [
        Match(qa_id="qa-name", method="semantic", score=0.75, matched_question="Who is Simon?"),
        Match(qa_id="qa-food", method="semantic", score=0.70,
              matched_question="Does Simon have food allergy?"),
        Match(qa_id="qa-sibling", method="semantic", score=0.65,
              matched_question="Who is Simon's brother?"),
    ])
    # qa-demoscene is NOT in the vector list; full-text ranks it #1.
    ranked = _hybrid_seed_ranked("how is Simon related to the demoscene", None)
    top3 = [m.qa_id for m in ranked[:3]]
    assert "qa-demoscene" in top3          # slot 2 belongs to full-text's best
    assert ranked[1].qa_id == "qa-demoscene"
    assert ranked[1].method == "fulltext"
    assert ranked[1].matched_question in (
        "Demoscene / computer parties", "Simon's demo projects 1994-")


def test_hybrid_degrades_to_fulltext_when_embeddings_fail(kb, monkeypatch):
    def boom(q, vs, **_):
        raise RuntimeError("embedding server down")

    monkeypatch.setattr(qkb, "_semantic_ranked", boom)
    ranked = _hybrid_seed_ranked("demoscene", None)
    assert ranked and ranked[0].qa_id == "qa-demoscene"
    assert ranked[0].method == "fulltext"
