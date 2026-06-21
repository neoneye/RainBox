"""The Q&A overlay comes from the customize.dir setting: PII/persona entries
live in <customize.dir>/question_answer.jsonl and override base entries by
id. Loader-level tests — no embedding, no pgvector."""

import json
from pathlib import Path

import pytest

import db
import agents.query_kb_helpers as kb


@pytest.fixture()
def app_ctx():
    app = db.make_app()
    ctx = app.app_context()
    ctx.push()
    try:
        yield
    finally:
        db.db.session.rollback()
        ctx.pop()


@pytest.fixture()
def customize_dir(app_ctx, tmp_path, monkeypatch):
    """A tmp customizations dir wired into the setting; the app_setting row
    is reverted afterwards (set to None = unset).

    Note: set_setting COMMITS internally, so app_ctx's rollback cannot undo it —
    the explicit `finally: set_setting(None)` teardown is required; don't remove it."""
    monkeypatch.delenv("RAINBOX_CUSTOMIZE_DIR", raising=False)
    db.set_setting("customize.dir", str(tmp_path))
    try:
        yield tmp_path
    finally:
        db.set_setting("customize.dir", None)


def _write_overlay(dirpath: Path, entries: list[dict]) -> None:
    (dirpath / "question_answer.jsonl").write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n")


def test_overlay_path_from_setting(customize_dir):
    assert kb._overlay_path() == customize_dir / "question_answer.jsonl"


def test_overlay_path_unset_is_none(app_ctx, monkeypatch):
    monkeypatch.delenv("RAINBOX_CUSTOMIZE_DIR", raising=False)
    db.set_setting("customize.dir", None)
    assert kb._overlay_path() is None


def test_load_jsonl_merges_overlay_by_id(customize_dir):
    _write_overlay(customize_dir, [
        # overrides a real base entry
        {"id": "identity.builtwith", "kind": "static",
         "questions": ["What are you built with?"], "answer": "OVERLAY WINS"},
        # overlay-only entry
        {"id": "test.overlay_only", "kind": "static",
         "questions": ["overlay only?"], "answer": "yes"},
    ])
    by_id = {e["id"]: e for e in kb._load_jsonl()}
    assert by_id["identity.builtwith"]["answer"] == "OVERLAY WINS"
    assert by_id["test.overlay_only"]["answer"] == "yes"
    # stable base entries; if this fails, check data/question_answer.jsonl
    assert "project.rainbox" in by_id


def test_load_jsonl_without_overlay_is_base_only(customize_dir):
    # setting points at a dir with NO question_answer.jsonl → base only
    by_id = {e["id"]: e for e in kb._load_jsonl()}
    assert "test.overlay_only" not in by_id
    # stable base entries; if this fails, check data/question_answer.jsonl
    assert "identity.builtwith" in by_id
    assert by_id["identity.builtwith"]["answer"] != "OVERLAY WINS"


class _FakeIndex:
    """Records the documents that would have been embedded."""
    last_docs: list | None = None

    @classmethod
    def from_documents(cls, docs, storage_context=None, embed_model=None):
        cls.last_docs = list(docs)
        return cls()


def _wire_fakes(monkeypatch):
    """rebuild_kb without Ollama/pgvector: fake the index build, the vector
    store, and the table ops.

    # The fake _vector_store skips _lock — a deadlock regression from moving
    # the real _vector_store() call inside rebuild_kb's locked section would
    # pass here but hang in production. Keep the call-before-lock order
    # (see rebuild_kb's docstring).
    """
    truncated = {"n": 0}
    monkeypatch.setattr(kb, "VectorStoreIndex", _FakeIndex)
    monkeypatch.setattr(kb, "_vector_store", lambda: object())
    monkeypatch.setattr(kb, "_embed_model", lambda: object())
    monkeypatch.setattr(kb, "_truncate_table",
                        lambda: truncated.__setitem__("n", truncated["n"] + 1))
    return truncated


def test_rebuild_kb_resets_and_repopulates(customize_dir, monkeypatch):
    truncated = _wire_fakes(monkeypatch)
    _write_overlay(customize_dir, [
        {"id": "test.rebuild", "kind": "static",
         "questions": ["rebuild test?"], "answer": "fresh"},
    ])
    counts = kb.rebuild_kb()
    assert truncated["n"] == 1
    assert counts["entries"] == len(kb._load_jsonl())
    assert counts["documents"] == len(_FakeIndex.last_docs)
    # in-process registry rebuilt: the overlay entry is reachable
    assert kb._entries_by_id["test.rebuild"]["answer"] == "fresh"
    assert kb._alias_table.get("rebuild test") == "test.rebuild"
    assert kb._populated is True


def test_rebuild_kb_failure_propagates_and_next_call_heals(customize_dir, monkeypatch):
    _wire_fakes(monkeypatch)

    # Save the original classmethod before boom overwrites it on the class.
    _original_from_docs = _FakeIndex.__dict__["from_documents"]

    def boom(docs, storage_context=None, embed_model=None):
        raise RuntimeError("ollama down")

    monkeypatch.setattr(kb.VectorStoreIndex, "from_documents", boom)
    with pytest.raises(RuntimeError, match="ollama down"):
        kb.rebuild_kb()
    assert kb._populated is False  # left rebuildable

    # Restore _FakeIndex.from_documents before the heal call so kb.VectorStoreIndex
    # (still _FakeIndex) has its real classmethod again.
    monkeypatch.setattr(kb, "VectorStoreIndex", _FakeIndex)
    monkeypatch.setattr(kb.VectorStoreIndex, "from_documents", _original_from_docs)
    counts = kb.rebuild_kb()  # heals
    assert counts["documents"] > 0
    assert kb._populated is True


def test_load_jsonl_tags_source(customize_dir, monkeypatch):
    # base file (upstream) has one entry; overlay has another + an override.
    base = [{"id": "u1", "path": "p.u1", "kind": "static", "questions": ["qu"], "answer": "base-u1"},
            {"id": "shared", "path": "p.s", "kind": "static", "questions": ["qs"], "answer": "base-shared"}]
    monkeypatch.setattr(kb, "QA_JSONL_PATH", customize_dir / "base.jsonl")
    (customize_dir / "base.jsonl").write_text("\n".join(json.dumps(e) for e in base) + "\n")
    _write_overlay(customize_dir, [
        {"id": "o1", "path": "p.o1", "kind": "static", "questions": ["qo"], "answer": "overlay-o1"},
        {"id": "shared", "path": "p.s", "kind": "static", "questions": ["qs"], "answer": "overlay-shared"},
    ])
    by_id = {e["id"]: e for e in kb._load_jsonl()}
    assert by_id["u1"]["_source"] == "upstream"
    assert by_id["o1"]["_source"] == "user-overlay"
    assert by_id["shared"]["_source"] == "user-overlay"   # overlay overrides → its source wins
    assert by_id["shared"]["answer"] == "overlay-shared"
