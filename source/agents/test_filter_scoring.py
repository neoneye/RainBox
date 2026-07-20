"""Unit tests for the code-side keep/drop policy over the filter LLM's
Likert scores (agents.query_filter_router.apply_filter_scores).

The LLM only scores candidates (direct/indirect/relevancy, "1".."5"); which
candidates survive is decided here, deterministically: fewer than top_k
candidates → keep all (an over-aggressive scorer cannot empty a small result
set); a full list → relative first, absolute second — the top
FILTER_KEEP_TOP_N ranked candidates survive on rank (unless pure noise below
FILTER_KEEP_TOP_FLOOR), plus anything with a scale at FILTER_KEEP_THRESHOLD.
"""

from agents.query_filter_router import (
    FILTER_KEEP_THRESHOLD,
    FILTER_KEEP_TOP_FLOOR,
    FILTER_KEEP_TOP_N,
    FilterDecision,
    apply_filter_scores,
)
from memory.seed_memory import Match


def _match(qa_id, score=0.5):
    return Match(qa_id=qa_id, method="semantic", score=score,
                 matched_question=qa_id)


def _decision(*items):
    return FilterDecision(items=[
        {"id": i[0], "direct": i[1], "indirect": i[2], "relevancy": i[3]}
        for i in items
    ])


def test_full_list_keeps_threshold_and_top_ranked():
    candidates = [_match(f"qa-{n}") for n in range(5)]
    decision = _decision(
        ("qa-0", "5", "1", "5"),   # direct at threshold → kept (rank 0)
        ("qa-1", "1", "4", "2"),   # indirect at threshold → kept (rank 2)
        ("qa-2", "3", "3", "3"),   # below threshold, but rank 1 → kept
        ("qa-3", "1", "1", "1"),   # noise, low rank → dropped
        ("qa-4", "1", "1", "4"),   # relevancy at threshold → kept
    )
    scored = apply_filter_scores(decision, candidates)
    kept = {s.qa_id for s in scored if s.kept}
    assert kept == {"qa-0", "qa-1", "qa-2", "qa-4"}
    # The boundaries the cases above encode.
    assert FILTER_KEEP_THRESHOLD == 4
    assert FILTER_KEEP_TOP_N == 2
    assert FILTER_KEEP_TOP_FLOOR == 2


def test_full_list_low_calibrated_scorer_keeps_best_by_rank():
    """The operator's gemma4:e4b case: a scorer that calibrates the whole
    scale low (best candidate 2/1/3) must not empty the list — the top-ranked
    candidates survive on relative merit; pure 1/1/1 noise still drops."""
    candidates = [_match(f"qa-{n}") for n in range(5)]
    decision = _decision(
        ("qa-0", "2", "1", "3"),   # best-ranked → kept by rank
        ("qa-1", "1", "2", "2"),   # second-ranked, above floor → kept by rank
        ("qa-2", "1", "1", "1"),   # noise → dropped
        ("qa-3", "1", "1", "1"),   # noise → dropped
        ("qa-4", "1", "1", "1"),   # noise → dropped
    )
    scored = apply_filter_scores(decision, candidates)
    kept = {s.qa_id for s in scored if s.kept}
    assert kept == {"qa-0", "qa-1"}


def test_full_list_of_pure_noise_keeps_nothing():
    """Rank alone is not enough: when even the best candidate never rises
    above the noise floor, the list empties — an off-topic query must not
    feed junk to the route LLM."""
    candidates = [_match(f"qa-{n}") for n in range(5)]
    decision = _decision(*((f"qa-{n}", "1", "1", "1") for n in range(5)))
    scored = apply_filter_scores(decision, candidates)
    assert not any(s.kept for s in scored)


def test_fewer_than_top_k_keeps_everything():
    """The operator's rule: with fewer than top_k candidates there is no real
    competition — keep all, even those the LLM scored as droppable."""
    candidates = [_match("qa-good"), _match("qa-weak")]
    decision = _decision(
        ("qa-good", "5", "5", "5"),
        ("qa-weak", "1", "1", "1"),
    )
    scored = apply_filter_scores(decision, candidates)
    assert all(s.kept for s in scored)


def test_hallucinated_ids_are_ignored():
    candidates = [_match(f"qa-{n}") for n in range(5)]
    decision = _decision(("qa-invented", "5", "5", "5"))
    scored = apply_filter_scores(decision, candidates)
    assert {s.qa_id for s in scored} == {f"qa-{n}" for n in range(5)}
    assert not any(s.kept for s in scored)  # real candidates were unscored


def test_unscored_candidates_default_to_zero_on_a_full_list():
    candidates = [_match(f"qa-{n}") for n in range(5)]
    decision = _decision(("qa-0", "5", "1", "1"))  # the other four omitted
    scored = apply_filter_scores(decision, candidates)
    by_id = {s.qa_id: s for s in scored}
    assert by_id["qa-0"].kept
    assert by_id["qa-1"].direct == 0 and not by_id["qa-1"].kept


def test_ordering_is_best_first_direct_dominates():
    candidates = [_match("qa-a"), _match("qa-b"), _match("qa-c")]
    decision = _decision(
        ("qa-a", "2", "5", "5"),
        ("qa-b", "5", "1", "1"),
        ("qa-c", "2", "5", "4"),
    )
    scored = apply_filter_scores(decision, candidates)
    assert [s.qa_id for s in scored] == ["qa-b", "qa-a", "qa-c"]


def test_duplicate_score_rows_first_one_wins():
    candidates = [_match(f"qa-{n}") for n in range(5)]
    decision = _decision(
        ("qa-0", "5", "5", "5"),
        ("qa-0", "1", "1", "1"),   # duplicate row for the same id
    )
    scored = apply_filter_scores(decision, candidates)
    by_id = {s.qa_id: s for s in scored}
    assert by_id["qa-0"].kept and by_id["qa-0"].direct == 5
