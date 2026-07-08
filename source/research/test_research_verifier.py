from research import prompts
from research.report import SubtaskResult
from research.researcher import SourceRegistry
from research.telemetry import Telemetry
from research.verifier import (
    ClaimListModel,
    ClaimModel,
    ConflictModel,
    ConsistencyModel,
    EntailmentModel,
    OpenQuestionDecision,
    OpenQuestionReview,
    TierModel,
    validate_open_questions,
    verify_findings,
)
from research.test_research_stages import FakeCaller


def _noop_progress(stage, detail):
    pass


def _registry_with_source(url="https://example.org/a", extract="the raw text"):
    registry = SourceRegistry()
    source = registry.add(url, "A")
    registry.extracts[source.id] = extract
    return registry


def _ok_result(findings="The school opened in 1948 [1]."):
    return SubtaskResult(subtask_id="S1", title="T", findings_markdown=findings)


def test_supported_claim_keeps_section_untouched():
    registry = _registry_with_source(extract="It opened in 1948.")
    result = _ok_result()
    caller = FakeCaller(
        structured={
            prompts.TIER_SYSTEM: [TierModel(tier="encyclopedia", reason="wiki")],
            prompts.CLAIMS_SYSTEM: [
                ClaimListModel(
                    claims=[
                        ClaimModel(
                            text="The school opened in 1948.",
                            type="date",
                            source_ids=[1],
                        )
                    ]
                )
            ],
            prompts.ENTAIL_SYSTEM: [
                EntailmentModel(verdict="supported", evidence="It opened in 1948.")
            ],
        }
    )
    verified, stats = verify_findings(
        caller, registry, [result], _noop_progress, Telemetry()
    )
    assert stats == {"claims": 1, "keep": 1, "correct": 0, "hedge": 0, "drop": 0}
    assert verified == ["The school opened in 1948."]
    assert result.findings_markdown == "The school opened in 1948 [1]."  # no rewrite
    assert registry.all()[0].tier == "encyclopedia"


def test_contradicted_claim_triggers_rewrite_with_correction():
    registry = _registry_with_source(extract="Enrollment fell from 2016 to 2024.")
    result = _ok_result("Enrollment grew from 2016 to 2024 [1].")
    caller = FakeCaller(
        structured={
            prompts.TIER_SYSTEM: [TierModel(tier="official", reason="municipal")],
            prompts.CLAIMS_SYSTEM: [
                ClaimListModel(
                    claims=[
                        ClaimModel(
                            text="Enrollment grew from 2016 to 2024.",
                            type="number",
                            source_ids=[1],
                        )
                    ]
                )
            ],
            prompts.ENTAIL_SYSTEM: [
                EntailmentModel(
                    verdict="contradicted",
                    evidence="Enrollment fell from 2016 to 2024.",
                    corrected_claim="Enrollment fell from 2016 to 2024.",
                )
            ],
        },
        plain={prompts.REWRITE_SYSTEM: ["Enrollment fell from 2016 to 2024 [1]."]},
    )
    ledger = Telemetry()
    verified, stats = verify_findings(caller, registry, [result], _noop_progress, ledger)
    assert stats["correct"] == 1
    assert verified == ["Enrollment fell from 2016 to 2024."]
    assert result.findings_markdown == "Enrollment fell from 2016 to 2024 [1]."
    claim_row = next(e for e in ledger.events if e["event"] == "claim")
    assert claim_row["verdict"] == "contradicted"
    assert claim_row["action"] == "correct"


def test_unsupported_low_tier_claim_is_dropped():
    registry = _registry_with_source(extract="celebrity gossip")
    result = _ok_result("He married Queen Mary of Denmark [1].")
    caller = FakeCaller(
        structured={
            prompts.TIER_SYSTEM: [TierModel(tier="tabloid", reason="sensational")],
            prompts.CLAIMS_SYSTEM: [
                ClaimListModel(
                    claims=[
                        ClaimModel(
                            text="He married Queen Mary of Denmark.",
                            type="name",
                            source_ids=[1],
                        )
                    ]
                )
            ],
            prompts.ENTAIL_SYSTEM: [
                EntailmentModel(verdict="unsupported", evidence="not stated")
            ],
        },
        plain={prompts.REWRITE_SYSTEM: ["NOTHING VERIFIED"]},
    )
    verified, stats = verify_findings(
        caller, registry, [result], _noop_progress, Telemetry()
    )
    assert stats["drop"] == 1
    assert verified == []
    assert result.failed
    assert result.failure_note == "no claims survived verification"


