# S3 — skill-candidate write family — design (2026-06-20)

**Status:** approved-direction, complete spec (decisions all made; implement
directly). Implements card **S3** of
[`../../proposals/2026-06-20-improvements-v3.md`](../../proposals/2026-06-20-improvements-v3.md):
the assistant can **propose a candidate skill** (inert) and **activate** one —
closing the skill half of v2's "memory *and* skill candidates" (the memory half,
`remember`/`activate_memory`, already shipped). Mirrors that pattern exactly.

## Decisions (made, with rationale)

- **`propose_skill` — log-and-undo.** A candidate skill is inert (never injected
  until active), so proposing one is low blast radius — mirrors `remember`. Undo
  deletes the just-written candidate file (mirrors `kanban_create`'s delete-inverse).
- **`activate_skill` — confirm-tier.** Activating a skill *steers future behavior*,
  so it needs operator approval — mirrors `activate_memory` (no dry-run; the
  preview is just "activate skill <id>").
- **`skill_delete` — internal (`prompt_exposed=False`).** The undo-inverse of
  `propose_skill`; the model can't invoke it directly (the `_validate_decision`
  guard from S2 covers this). Deletes the candidate file.
- **Candidates are written as overlay files.** Skills live in files; the writable
  location is `<customize.dir>/skills/` (the operator overlay). If `customize.dir`
  is unset there is no writable overlay → `propose_skill` fails cleanly. Base
  `data/skills/` (the shipped set) is never written.
- **Provenance** is recorded in frontmatter: `created_by: assistant`,
  `source_journal_id`, `source_step_id`, `status: candidate`.

## `skills/loader.py` — writer helpers

(Reuse `_VALID_STATUS`, `_split_frontmatter`, `_overlay_dir`.)

```python
import re
_SKILL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

def _skill_file(skills_dir: Path, skill_id: str) -> Path:
    return skills_dir / f"{skill_id}.md"

def write_candidate_skill(
    *, skill_id: str, title: str, body: str, created_by: str = "assistant",
    retrieval_tags: list[str] | None = None, source_journal_id=None,
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

def set_skill_status(skill_id: str, status: str, *, skills_dir: Path | None = None) -> bool:
    """Rewrite a skill file's frontmatter status (e.g. candidate -> active).
    False if the overlay/file is missing or the status is invalid."""
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
```

Export the three from `skills/__init__.py` (and `re`/`Any` are available in
loader; add imports if missing).

## `agents/assistant.py`

**Enum** (after `edit_file`):

```python
    PROPOSE_SKILL = "propose_skill"    # log-and-undo: write an inert candidate skill
    ACTIVATE_SKILL = "activate_skill"  # confirm-tier: activate a candidate skill
    SKILL_DELETE = "skill_delete"      # internal: propose_skill's undo inverse (not prompt-exposed)
```

**Actions:**

```python
def _action_propose_skill(ctx, args):
    """Log-and-undo write: write an inert candidate skill to the overlay. It is
    never injected until activated; undo deletes it."""
    skill_id = str(args.get("skill_id", "")).strip().lower()
    title = str(args.get("title", "")).strip()
    body = str(args.get("body", "")).strip()
    tags = [t for t in str(args.get("tags", "")).split(",") if t.strip()]
    path = skills.write_candidate_skill(
        skill_id=skill_id, title=title, body=body, created_by="assistant",
        retrieval_tags=tags, source_journal_id=ctx.journal_id,
        source_step_id=ctx.step_index,
    )
    if path is None:
        return AssistantObservation(
            ok=False,
            text="couldn't propose skill (no skills overlay configured, invalid id, "
                 "or that id already exists)",
        )
    return AssistantObservation(
        ok=True,
        text=f"Proposed candidate skill '{skill_id}' (inert until you activate it; reject to undo).",
        data={"skill_id": skill_id,
              "undo": {"capability": "skill_delete", "payload": {"skill_id": skill_id}}},
    )

def _action_activate_skill(ctx, args):
    """Confirm-tier write: activate a candidate skill so it can steer future turns."""
    skill_id = str(args.get("skill_id", "")).strip().lower()
    if not skills.set_skill_status(skill_id, "active"):
        return AssistantObservation(ok=False, text=f"no such candidate skill: {skill_id}")
    return AssistantObservation(
        ok=True, text=f"Activated skill '{skill_id}'.", data={"skill_id": skill_id})

def _action_delete_skill(ctx, args):
    """Internal: delete a skill file — propose_skill's undo inverse. Not
    prompt-exposed (reached only via undo_write_intent)."""
    skill_id = str(args.get("skill_id", "")).strip().lower()
    if not skills.delete_skill_file(skill_id):
        return AssistantObservation(ok=False, text=f"no such skill: {skill_id}")
    return AssistantObservation(
        ok=True, text=f"Deleted skill '{skill_id}'", data={"skill_id": skill_id})
```

