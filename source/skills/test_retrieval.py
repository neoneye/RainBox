"""Tests for skills retrieval: active-only lexical matching, block rendering,
and retrieval telemetry. The "candidates are inert" contract is enforced here —
a candidate skill is never retrieved or injected.
"""

from uuid import uuid4

import pytest

import db
from db import RetrievalEvent
from skills.retrieval import build_skill_block, format_skill_context, retrieve_skills


def _write(d, name, frontmatter, body):
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(f"---\n{frontmatter}\n---\n{body}\n", encoding="utf-8")


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    try:
        yield app
    finally:
        db.db.session.rollback()
        ctx.pop()


def test_retrieve_returns_only_active_skills(tmp_path):
    _write(
        tmp_path, "active.md",
        "id: git-help\nstatus: active\ncreated_by: human\nretrieval_tags: [git, status]",
        "# Git help\n\nHow to inspect git repository status.",
    )
    _write(
        tmp_path, "candidate.md",
        "id: git-candidate\nstatus: candidate\ncreated_by: assistant\nretrieval_tags: [git, status]",
        "# Git candidate\n\nUnreviewed git status guidance.",
    )
    out = retrieve_skills("how do I check git status", base_dir=tmp_path, overlay_dir=None)
    ids = [r.id for r in out]
    assert "git-help" in ids
    assert "git-candidate" not in ids  # candidates are inert


def test_ranks_by_lexical_overlap(tmp_path):
    _write(
        tmp_path, "a.md",
        "id: strong\nstatus: active\ncreated_by: human\nretrieval_tags: [kanban, board, card]",
        "# Kanban board help\n\nReading kanban boards and cards.",
    )
    _write(
        tmp_path, "b.md",
        "id: weak\nstatus: active\ncreated_by: human\nretrieval_tags: [git]",
        "# Git help\n\nGit things.",
    )
    out = retrieve_skills("read the kanban board cards", base_dir=tmp_path, overlay_dir=None)
    assert out[0].id == "strong"


def test_no_query_tokens_returns_empty(tmp_path):
    _write(tmp_path, "a.md", "id: x\nstatus: active\ncreated_by: human", "# X\n\nbody")
    assert retrieve_skills("", base_dir=tmp_path, overlay_dir=None) == []


def test_format_block_contains_title_and_body(tmp_path):
    _write(
        tmp_path, "a.md",
        "id: greet\nstatus: active\ncreated_by: human\nretrieval_tags: [hello]",
        "# How to greet\n\nSay hello warmly.",
    )
    out = retrieve_skills("hello greet", base_dir=tmp_path, overlay_dir=None)
    block = format_skill_context(out)
    assert "How to greet" in block
    assert "Say hello warmly" in block


def test_build_skill_block_records_considered_and_injected(app_ctx, tmp_path):
    _write(
        tmp_path, "a.md",
        "id: tele-skill\nstatus: active\ncreated_by: human\nretrieval_tags: [widget]",
        "# Widget help\n\nAll about widgets.",
    )
    room, agent = uuid4(), uuid4()
    block, injected = build_skill_block(
        "tell me about widget", room_uuid=room, agent_uuid=agent, journal_id=uuid4(),
        base_dir=tmp_path, overlay_dir=None,
    )
    try:
        assert "Widget help" in block
        assert [s.id for s in injected] == ["tele-skill"]
        events = (
            db.db.session.query(RetrievalEvent)
            .filter(RetrievalEvent.target_type == "skill",
                    RetrievalEvent.target_id == "tele-skill")
            .all()
        )
        stages = {e.stage for e in events}
        assert "considered" in stages
        assert "injected" in stages
    finally:
        db.db.session.query(RetrievalEvent).filter(
            RetrievalEvent.target_id == "tele-skill"
        ).delete()
        db.db.session.commit()


def test_build_skill_block_excludes_candidates(app_ctx, tmp_path):
    """A candidate skill that matches strongly must never enter the block."""
    _write(
        tmp_path, "cand.md",
        "id: cand-skill\nstatus: candidate\ncreated_by: assistant\nretrieval_tags: [zebra]",
        "# Zebra candidate\n\nUnreviewed zebra guidance.",
    )
    room, agent = uuid4(), uuid4()
    block, injected = build_skill_block(
        "tell me about zebra", room_uuid=room, agent_uuid=agent, journal_id=uuid4(),
        base_dir=tmp_path, overlay_dir=None,
    )
    assert block == ""
    assert injected == []
