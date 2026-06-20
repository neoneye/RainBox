"""The user profile block: a query-independent digest of the operator's active
self-model, injected into the assistant prompt like the skills block.

Unlike ``query_memory`` (a model-chosen action with a query), the profile block
is assembled once per turn and is *always* present — it surfaces stable
preferences, project decisions, and operator facts regardless of whether the
model thinks to ask. Selection is deliberately non-vector: this is a small
stable digest (confidence + recency + kind preference), not a search.

Contracts honoured here:
- *Filter before rank*: reuse ``memory.retrieval.hard_filtered_claims`` so
  secret/expired/out-of-scope/candidate claims never enter selection.
- *Every influence is explainable*: each fact carries its claim uuid, and every
  considered/injected fact is recorded in retrieval telemetry.
- *Context is budgeted*: the block is built under an explicit char cap.
"""

import logging
from dataclasses import dataclass
from uuid import UUID

import db
from memory.retrieval import hard_filtered_claims

logger = logging.getLogger(__name__)

# Prompt-budget caps for the profile section (simple counts/chars, like the
# skills block).
MAX_PROFILE_BLOCK_CHARS: int = 1500
MAX_PROFILE_FACTS: int = 12

# Self-model kinds, best first. A claim whose kind is absent here (e.g.
# ``episode_summary``, ``procedure``) is not part of the operator profile and is
# excluded entirely — procedures are the skills layer's job, episodes are noise.
_KIND_PRIORITY: dict[str, int] = {
    "preference": 0,
    "project_decision": 1,
    "fact": 2,
}


def _is_profile_material(claim) -> bool:
    """Whether an already-hard-filtered claim belongs in the operator profile.

    Scope/sensitivity/expiry/status are already enforced by
    ``hard_filtered_claims`` (which also excludes project-scoped claims until a
    project key exists), so this only narrows by *content*:

    - Kind must be a self-model kind (``_KIND_PRIORITY``); ``episode_summary``
      and ``procedure`` are not operator profile.
    - A plain ``fact`` is only included when it has a subject (an operator-
      referring entity); a subject-less ambient fact is not worth injecting on
      every turn.
    """
    if claim.kind not in _KIND_PRIORITY:
        return False
    if claim.kind == "fact" and not (claim.subject or "").strip():
        return False
    return True


@dataclass(frozen=True)
class RetrievedProfileFact:
    uuid: UUID
    kind: str
    text: str
    confidence: float
    reason: str = "profile_digest"


def select_profile_facts(
    *,
    agent_uuid: UUID | None,
    room_uuid: UUID | None,
    limit: int = MAX_PROFILE_FACTS,
) -> list[RetrievedProfileFact]:
    """Active self-model claims for this agent/room, ranked for a profile digest:
    self-model kind first (preference > project_decision > fact), then confidence
    desc, then most-recent. Forbidden claims are removed before ranking by the
    shared hard filter. Secrets are never included in the profile."""
    candidates = hard_filtered_claims(
        include_secret=False, room_uuid=room_uuid, agent_uuid=agent_uuid
    )
    profile = [c for c in candidates if _is_profile_material(c)]
    profile.sort(
        key=lambda c: (
            _KIND_PRIORITY[c.kind],
            -float(c.confidence),
            -c.updated_at.timestamp() if hasattr(c.updated_at, "timestamp") else 0,
        )
    )
    return [
        RetrievedProfileFact(
            uuid=c.uuid, kind=c.kind, text=c.text, confidence=float(c.confidence)
        )
        for c in profile[:limit]
    ]


def format_profile_context(facts: list[RetrievedProfileFact]) -> str:
    """Render the profile facts as a prompt block, or "" when none — so callers
    can concatenate unconditionally without a stray header."""
    if not facts:
        return ""
    lines = ["About the operator (active profile):"]
    for f in facts:
        lines.append(f"- [{f.kind}] {f.text}")
    return "\n".join(lines)


def build_profile_block(
    *,
    agent_uuid: UUID | None,
    room_uuid: UUID | None,
    journal_id: UUID | None = None,
) -> tuple[str, list[RetrievedProfileFact]]:
    """Select profile facts, record telemetry, and render the block under the
    char budget. Returns (block_text, injected_facts).

    Every selected fact is recorded ``considered``; those that fit the budget and
    enter the block are also recorded ``injected``."""
    selected = select_profile_facts(agent_uuid=agent_uuid, room_uuid=room_uuid)
    if not selected:
        return "", []

    injected: list[RetrievedProfileFact] = []
    block = ""
    for rank, fact in enumerate(selected):
        _record(fact, "considered", rank, room_uuid, agent_uuid, journal_id)
        candidate_block = format_profile_context([*injected, fact])
        if len(candidate_block) <= MAX_PROFILE_BLOCK_CHARS:
            injected.append(fact)
            block = candidate_block

    for rank, fact in enumerate(injected):
        _record(fact, "injected", rank, room_uuid, agent_uuid, journal_id)

    return block, injected


def _record(
    fact: RetrievedProfileFact,
    stage: str,
    rank: int,
    room_uuid: UUID | None,
    agent_uuid: UUID | None,
    journal_id: UUID | None,
) -> None:
    try:
        db.record_retrieval_event(
            target_type="memory_claim",
            target_id=str(fact.uuid),
            stage=stage,
            room_uuid=room_uuid,
            agent_uuid=agent_uuid,
            journal_id=journal_id,
            source="user_profile.retrieval",
            retrieval_rank=rank,
            retrieval_score=round(float(fact.confidence), 6),
        )
    except Exception:  # telemetry must never break a turn
        logger.warning(
            "user_profile: failed to record retrieval event for %s", fact.uuid
        )
