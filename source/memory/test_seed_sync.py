"""Incremental Q&A sync: row hashing, the differ, and the reconcile.

Unit tests need no DB; integration tests (the `sync_env` fixture) run against
a throwaway pgvector table in the test database with a fake embedder, in the
style of memory/test_embeddings.py.
"""
import hashlib

import memory.seed_memory as seed_memory


def _write(p, lines):
    p.write_text("".join(l + "\n" for l in lines))


# --- row hashing ---------------------------------------------------------------


def test_load_jsonl_carries_row_sha256_of_stripped_line(tmp_path, monkeypatch):
    line = '{"id": "a", "questions": ["ok"], "answer": "x"}'
    p = tmp_path / "qa.jsonl"
    _write(p, ["  " + line + "  "])   # loader hashes the STRIPPED line
    monkeypatch.setattr(seed_memory, "QA_JSONL_PATH", p)
    monkeypatch.setattr(seed_memory, "_overlay_path", lambda: None)
    (entry,) = seed_memory._load_jsonl()
    assert entry["_row_sha256"] == hashlib.sha256(line.encode("utf-8")).hexdigest()


def test_overlay_override_carries_winning_files_hash(tmp_path, monkeypatch):
    base_line = '{"id": "a", "questions": ["ok"], "answer": "base"}'
    over_line = '{"id": "a", "questions": ["ok"], "answer": "overlay"}'
    base = tmp_path / "base.jsonl"
    _write(base, [base_line])
    overlay = tmp_path / "overlay.jsonl"
    _write(overlay, [over_line])
    monkeypatch.setattr(seed_memory, "QA_JSONL_PATH", base)
    monkeypatch.setattr(seed_memory, "_overlay_path", lambda: overlay)
    (entry,) = seed_memory._load_jsonl()
    assert entry["_row_sha256"] == hashlib.sha256(over_line.encode("utf-8")).hexdigest()


def test_build_documents_stamps_row_hash_and_epoch():
    entries = [{"id": "s", "kind": "static", "questions": ["q?"],
                "answer": "a", "_row_sha256": "abc123"}]
    doc = seed_memory._build_documents(entries)[0]
    assert doc.metadata["row_sha256"] == "abc123"
    assert doc.metadata["kb_epoch"] == seed_memory.KB_EPOCH
    assert "row_sha256" in doc.excluded_embed_metadata_keys
    assert "kb_epoch" in doc.excluded_embed_metadata_keys


# --- the differ ------------------------------------------------------------


def _e(qa_id, sha="s1"):
    return {"id": qa_id, "questions": ["q?"], "answer": "a", "_row_sha256": sha}


def test_diff_rows_classifies_new_dirty_deleted_unchanged():
    ep = seed_memory.KB_EPOCH
    entries = [_e("new1"), _e("dirty1", sha="changed"), _e("same1")]
    stamps = {"dirty1": f"old|{ep}", "same1": f"s1|{ep}", "gone1": f"x|{ep}"}
    new, dirty, deleted, unchanged = seed_memory._diff_rows(entries, stamps)
    assert [e["id"] for e in new] == ["new1"]
    assert [e["id"] for e in dirty] == ["dirty1"]
    assert deleted == ["gone1"]
    assert unchanged == 1


def test_diff_rows_epoch_bump_dirties_everything(monkeypatch):
    entries = [_e("a"), _e("b")]
    stamps = {"a": f"s1|{seed_memory.KB_EPOCH}", "b": f"s1|{seed_memory.KB_EPOCH}"}
    monkeypatch.setattr(seed_memory, "KB_EPOCH", "other-model|9")
    new, dirty, deleted, unchanged = seed_memory._diff_rows(entries, stamps)
    assert [e["id"] for e in dirty] == ["a", "b"] and not new and unchanged == 0


def test_diff_rows_conflicting_stamp_is_dirty():
    # A qa_id whose nodes disagree (past partial write) maps to None -> dirty.
    entries = [_e("a")]
    new, dirty, deleted, unchanged = seed_memory._diff_rows(entries, {"a": None})
    assert [e["id"] for e in dirty] == ["a"]


def test_diff_rows_legacy_unstamped_row_is_dirty():
    # Pre-stamp tables yield "None|None" stamps -> everything dirty once.
    entries = [_e("a")]
    new, dirty, deleted, unchanged = seed_memory._diff_rows(entries, {"a": "None|None"})
    assert [e["id"] for e in dirty] == ["a"]
