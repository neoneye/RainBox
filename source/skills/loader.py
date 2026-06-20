"""Load procedural skills from markdown files with YAML frontmatter.

Layout: base skills ship in `data/skills/*.md`; the operator overlay lives in
`<customize.dir>/skills/*.md` (resolved from the `customize.dir` setting, same
pattern as the Q&A overlay). The loader is robust — a malformed or conflicting
skill is skipped with a warning rather than breaking a live assistant turn.

Resolution rules (docs/proposals/2026-06-19-improvements-v2.md, "Draft: skills
metadata and dedup"):

1. Load base, then overlay.
2. id is a lowercase slug; ids with path separators are rejected.
3. Overlay wins over base for the same id.
4. A `rejected` overlay with the same id suppresses the base skill.
5. `candidate` skills load but are never injected (the retrieval layer filters).
6. `supersedes` hides the predecessor only when the successor is `active`.
7. A `supersedes` cycle invalidates every skill involved.
8. Duplicate ids inside one directory are an error — neither is chosen.
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml

import db

logger = logging.getLogger(__name__)

# A skill id is a lowercase kebab slug (matches the loader's id rules).
_SKILL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# Base skills that ship with rainbox.
SKILLS_DIR: Path = Path(__file__).resolve().parent.parent / "data" / "skills"

_VALID_STATUS = frozenset({"candidate", "active", "superseded", "rejected"})

# Sentinel so an explicit `overlay_dir=None` means "no overlay" while an omitted
# argument resolves the customize.dir overlay (which needs an app context).
_UNSET = object()


@dataclass(frozen=True)
class Skill:
    id: str
    status: str
    created_by: str
    title: str
    body: str
    first_paragraph: str
    retrieval_tags: list[str] = field(default_factory=list)
    supersedes: str | None = None
    source_journal_id: UUID | None = None
    source_step_id: int | None = None
    updated_at: str | None = None
    origin: str = "base"  # "base" | "overlay"
    source_path: str = ""


def _overlay_dir() -> Path | None:
    """The operator's skills overlay dir, from the customize.dir setting."""
    value = db.get_setting("customize.dir")
    if not value:
        return None
    return Path(str(value)) / "skills"


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """Return (frontmatter dict, body). A file without a leading `---` block has
    empty frontmatter and the whole text as body."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("\n", 1)
    rest = parts[1] if len(parts) > 1 else ""
    end = rest.find("\n---")
    if end == -1:
        return {}, text
    fm_text = rest[:end]
    body = rest[end + len("\n---"):].lstrip("\n")
    data = yaml.safe_load(fm_text) or {}
    if not isinstance(data, dict):
        return {}, body
    return data, body


def _title_and_first_paragraph(body: str) -> tuple[str, str]:
    title = ""
    paragraph_lines: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not title and stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            continue
        if title:
            if stripped:
                paragraph_lines.append(stripped)
            elif paragraph_lines:
                break
    return title, " ".join(paragraph_lines)


def _parse_skill(path: Path, origin: str) -> Skill | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("skills: cannot read %s: %s", path, e)
        return None
    try:
        fm, body = _split_frontmatter(text)
    except yaml.YAMLError as e:
        logger.warning("skills: bad frontmatter in %s: %s", path, e)
        return None

    raw_id = str(fm.get("id", "")).strip().lower()
    if not raw_id:
        logger.warning("skills: %s has no id; skipping", path)
        return None
    if "/" in raw_id or "\\" in raw_id:
        logger.warning("skills: id %r in %s has a path separator; skipping", raw_id, path)
        return None

    status = str(fm.get("status", "candidate")).strip().lower()
    if status not in _VALID_STATUS:
        logger.warning("skills: id %r has invalid status %r; skipping", raw_id, status)
        return None

    tags = fm.get("retrieval_tags") or []
    if not isinstance(tags, list):
        tags = []
    title, first_paragraph = _title_and_first_paragraph(body)
    supersedes = fm.get("supersedes")
    return Skill(
        id=raw_id,
        status=status,
        created_by=str(fm.get("created_by", "human")).strip().lower(),
        title=title or raw_id,
        body=body.strip(),
        first_paragraph=first_paragraph,
        retrieval_tags=[str(t).strip().lower() for t in tags],
        supersedes=str(supersedes).strip().lower() if supersedes else None,
        source_journal_id=fm.get("source_journal_id"),
        source_step_id=fm.get("source_step_id"),
        updated_at=str(fm["updated_at"]) if fm.get("updated_at") else None,
        origin=origin,
        source_path=str(path),
    )


def _load_dir(directory: Path | None, origin: str) -> list[Skill]:
    """Parse one directory's *.md skills, dropping same-directory id collisions
    (an error, not last-write-wins)."""
    if directory is None or not directory.is_dir():
        return []
    by_id: dict[str, Skill] = {}
    collisions: set[str] = set()
    for path in sorted(directory.glob("*.md")):
        skill = _parse_skill(path, origin)
        if skill is None:
            continue
        if skill.id in by_id:
            logger.warning(
                "skills: duplicate id %r in %s; dropping both", skill.id, directory
            )
            collisions.add(skill.id)
            continue
        by_id[skill.id] = skill
    for dup in collisions:
        by_id.pop(dup, None)
    return list(by_id.values())


def load_skills(
    base_dir: Path | None = None,
    overlay_dir=_UNSET,
) -> list[Skill]:
    """Load skills from base + overlay and apply the resolution rules. Returns
    every surviving skill (any status); the retrieval layer filters to active.

    `base_dir` defaults to the shipped skills dir. `overlay_dir` defaults to the
    customize.dir overlay; pass `overlay_dir=None` for no overlay (tests).
    """
    if base_dir is None:
        base_dir = SKILLS_DIR
    if overlay_dir is _UNSET:
        overlay_dir = _overlay_dir()

    base = _load_dir(base_dir, "base")
    overlay = _load_dir(overlay_dir, "overlay")

    # Overlay wins over base for the same id (rule 3); a rejected overlay still
    # replaces the base entry, which suppresses it (rule 4).
    merged: dict[str, Skill] = {s.id: s for s in base}
    for s in overlay:
        merged[s.id] = s

    # supersedes cycles invalidate every involved skill (rule 7).
    in_cycle = _ids_in_supersedes_cycle(merged)
    for bad in in_cycle:
        logger.warning("skills: id %r is in a supersedes cycle; skipping", bad)
        merged.pop(bad, None)

    # An active successor hides its predecessor (rule 6).
    superseded: set[str] = set()
    for s in merged.values():
        if s.status == "active" and s.supersedes:
            superseded.add(s.supersedes)

    result = [s for s in merged.values() if s.id not in superseded]
    # A rejected skill is never usable; drop it from the returned set so callers
    # never see it as available (its only job was to suppress a base id above).
    return [s for s in result if s.status != "rejected"]


def lint_skills(base_dir: Path | None = None, overlay_dir=_UNSET) -> list[str]:
    """Paths of *.md skill files that fail to parse (invalid metadata) — for
    `rainbox doctor`. Reuses the loader's own parser."""
    if base_dir is None:
        base_dir = SKILLS_DIR
    if overlay_dir is _UNSET:
        overlay_dir = _overlay_dir()
    bad: list[str] = []
    for d in (base_dir, overlay_dir):
        if d is None or not Path(d).is_dir():
            continue
        for p in sorted(Path(d).glob("*.md")):
            if _parse_skill(p, "lint") is None:
                bad.append(str(p))
    return bad


