"""Tests for `rainbox doctor` (tools.doctor). Model-free: the embedder is
injected so no live Ollama is needed."""

import pytest

import db
import skills
from tools.doctor import (
    Check,
    check_capabilities,
    check_embedder,
    check_model_groups,
    exit_code,
    format_checks,
    run_doctor,
)


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


def _write(d, name, text):
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text, encoding="utf-8")


def test_lint_detects_invalid_skill_file(tmp_path):
    _write(tmp_path, "good.md",
           "---\nid: good-skill\nstatus: active\ncreated_by: human\n---\n# Good\n\nbody")
    _write(tmp_path, "bad.md", "---\nstatus: active\n---\n# No id here\n\nbody")
    bad = skills.lint_skills(base_dir=tmp_path, overlay_dir=None)
    assert bad == [str(tmp_path / "bad.md")]


def test_embedder_ok_and_warn():
    ok = check_embedder(embed_fn=lambda _t: [0.1] * 768)
    assert ok.status == "ok" and "768" in ok.detail

    def boom(_t):
        raise RuntimeError("connection refused")

    warn = check_embedder(embed_fn=boom)
    assert warn.status == "warn" and "lexical-only" in warn.detail


def test_embedder_empty_vector_warns():
    assert check_embedder(embed_fn=lambda _t: []).status == "warn"


def test_capabilities_ok(app_ctx):
    c = check_capabilities()
    assert c.status == "ok"
    # at least the read-only + write capabilities are enabled.
    assert c.name == "capabilities" and "enabled" in c.detail


def test_model_groups_check_reflects_db(app_ctx):
    c = check_model_groups()
    assert c.name == "model_groups" and c.status in ("ok", "fail")


def test_run_doctor_covers_all_probes(app_ctx):
    checks = run_doctor(embed_fn=lambda _t: [0.1] * 768)
    names = {c.name for c in checks}
    assert {"capabilities", "model_groups", "embedder", "skills", "mcp"} <= names
    assert all(isinstance(c, Check) for c in checks)


def test_exit_code_and_format():
    assert exit_code([Check("a", "ok", "x"), Check("b", "warn", "y")]) == 0
    assert exit_code([Check("a", "ok", "x"), Check("b", "fail", "y")]) == 1
    out = format_checks([Check("a", "ok", "fine"), Check("b", "fail", "broken")])
    assert "✓ a: fine" in out and "✗ b: broken" in out
