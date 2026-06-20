"""Tests for the procedural-skills loader: markdown + frontmatter, base/overlay
merge, id normalization, and the candidate/active/supersede/reject rules.

No DB and no LM Studio: the loader is pure filesystem + parsing. Tests write
skill files into tmp dirs and pass them explicitly.
"""

from pathlib import Path

from skills.loader import Skill, load_skills


def _write(d: Path, name: str, frontmatter: str, body: str) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(f"---\n{frontmatter}\n---\n{body}\n", encoding="utf-8")


def test_parses_frontmatter_title_and_first_paragraph(tmp_path):
    _write(
        tmp_path, "summarize.md",
        "id: summarize-pr-review\nstatus: active\ncreated_by: human\n"
        "retrieval_tags: [github, review, pull-request]",
        "# Summarize a PR review\n\nUse when the operator asks for a review "
        "summary. First list blocking findings.",
    )
    skills = load_skills(base_dir=tmp_path, overlay_dir=None)
    assert len(skills) == 1
    s = skills[0]
    assert isinstance(s, Skill)
    assert s.id == "summarize-pr-review"
    assert s.status == "active"
    assert s.created_by == "human"
    assert s.retrieval_tags == ["github", "review", "pull-request"]
    assert s.title == "Summarize a PR review"
    assert "blocking findings" in s.first_paragraph
    assert "blocking findings" in s.body


def test_candidate_is_loaded_but_flagged(tmp_path):
    _write(tmp_path, "c.md", "id: c\nstatus: candidate\ncreated_by: assistant", "# C\n\nbody")
    skills = load_skills(base_dir=tmp_path, overlay_dir=None)
    assert [s.status for s in skills] == ["candidate"]


def test_overlay_wins_over_base_for_same_id(tmp_path):
    base = tmp_path / "base"
    overlay = tmp_path / "overlay"
    _write(base, "x.md", "id: dup\nstatus: active\ncreated_by: human", "# Base\n\nbase body")
    _write(overlay, "x.md", "id: dup\nstatus: active\ncreated_by: human", "# Overlay\n\novl body")
    skills = load_skills(base_dir=base, overlay_dir=overlay)
    assert len(skills) == 1
    assert skills[0].title == "Overlay"
    assert skills[0].origin == "overlay"


def test_rejected_overlay_suppresses_base(tmp_path):
    base = tmp_path / "base"
    overlay = tmp_path / "overlay"
    _write(base, "x.md", "id: dup\nstatus: active\ncreated_by: human", "# Base\n\nbody")
    _write(overlay, "x.md", "id: dup\nstatus: rejected\ncreated_by: human", "# Rej\n\nbody")
    skills = load_skills(base_dir=base, overlay_dir=overlay)
    # The base skill is suppressed; the rejected overlay is not usable either.
    assert [s for s in skills if s.status == "active"] == []


def test_supersedes_hides_predecessor_when_successor_active(tmp_path):
    _write(tmp_path, "old.md", "id: old\nstatus: active\ncreated_by: human", "# Old\n\nbody")
    _write(
        tmp_path, "new.md",
        "id: new\nstatus: active\ncreated_by: human\nsupersedes: old",
        "# New\n\nbody",
    )
    ids = {s.id for s in load_skills(base_dir=tmp_path, overlay_dir=None) if s.status == "active"}
    assert ids == {"new"}


def test_supersedes_does_not_hide_when_successor_is_candidate(tmp_path):
    _write(tmp_path, "old.md", "id: old\nstatus: active\ncreated_by: human", "# Old\n\nbody")
    _write(
        tmp_path, "new.md",
        "id: new\nstatus: candidate\ncreated_by: assistant\nsupersedes: old",
        "# New\n\nbody",
    )
    active = {s.id for s in load_skills(base_dir=tmp_path, overlay_dir=None) if s.status == "active"}
    assert "old" in active  # an inert candidate cannot retire the predecessor


def test_id_with_path_separator_is_rejected(tmp_path):
    _write(tmp_path, "bad.md", "id: a/b\nstatus: active\ncreated_by: human", "# Bad\n\nbody")
    assert load_skills(base_dir=tmp_path, overlay_dir=None) == []


def test_duplicate_ids_in_same_dir_are_dropped_not_last_write_wins(tmp_path):
    _write(tmp_path, "one.md", "id: dup\nstatus: active\ncreated_by: human", "# One\n\nbody")
    _write(tmp_path, "two.md", "id: dup\nstatus: active\ncreated_by: human", "# Two\n\nbody")
    # A same-directory id collision is an error: neither is silently chosen.
    assert load_skills(base_dir=tmp_path, overlay_dir=None) == []


def test_supersedes_cycle_invalidates_involved_skills(tmp_path):
    _write(tmp_path, "a.md", "id: a\nstatus: active\ncreated_by: human\nsupersedes: b", "# A\n\nbody")
    _write(tmp_path, "b.md", "id: b\nstatus: active\ncreated_by: human\nsupersedes: a", "# B\n\nbody")
    ids = {s.id for s in load_skills(base_dir=tmp_path, overlay_dir=None)}
    assert "a" not in ids and "b" not in ids


def test_missing_base_dir_degrades_to_empty(tmp_path):
    assert load_skills(base_dir=tmp_path / "nope", overlay_dir=None) == []