def _skill_file(skills_dir: Path, skill_id: str) -> Path:
    return skills_dir / f"{skill_id}.md"


def write_candidate_skill(
    *, skill_id: str, title: str, body: str, created_by: str = "assistant",
    retrieval_tags: list[str] | None = None, source_journal_id: object = None,
    source_step_id: int | None = None, skills_dir: Path | None = None,
) -> Path | None:
    """Write an inert candidate skill file to the overlay (or `skills_dir`).
    Returns the path, or None if the overlay is unconfigured, the id is not a
    clean slug, or a skill with that id already exists (never overwrite)."""
    if skills_dir is None:
        skills_dir = _overlay_dir()
    if skills_dir is None:
        return None
    skill_id = skill_id.strip().lower()
    if not _SKILL_ID_RE.match(skill_id):
        return None
    skills_dir.mkdir(parents=True, exist_ok=True)
    path = _skill_file(skills_dir, skill_id)
    if path.exists():
        return None
    fm: dict[str, Any] = {
        "id": skill_id, "status": "candidate", "created_by": created_by,
        "retrieval_tags": [t.strip().lower() for t in (retrieval_tags or []) if t.strip()],
    }
    if source_journal_id is not None:
        fm["source_journal_id"] = str(source_journal_id)
    if source_step_id is not None:
        fm["source_step_id"] = source_step_id
    front = yaml.safe_dump(fm, sort_keys=False).strip()
    path.write_text(f"---\n{front}\n---\n# {title}\n\n{body}\n", encoding="utf-8")
    return path


def set_skill_status(
    skill_id: str, status: str, *, if_current: str | None = None,
    skills_dir: Path | None = None,
) -> bool:
    """Rewrite a skill file's frontmatter status (e.g. candidate -> active).
    False if the overlay/file is missing, the status is invalid, or `if_current`
    is given and the file's current status doesn't match (so e.g. activation only
    promotes a genuine candidate, not a rejected/superseded/already-active skill)."""
    if status not in _VALID_STATUS:
        return False
    if skills_dir is None:
        skills_dir = _overlay_dir()
    if skills_dir is None:
        return False
    path = _skill_file(skills_dir, skill_id.strip().lower())
    if not path.exists():
        return False
    fm, body = _split_frontmatter(path.read_text(encoding="utf-8"))
    if if_current is not None and str(fm.get("status", "")).strip().lower() != if_current:
        return False
    fm["status"] = status
    front = yaml.safe_dump(fm, sort_keys=False).strip()
    path.write_text(f"---\n{front}\n---\n{body}\n", encoding="utf-8")
    return True


def delete_skill_file(skill_id: str, *, skills_dir: Path | None = None) -> bool:
    """Delete a skill file (the undo-inverse of write_candidate_skill)."""
    if skills_dir is None:
        skills_dir = _overlay_dir()
    if skills_dir is None:
        return False
    path = _skill_file(skills_dir, skill_id.strip().lower())
    if not path.exists():
        return False
    path.unlink()
    return True


def _ids_in_supersedes_cycle(skills: dict[str, Skill]) -> set[str]:
    """Ids that participate in a `supersedes` cycle."""
    bad: set[str] = set()
    for start in skills:
        seen: list[str] = []
        cur: str | None = start
        while cur is not None and cur in skills:
            if cur in seen:
                bad.update(seen[seen.index(cur):])
                break
            seen.append(cur)
            cur = skills[cur].supersedes
    return bad
