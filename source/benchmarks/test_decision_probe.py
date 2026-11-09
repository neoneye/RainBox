"""Offline checks for the decision-model probe: the fixture is well-formed, the
requests have the shapes the proposals measured, and the scoring counts bands
the way the proposals read them. No model is called."""
import pytest

from benchmarks import decision_probe as dp

CASES = dp.load_cases()
BANDS = CASES["bands"]


def _exp(exp_id):
    return next(e for e in CASES["experiments"] if e["id"] == exp_id)


def test_fixture_ids_are_unique_and_labels_valid():
    seen = set()
    for exp in CASES["experiments"]:
        assert exp["id"] not in seen
        seen.add(exp["id"])
        items = exp.get("cases") or exp.get("candidates")
        ids = [c["id"] for c in items]
        assert len(ids) == len(set(ids)), exp["id"]
        if exp["kind"] == "choice":
            assert all(c["expected"] in exp["options"] for c in items), exp["id"]
        if exp["kind"] == "noul_checklist":
            for c in items:
                assert set(c["expected"]) <= set(exp["questions"]), c["id"]


def test_requests_per_experiment():
    counts = {e["id"]: len(dp.build_requests(e)) for e in CASES["experiments"]}
    assert counts == {
        "identity_pair": 14, "identity_fanout": 1, "redo_noul": 14,
        "short_message_choice": 51, "reply_checklist": 12,
        "verdict_choice": 14, "hedge": 8, "same_answer": 8,
    }


def test_short_message_states_match_contexts():
    reqs = dp.build_requests(_exp("short_message_choice"))
    alone = next(r for r in reqs if r["context"] == "alone")
    after = next(r for r in reqs if r["context"] == "after_readme")
    assert set(alone["state"]) == {"short_message"}
    assert after["state"]["previous_request"] == "fix the typo in the README"
    assert set(after["questions"]["q"]["criteria"]) == {
        "redo", "expand", "command", "acknowledge", "new_request"}


def test_checklist_asks_repeat_questions_only_with_earlier_replies():
    reqs = {r["case"]: r for r in dp.build_requests(_exp("reply_checklist"))}
    assert "acknowledges" not in reqs["wrong_subject"]["questions"]
    assert {"acknowledges", "unexplained_change"} <= set(
        reqs["repeat_acknowledged"]["questions"])
    assert set(reqs["wrong_subject"]["expected"]) <= set(
        reqs["wrong_subject"]["questions"])


def test_fanout_is_one_request_with_a_question_per_candidate():
    (req,) = dp.build_requests(_exp("identity_fanout"))
    assert len(req["questions"]) == 14
    assert req["state"] == {"current_message": "how many r are in strawberry?"}


@pytest.mark.parametrize("p,expected,band,accepted_wrong", [
    (0.97, True, "yes", False),
    (0.84, False, "yes", True),     # confident and wrong: what the band exists to count
    (0.68, False, "unclear", False),
    (0.05, False, "no", False),
    (0.10, True, "no", True),
])
def test_score_noul_bands(p, expected, band, accepted_wrong):
    s = dp.score_noul(p, expected, BANDS)
    assert s["band"] == band
    assert s["accepted_wrong"] is accepted_wrong


def test_score_choice_accepts_only_at_threshold():
    s = dp.score_choice({"redo": 0.55, "expand": 0.40, "command": 0.05}, "expand", 0.8)
    assert (s["choice"], s["top_ok"], s["accepted"], s["accepted_wrong"]) == (
        "redo", False, False, False)
    s = dp.score_choice({"redo": 0.97, "expand": 0.03}, "redo", 0.8)
    assert s["accepted"] and not s["accepted_wrong"]


def test_summarize_counts_abstentions_and_errors():
    rows = [
        {"ms": 100, "scored": {"q": dp.score_noul(0.9, True, BANDS)}},
        {"ms": 300, "scored": {"q": dp.score_noul(0.5, True, BANDS)}},
        {"error": "timeout"},
    ]
    s = dp.summarize(rows)
    assert s["by_question"]["q"] == {"scored": 2, "top_ok": 2, "accepted": 1,
                                     "accepted_wrong": 0, "abstained": 1}
    assert (s["errors"], s["median_ms"]) == (1, 200)


@pytest.mark.parametrize("url,model", [
    ("http://example.com:11434", "d1:3b"),
    ("http://localhost:11434", "clef:cloud"),
])
def test_refuses_remote_execution(url, model):
    with pytest.raises(SystemExit):
        dp.check_local(url, model)