def test_unsupported_decent_tier_claim_is_hedged():
    registry = _registry_with_source(extract="some text")
    result = _ok_result("The nose was made of silver [1].")
    caller = FakeCaller(
        structured={
            prompts.TIER_SYSTEM: [TierModel(tier="news", reason="outlet")],
            prompts.CLAIMS_SYSTEM: [
                ClaimListModel(
                    claims=[
                        ClaimModel(
                            text="The nose was made of silver.",
                            type="other",
                            source_ids=[1],
                        )
                    ]
                )
            ],
            prompts.ENTAIL_SYSTEM: [
                EntailmentModel(verdict="unsupported", evidence="not stated")
            ],
        },
        plain={
            prompts.REWRITE_SYSTEM: [
                "According to [1], the nose may have been silver (weak support)."
            ]
        },
    )
    verified, stats = verify_findings(
        caller, registry, [result], _noop_progress, Telemetry()
    )
    assert stats["hedge"] == 1
    assert "weak support" in result.findings_markdown


def test_claim_with_no_stored_extract_is_unsupported_without_llm_call():
    registry = SourceRegistry()
    registry.add("https://example.org/a", "A")  # no extract stored
    result = _ok_result("Claim citing nothing readable [1].")
    caller = FakeCaller(
        structured={
            prompts.CLAIMS_SYSTEM: [
                ClaimListModel(
                    claims=[
                        ClaimModel(
                            text="Claim citing nothing readable.",
                            type="other",
                            source_ids=[1],
                        )
                    ]
                )
            ],
        },
        plain={prompts.REWRITE_SYSTEM: ["hedged [1]."]},
    )
    verified, stats = verify_findings(
        caller, registry, [result], _noop_progress, Telemetry()
    )
    assert stats["hedge"] == 1
    # no TIER call (no extract) and no ENTAIL call (nothing to check against)
    assert all(c[0] != prompts.ENTAIL_SYSTEM for c in caller.calls)
    assert all(c[0] != prompts.TIER_SYSTEM for c in caller.calls)


def test_consistency_conflict_demotes_both_claims_to_hedge():
    registry = _registry_with_source(extract="text")
    first = _ok_result("Founded in 1895 [1].")
    second = SubtaskResult(
        subtask_id="S2", title="T2", findings_markdown="A prototype existed in 1875 [1]."
    )
    caller = FakeCaller(
        structured={
            prompts.TIER_SYSTEM: [TierModel(tier="reference", reason="archive")],
            prompts.CLAIMS_SYSTEM: [
                ClaimListModel(
                    claims=[
                        ClaimModel(text="Founded in 1895.", type="date", source_ids=[1])
                    ]
                ),
                ClaimListModel(
                    claims=[
                        ClaimModel(
                            text="A prototype existed in 1875.",
                            type="date",
                            source_ids=[1],
                        )
                    ]
                ),
            ],
            prompts.ENTAIL_SYSTEM: [
                EntailmentModel(verdict="supported", evidence="1895"),
                EntailmentModel(verdict="supported", evidence="1875"),
            ],
            prompts.CONSISTENCY_SYSTEM: [
                ConsistencyModel(
                    conflicts=[
                        ConflictModel(
                            first=0,
                            second=1,
                            reason="prototype predates founding",
                        )
                    ]
                )
            ],
        },
        plain={
            prompts.REWRITE_SYSTEM: [
                "Sources conflict on the founding [1].",
                "Sources conflict on the prototype [1].",
            ]
        },
    )
    ledger = Telemetry()
    verified, stats = verify_findings(
        caller, registry, [first, second], _noop_progress, ledger
    )
    assert stats["hedge"] == 2
    assert verified == []
    conflict_rows = [e for e in ledger.events if e["event"] == "consistency_conflict"]
    assert len(conflict_rows) == 1
    assert conflict_rows[0]["reason"] == "prototype predates founding"


def test_validate_open_questions_removes_and_rewrites():
    caller = FakeCaller(
        structured={
            prompts.OPENQ_REVIEW_SYSTEM: [
                OpenQuestionReview(
                    decisions=[
                        OpenQuestionDecision(
                            index=0,
                            action="remove",
                            reason="answered by verified claim",
                        ),
                        OpenQuestionDecision(
                            index=1,
                            action="rewrite",
                            rewrite="Which progenitor produced SN 1572?",
                            reason="narrower genuine question",
                        ),
                    ]
                )
            ]
        }
    )
    ledger = Telemetry()
    result = validate_open_questions(
        caller,
        ["Tycho Brahe was born on 1546-12-14."],
        "- When was Tycho Brahe born?\n- Was SN 1572 atmospheric?\n- What else?",
        ledger,
        _noop_progress,
    )
    assert result == (
        "- Which progenitor produced SN 1572?\n- What else?"
    )
    actions = [e["action"] for e in ledger.events if e["event"] == "open_question"]
    assert actions == ["remove", "rewrite", "keep"]


def test_validate_open_questions_no_bullets_is_identity():
    caller = FakeCaller()
    text = "nothing here"
    assert (
        validate_open_questions(caller, [], text, Telemetry(), _noop_progress) == text
    )
    assert caller.calls == []
