"""Claim verification: the gate between "cited" and "checked".

The pipeline's earlier stages guarantee provenance (every claim carries a
[n] citation) but not entailment (that source [n] actually says it), and
small models systematically strengthen hedges, invent uncertainty, and let
tabloid material shape conclusions. This stage closes that gap with the
run's own material — no world knowledge assumed:

1. classify each fetched source's quality tier,
2. extract the checkable claims from each findings section,
3. check every claim against the RAW extracts of the sources it cites
   (notes are a compression hop; checking a compression against a
   compression lets amplified errors through),
4. one consistency pass across all surviving claims (catches an entity
   acting before it existed, trends stated in opposite directions),
5. rewrite each findings section from the verdicts: keep / correct /
   hedge / drop,
6. after synthesis, validate the open questions against the verified
   claims — an open question must be something the sources genuinely
   leave unresolved, not manufactured doubt.

Every decision lands in the claims ledger (`report.claims.jsonl` next to
the report): the prose is the view, the ledger is the audit trail."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from research import prompts
from research.caller import Caller
from research.report import SubtaskResult
from research.researcher import Progress, SourceRegistry
from research.telemetry import Telemetry

NOTHING_VERIFIED = "NOTHING VERIFIED"

# Tiers whose unsupported claims are dropped rather than hedged: rumor-grade
# material may appear only when a source actually states it.
LOW_TIERS = {"blog", "marketing", "tabloid"}

ENTAIL_EXTRACT_CHAR_CAP = 3500
ENTAIL_MAX_SOURCES = 2
TIER_EXTRACT_CHAR_CAP = 1500
OPENQ_CLAIMS_CHAR_CAP = 8000


class TierModel(BaseModel):
    tier: Literal[
        "official", "reference", "encyclopedia", "news", "blog", "marketing", "tabloid"
    ]
    reason: str = Field(description="One line on why this tier fits.")


class ClaimModel(BaseModel):
    text: str = Field(description="The claim as one self-contained sentence.")
    type: str = Field(description="date | number | name | event | causal | other")
    source_ids: list[int] = Field(description="The bracketed source numbers it cites.")


class ClaimListModel(BaseModel):
    claims: list[ClaimModel]


class EntailmentModel(BaseModel):
    verdict: Literal["supported", "unsupported", "contradicted"]
    evidence: str = Field(description="The decisive source text, quoted.")
    corrected_claim: str = Field(
        default="", description="When contradicted: what the source actually states."
    )


class ConflictModel(BaseModel):
    first: int
    second: int
    reason: str


class ConsistencyModel(BaseModel):
    conflicts: list[ConflictModel]


class OpenQuestionDecision(BaseModel):
    index: int
    action: Literal["keep", "rewrite", "remove"]
    rewrite: str = ""
    reason: str = ""


class OpenQuestionReview(BaseModel):
    decisions: list[OpenQuestionDecision]


class _VerifiedClaim:
    def __init__(self, section_index: int, claim: ClaimModel, entail: EntailmentModel):
        self.section_index = section_index
        self.claim = claim
        self.entail = entail
        self.action = "keep"  # keep | correct | hedge | drop
        self.conflict_reason = ""

    def final_text(self) -> str:
        if self.action == "correct" and self.entail.corrected_claim.strip():
            return self.entail.corrected_claim.strip()
        return self.claim.text.strip()


def classify_sources(
    caller: Caller,
    registry: SourceRegistry,
    ledger: Telemetry,
    progress: Progress,
) -> dict[int, str]:
    """One tier per fetched source; also stamped onto the Source so the
    References section can show it."""
    tiers: dict[int, str] = {}
    for source in registry.all():
        extract = registry.extracts.get(source.id)
        if not extract:
            continue
        user_prompt = (
            f"URL: {source.url}\nTITLE: {source.title}\n\n"
            f"START OF EXTRACT:\n{extract[:TIER_EXTRACT_CHAR_CAP]}"
        )
        result = caller.structured(prompts.TIER_SYSTEM, user_prompt, TierModel)
        assert isinstance(result, TierModel)
        tiers[source.id] = result.tier
        source.tier = result.tier
        ledger.record(
            {
                "event": "source_tier",
                "source": source.id,
                "url": source.url,
                "tier": result.tier,
                "reason": result.reason,
            }
        )
    progress("verify", f"classified {len(tiers)} sources")
    return tiers


def verify_findings(
    caller: Caller,
    registry: SourceRegistry,
    results: list[SubtaskResult],
    progress: Progress,
    ledger: Telemetry,
) -> tuple[list[str], dict[str, int], dict[int, str]]:
    """Run the claim gate over every successful findings section, rewriting
    the sections in place. Returns (verified claim texts, stats, tiers)."""
    tiers = classify_sources(caller, registry, ledger, progress)

    checked: list[_VerifiedClaim] = []
    for index, result in enumerate(results):
        if result.failed or not result.findings_markdown.strip():
            continue
        progress("verify", f"extracting claims from {result.subtask_id}")
        claim_list = caller.structured(
            prompts.CLAIMS_SYSTEM, result.findings_markdown, ClaimListModel
        )
        assert isinstance(claim_list, ClaimListModel)
        for claim in claim_list.claims:
            if not claim.text.strip():
                continue
            entail = _entail(caller, registry, claim)
            verified = _VerifiedClaim(index, claim, entail)
            verified.action = _claim_action(claim, entail, tiers)
            checked.append(verified)

    _apply_consistency(caller, checked, ledger, progress)

    stats = {"claims": len(checked), "keep": 0, "correct": 0, "hedge": 0, "drop": 0}
    for verified in checked:
        stats[verified.action] += 1
        ledger.record(
            {
                "event": "claim",
                "subtask": results[verified.section_index].subtask_id,
                "text": verified.claim.text,
                "type": verified.claim.type,
                "source_ids": verified.claim.source_ids,
                "verdict": verified.entail.verdict,
                "evidence": verified.entail.evidence,
                "corrected_claim": verified.entail.corrected_claim,
                "conflict": verified.conflict_reason,
                "action": verified.action,
            }
        )

    _rewrite_sections(caller, results, checked, progress)

    verified_texts = [
        v.final_text() for v in checked if v.action in ("keep", "correct")
    ]
    progress(
        "verify",
        f"{stats['claims']} claims: {stats['keep']} kept, {stats['correct']} "
        f"corrected, {stats['hedge']} hedged, {stats['drop']} dropped",
    )
    return verified_texts, stats, tiers


def _entail(
    caller: Caller, registry: SourceRegistry, claim: ClaimModel
) -> EntailmentModel:
    blocks: list[str] = []
    for source_id in claim.source_ids[:ENTAIL_MAX_SOURCES]:
        extract = registry.extracts.get(source_id)
        if not extract:
            continue
        sources = registry.all()
        if not 1 <= source_id <= len(sources):
            continue
        source = sources[source_id - 1]
        blocks.append(
            prompts.wrap_source_block(
                source_id, source.url, extract[:ENTAIL_EXTRACT_CHAR_CAP]
            )
        )
    if not blocks:
        # A claim citing nothing we can re-read cannot be verified.
        return EntailmentModel(
            verdict="unsupported", evidence="no stored extract for cited sources"
        )
    user_prompt = f"CLAIM: {claim.text}\n\n" + "\n\n".join(blocks)
    result = caller.structured(prompts.ENTAIL_SYSTEM, user_prompt, EntailmentModel)
    assert isinstance(result, EntailmentModel)
    return result


def _claim_action(
    claim: ClaimModel, entail: EntailmentModel, tiers: dict[int, str]
) -> str:
    if entail.verdict == "supported":
        return "keep"
    if entail.verdict == "contradicted":
        return "correct" if entail.corrected_claim.strip() else "drop"
    cited = [tiers.get(source_id) for source_id in claim.source_ids]
    known = [tier for tier in cited if tier]
    if known and all(tier in LOW_TIERS for tier in known):
        return "drop"
    return "hedge"


def _apply_consistency(
    caller: Caller,
    checked: list[_VerifiedClaim],
    ledger: Telemetry,
    progress: Progress,
) -> None:
    """Cross-claim contradiction pass over the survivors; conflicting pairs
    are demoted to hedge so the rewrite presents them as a conflict instead
    of stating both as fact."""
    survivors = [v for v in checked if v.action in ("keep", "correct")]
    if len(survivors) < 2:
        return
    progress("verify", f"consistency pass over {len(survivors)} claims")
    listing = "\n".join(
        f"[{i}] {verified.final_text()}" for i, verified in enumerate(survivors)
    )
    result = caller.structured(prompts.CONSISTENCY_SYSTEM, listing, ConsistencyModel)
    assert isinstance(result, ConsistencyModel)
    for conflict in result.conflicts:
        pair = [conflict.first, conflict.second]
        if any(not 0 <= i < len(survivors) for i in pair) or pair[0] == pair[1]:
            continue
        for i in pair:
            survivors[i].action = "hedge"
            survivors[i].conflict_reason = conflict.reason
        ledger.record(
            {
                "event": "consistency_conflict",
                "first": survivors[pair[0]].claim.text,
                "second": survivors[pair[1]].claim.text,
                "reason": conflict.reason,
            }
        )


def _rewrite_sections(
    caller: Caller,
    results: list[SubtaskResult],
    checked: list[_VerifiedClaim],
    progress: Progress,
) -> None:
    by_section: dict[int, list[_VerifiedClaim]] = {}
    for verified in checked:
        by_section.setdefault(verified.section_index, []).append(verified)
    for index, section_claims in by_section.items():
        if all(v.action == "keep" for v in section_claims):
            continue
        result = results[index]
        progress("verify", f"rewriting {result.subtask_id} from verdicts")
        lines = []
        for verified in section_claims:
            if verified.action == "keep":
                lines.append(f"- KEEP: {verified.claim.text}")
            elif verified.action == "correct":
                lines.append(
                    f"- CORRECT: {verified.claim.text} -> "
                    f"{verified.entail.corrected_claim.strip()}"
                )
            elif verified.action == "hedge":
                reason = verified.conflict_reason or "weak support"
                lines.append(f"- HEDGE ({reason}): {verified.claim.text}")
            else:
                lines.append(f"- DROP: {verified.claim.text}")
        user_prompt = (
            f"{result.findings_markdown}\n\nCLAIM ACTIONS:\n" + "\n".join(lines)
        )
        rewritten = caller.plain(prompts.REWRITE_SYSTEM, user_prompt).strip()
        if not rewritten or rewritten == NOTHING_VERIFIED:
            result.findings_markdown = ""
            result.failed = True
            result.failure_note = "no claims survived verification"
        else:
            result.findings_markdown = rewritten


SCOPE_SOURCES = 3
SCOPE_EXTRACT_CHAR_CAP = 2500


def verify_scope(
    caller: Caller,
    registry: SourceRegistry,
    scope_text: str,
    ledger: Telemetry,
    progress: Progress,
) -> str:
    """The framing layer is claims too: check the chosen scope statement
    against the fetched corpus and correct it when the sources contradict
    it. (A real run dropped 'released in 2017' from the body while the
    Scope header kept asserting a 2017 film the query never asked about.)"""
    lines = scope_text.splitlines()
    if not lines or not lines[0].strip():
        return scope_text
    chosen = lines[0].strip()
    sources = sorted(
        (s for s in registry.all() if s.id in registry.extracts),
        key=lambda s: len(registry.extracts[s.id]),
        reverse=True,
    )[:SCOPE_SOURCES]
    if not sources:
        return scope_text
    blocks = [
        prompts.wrap_source_block(
            source.id,
            source.url,
            registry.extracts[source.id][:SCOPE_EXTRACT_CHAR_CAP],
        )
        for source in sources
    ]
    result = caller.structured(
        prompts.ENTAIL_SYSTEM,
        f"CLAIM: {chosen}\n\n" + "\n\n".join(blocks),
        EntailmentModel,
    )
    assert isinstance(result, EntailmentModel)
    corrected = result.corrected_claim.strip()
    ledger.record(
        {
            "event": "scope_check",
            "scope": chosen,
            "verdict": result.verdict,
            "evidence": result.evidence,
            "corrected": corrected,
        }
    )
    if result.verdict == "contradicted" and corrected:
        progress("verify", "scope corrected against sources")
        return "\n".join([corrected] + lines[1:])
    return scope_text


def verify_text(
    caller: Caller,
    registry: SourceRegistry,
    tiers: dict[int, str],
    text: str,
    ledger: Telemetry,
    origin: str,
    progress: Progress,
) -> str:
    """Run the claim gate over a framing text (e.g. the executive summary):
    the body verifier is useless if synthesis can reintroduce dropped
    claims one stage later. Same extract/entail/rewrite machinery as the
    findings sections, without the consistency pass."""
    if not text.strip():
        return text
    claim_list = caller.structured(prompts.CLAIMS_SYSTEM, text, ClaimListModel)
    assert isinstance(claim_list, ClaimListModel)
    claims = [c for c in claim_list.claims if c.text.strip()]
    if not claims:
        return text
    progress("verify", f"checking {len(claims)} {origin} claims")
    lines = []
    all_keep = True
    for claim in claims:
        entail = _entail(caller, registry, claim)
        action = _claim_action(claim, entail, tiers)
        ledger.record(
            {
                "event": "claim",
                "subtask": origin,
                "text": claim.text,
                "type": claim.type,
                "source_ids": claim.source_ids,
                "verdict": entail.verdict,
                "evidence": entail.evidence,
                "corrected_claim": entail.corrected_claim,
                "conflict": "",
                "action": action,
            }
        )
        if action == "keep":
            lines.append(f"- KEEP: {claim.text}")
        elif action == "correct":
            all_keep = False
            lines.append(
                f"- CORRECT: {claim.text} -> {entail.corrected_claim.strip()}"
            )
        elif action == "hedge":
            all_keep = False
            lines.append(f"- HEDGE (weak support): {claim.text}")
        else:
            all_keep = False
            lines.append(f"- DROP: {claim.text}")
    if all_keep:
        return text
    rewritten = caller.plain(
        prompts.REWRITE_SYSTEM, f"{text}\n\nCLAIM ACTIONS:\n" + "\n".join(lines)
    ).strip()
    if not rewritten or rewritten == NOTHING_VERIFIED:
        return ""
    return rewritten


_BULLET_RE = re.compile(r"^\s*[-*]\s+")


def validate_open_questions(
    caller: Caller,
    verified_texts: list[str],
    open_questions_markdown: str,
    ledger: Telemetry,
    progress: Progress,
) -> str:
    """Drop or narrow open questions the verified claims already settle —
    an open question must be genuine, not manufactured doubt."""
    questions = [
        _BULLET_RE.sub("", line).strip()
        for line in open_questions_markdown.splitlines()
        if _BULLET_RE.match(line)
    ]
    if not questions:
        return open_questions_markdown
    claims_block = "\n".join(f"- {text}" for text in verified_texts)
    claims_block = claims_block[:OPENQ_CLAIMS_CHAR_CAP] or "- (none)"
    numbered = "\n".join(f"[{i}] {q}" for i, q in enumerate(questions))
    progress("verify", f"reviewing {len(questions)} open questions")
    review = caller.structured(
        prompts.OPENQ_REVIEW_SYSTEM,
        f"VERIFIED CLAIMS:\n{claims_block}\n\nOPEN QUESTIONS:\n{numbered}",
        OpenQuestionReview,
    )
    assert isinstance(review, OpenQuestionReview)
    actions = {d.index: d for d in review.decisions if 0 <= d.index < len(questions)}
    kept: list[str] = []
    for i, question in enumerate(questions):
        decision = actions.get(i)
        action = decision.action if decision else "keep"
        if action == "remove":
            pass
        elif action == "rewrite" and decision and decision.rewrite.strip():
            kept.append(decision.rewrite.strip())
        else:
            kept.append(question)
        ledger.record(
            {
                "event": "open_question",
                "text": question,
                "action": action,
                "rewrite": decision.rewrite if decision else "",
                "reason": decision.reason if decision else "",
            }
        )
    return "\n".join(f"- {q}" for q in kept)
