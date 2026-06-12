"""File-backed persona loader.

A persona is behavior-as-data: a display name plus a system prompt, mapped to a
runnable `agent_uuid` (declared in `agent_config.py`). Phase 0 reads
`agent_profiles/personas.jsonl` and the referenced prompt files directly; a later
phase swaps this for a Postgres-backed query while keeping the same `Persona`
shape and `resolve_persona_for_agent` entry point.

This module deliberately has NO database import so it can be used from both the
agent processes and the seeding path without import cycles.
"""

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from uuid import UUID

_DIR = Path(__file__).resolve().parent / "agent_profiles"
PERSONAS_PATH = _DIR / "personas.jsonl"
CONVERSATIONS_DIR = _DIR / "conversations"


@dataclass(frozen=True)
class Persona:
    persona_id: UUID
    slug: str
    name: str
    description: str
    system_prompt: str          # already-read prompt body
    prompt_sha256: str          # provenance stamp recorded per turn/journal
    agent_kind: str             # Python agent class to run, e.g. chat_unstructured
    agent_role: str             # supervisor role name in agent_config, e.g. persona_egon
    agent_uuid: UUID            # runnable identity (inbox/journal/spawn)
    chat_user_uuid: UUID        # visible speaker (v1: == agent_uuid)


def _read_records() -> list[dict]:
    if not PERSONAS_PATH.exists():
        return []
    records: list[dict] = []
    for raw in PERSONAS_PATH.read_text().splitlines():
        line = raw.strip()
        if line:
            records.append(json.loads(line))
    return records


@lru_cache(maxsize=1)
def load_personas() -> dict[UUID, "Persona"]:
    """Parse personas.jsonl + prompt files, keyed by runnable agent_uuid.

    Cached per process. Raises on duplicate id/slug or a missing prompt file so a
    broken profile fails loudly at load, not at first turn. Disabled rows are
    skipped. Call `load_personas.cache_clear()` after editing files in a test."""
    personas: dict[UUID, Persona] = {}
    seen_ids: set[UUID] = set()
    seen_slugs: set[str] = set()
    for rec in _read_records():
        if not rec.get("enabled", True):
            continue
        persona_id = UUID(rec["id"])
        slug = rec["slug"]
        if persona_id in seen_ids:
            raise ValueError(f"duplicate persona id {persona_id}")
        if slug in seen_slugs:
            raise ValueError(f"duplicate persona slug {slug!r}")
        seen_ids.add(persona_id)
        seen_slugs.add(slug)
        body = (_DIR / rec["system_prompt_path"]).read_text()
        agent_uuid = UUID(rec["agent_uuid"])
        chat_user_uuid = UUID(rec.get("chat_user_uuid") or rec["agent_uuid"])
        personas[agent_uuid] = Persona(
            persona_id=persona_id,
            slug=slug,
            name=rec["name"],
            description=rec.get("description", ""),
            system_prompt=body,
            prompt_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            agent_kind=rec["agent_kind"],
            agent_role=rec["agent_role"],
            agent_uuid=agent_uuid,
            chat_user_uuid=chat_user_uuid,
        )
    return personas


def personas_by_slug() -> dict[str, "Persona"]:
    return {p.slug: p for p in load_personas().values()}


def resolve_persona_for_agent(agent_uuid: UUID) -> "Persona | None":
    """Return the persona bound to a runnable agent_uuid, or None for a
    non-persona agent. The chat agents call this to pick a system prompt."""
    if not isinstance(agent_uuid, UUID):
        agent_uuid = UUID(str(agent_uuid))
    return load_personas().get(agent_uuid)


def load_conversation_template(slug: str) -> dict:
    """Load one conversation template (agent_profiles/conversations/<slug>.json).
    Raises FileNotFoundError if the slug has no template."""
    return json.loads((CONVERSATIONS_DIR / f"{slug}.json").read_text())


def list_conversation_templates() -> list[dict]:
    """All conversation templates on disk, slug-sorted, as light summaries for a
    picker UI. Skips files that fail to parse rather than breaking the page."""
    if not CONVERSATIONS_DIR.exists():
        return []
    out: list[dict] = []
    for path in sorted(CONVERSATIONS_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (ValueError, OSError):
            continue
        policy = data.get("turn_policy", {}) or {}
        out.append({
            "slug": path.stem,
            "name": data.get("name", path.stem),
            "participants": [p.get("persona_slug") for p in data.get("participants", [])],
            "max_turns": policy.get("max_turns"),
        })
    return out


def validate_personas_against_config() -> list[str]:
    """Cross-check each persona's agent_role/agent_uuid against agent_config.
    Returns a list of human-readable problems (empty == valid). Imported lazily
    to keep this module free of an agent_config dependency at import time."""
    from agent_config import agent_config

    problems: list[str] = []
    for p in load_personas().values():
        entry = agent_config.get(p.agent_role)
        if entry is None:
            problems.append(f"persona {p.slug!r}: agent_role {p.agent_role!r} not in agent_config")
        elif entry["uuid"] != p.agent_uuid:
            problems.append(
                f"persona {p.slug!r}: agent_uuid {p.agent_uuid} != "
                f"agent_config[{p.agent_role!r}].uuid {entry['uuid']}"
            )
    return problems
