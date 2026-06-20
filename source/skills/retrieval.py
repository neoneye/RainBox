"""Lexical retrieval over *active* skills, with retrieval telemetry.

v1 is deliberately lexical (token overlap against title, retrieval_tags,
headings, and first paragraph) — the same family as the current memory
retriever. Phase 3 upgrades facts and skills together to hybrid semantic
retrieval; do not build a second semantic retriever just for skills.

Only `active` skills are considered (the "candidates are inert" contract). The
telemetry mirrors memory retrieval: a `considered` event per ranked skill and an
`injected` event per skill that actually lands in the prompt block.
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import db
from skills.loader import _UNSET, Skill, load_skills

logger = logging.getLogger(__name__)

# Prompt-budget caps for the skills section (simple counts/chars, like PR 1-4).
MAX_SKILLS_INJECTED: int = 3
MAX_SKILL_BLOCK_CHARS: int = 2000

# Tiny stopword set so common words don't dominate the overlap score.
_STOPWORDS = frozenset({
    "the", "a", "an", "to", "of", "and", "or", "in", "on", "for", "is", "are",
    "do", "i", "how", "what", "with", "my", "me", "it", "this", "that", "you",
    "can", "about", "tell",
})


def _tokens(text: str) -> set[str]:
    return {
        t for t in re.split(r"[^a-z0-9]+", (text or "").lower())
        if len(t) > 1 and t not in _STOPWORDS
    }


def _headings(body: str) -> str:
    return " ".join(
        line.lstrip("#").strip()
        for line in body.splitlines()
        if line.strip().startswith("#")
    )


@dataclass(frozen=True)
class RetrievedSkill:
    id: str
    title: str
    body: str
    score: float
    reason: str = "lexical_overlap"


def retrieve_skills(
    query: str,
    *,
    base_dir: Path | None = None,
    overlay_dir=_UNSET,
    limit: int = MAX_SKILLS_INJECTED,
) -> list[RetrievedSkill]:
    """Active skills ranked by lexical overlap with `query` (best first). Empty
    when the query has no usable tokens or nothing overlaps."""
    query_tokens = _tokens(query)
    if not query_tokens:
        return []
    skills = [s for s in load_skills(base_dir, overlay_dir) if s.status == "active"]
    scored: list[tuple[int, Skill]] = []
    for s in skills:
        hay = _tokens(
            " ".join([s.title, " ".join(s.retrieval_tags), s.first_paragraph,
                      _headings(s.body)])
        )
        overlap = len(query_tokens & hay)
        if overlap > 0:
            scored.append((overlap, s))
    # Best overlap first; id as a stable tiebreak.
    scored.sort(key=lambda pair: (-pair[0], pair[1].id))
    return [
        RetrievedSkill(id=s.id, title=s.title, body=s.body, score=float(overlap))
        for overlap, s in scored[:limit]
    ]


def format_skill_context(skills: list[RetrievedSkill]) -> str:
    """Render the injected skills as a prompt block, or "" when none."""
    if not skills:
        return ""
    parts = ["Relevant skills (procedural guidance):"]
    for s in skills:
        parts.append(f"## {s.title}\n{s.body}")
    return "\n\n".join(parts)


def build_skill_block(
    query: str,
    *,
    room_uuid: UUID | None = None,
    agent_uuid: UUID | None = None,
    journal_id: int | None = None,
    base_dir: Path | None = None,
    overlay_dir=_UNSET,
    limit: int = MAX_SKILLS_INJECTED,
) -> tuple[str, list[RetrievedSkill]]:
    """Retrieve active skills for `query`, record telemetry, and render the block
    under the char budget. Returns (block_text, injected_skills).

    Every retrieved skill is recorded `considered`; those that fit the budget and
    enter the block are also recorded `injected`.
    """
    retrieved = retrieve_skills(
        query, base_dir=base_dir, overlay_dir=overlay_dir, limit=limit
    )
    if not retrieved:
        return "", []

    injected: list[RetrievedSkill] = []
    block = ""
    for rank, rs in enumerate(retrieved):
        _record(rs, "considered", rank, query, room_uuid, agent_uuid, journal_id)
        candidate_block = format_skill_context([*injected, rs])
        if len(candidate_block) <= MAX_SKILL_BLOCK_CHARS:
            injected.append(rs)
            block = candidate_block

    for rank, rs in enumerate(injected):
        _record(rs, "injected", rank, query, room_uuid, agent_uuid, journal_id)

    return block, injected


def _record(
    rs: RetrievedSkill,
    stage: str,
    rank: int,
    query: str,
    room_uuid: UUID | None,
    agent_uuid: UUID | None,
    journal_id: int | None,
) -> None:
    try:
        db.record_retrieval_event(
            target_type="skill",
            target_id=rs.id,
            stage=stage,
            query=query,
            room_uuid=room_uuid,
            agent_uuid=agent_uuid,
            journal_id=journal_id,
            source="skills.retrieval",
            retrieval_rank=rank,
            retrieval_score=rs.score,
        )
    except Exception:  # telemetry must never break a turn
        logger.warning("skills: failed to record retrieval event for %s", rs.id)