**Registry entries:**

```python
    AssistantActionName.PROPOSE_SKILL: Capability(
        name=AssistantActionName.PROPOSE_SKILL, family="skill",
        description=('propose a reusable "how to" skill as an inert candidate '
                     '(never used until you activate it; reject to undo). args: '
                     '{"skill_id": "kebab-slug", "title": "...", "body": "markdown", '
                     'optional "tags": "a,b"}'),
        required_args=("skill_id", "title", "body"),
        optional_args=frozenset({"tags"}),
        action=_action_propose_skill, read=False, write=True, tier="log_and_undo",
    ),
    AssistantActionName.ACTIVATE_SKILL: Capability(
        name=AssistantActionName.ACTIVATE_SKILL, family="skill",
        description=('activate a candidate skill so it steers future answers; needs '
                     'your confirmation. args: {"skill_id": "..."}'),
        required_args=("skill_id",), action=_action_activate_skill,
        read=False, write=True, tier="confirm",
    ),
    AssistantActionName.SKILL_DELETE: Capability(
        name=AssistantActionName.SKILL_DELETE, family="skill",
        description="(internal) delete a skill file — propose_skill's undo inverse.",
        required_args=("skill_id",), action=_action_delete_skill,
        read=False, write=True, tier="log_and_undo", prompt_exposed=False,
    ),
```

(`skills` is already imported in assistant.py.)

## `agents/test_assistant_fakes.py`

Add `"propose_skill"`, `"activate_skill"`, `"skill_delete"` to the locked surface.

## Tests (TDD, model-free) — `agents/test_skill_candidates.py` (new)

Point the overlay at a tmp dir: `db.set_setting("customize.dir", str(tmp_path))`
in a fixture (restore the prior value in teardown). The action and
`build_skill_block` both resolve `<customize.dir>/skills/`.

1. **propose writes an inert candidate + returns delete-inverse:** action writes
   `<tmp>/skills/<id>.md` with `status: candidate`, `created_by: assistant`;
   `data["undo"]` = `{capability: skill_delete, payload: {skill_id}}`.
2. **candidate is inert; activation makes it injectable (the contract):** after
   propose, `build_skill_block(query matching the skill)` does NOT include it;
   after `activate_skill`, it DOES. (Phase 2 "candidates are inert" gate.)
3. **propose via loop + undo deletes the file.**
4. **activate is confirm-tier:** a scripted `activate_skill` only proposes a
   write intent (skill stays `candidate`); `execute_write_intent` flips it to
   `active`.
5. **model cannot invoke skill_delete** (validator guard) — file not deleted.
6. **bad id / duplicate / no-overlay rejected** (`ok=False`).
7. **capability flags:** propose=log_and_undo, activate=confirm, skill_delete
   `prompt_exposed=False`; surface lock updated.

## Done when

- The assistant can propose a candidate skill (inert) and, on operator
  confirmation, activate it; an unactivated assistant-written skill provably
  cannot influence a turn (test 2).
- Undo deletes a proposed candidate; the model cannot delete skills directly.
- Bad ids / duplicates / missing overlay fail cleanly with no file written.
- Model-free tests cover all of the above; full affected suite green.

## Out of scope (follow-ups)

- Editing/superseding an existing active skill (only propose-new + activate here).
- A skill-review UI surface (operator edits files / confirms via the write-intent
  endpoints today).
