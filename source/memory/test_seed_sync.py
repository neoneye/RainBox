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


# --- integration: throwaway pgvector table + fake embedder ---------------------

import json
import os
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
import sqlalchemy as sa
from llama_index.vector_stores.postgres import PGVectorStore
from psycopg import sql as psql

import db


def _fake_vector(text: str) -> list[float]:
    v = int(hashlib.sha256(text.encode()).hexdigest()[:8], 16) % 997 / 997.0
    return [v] * 768


@pytest.fixture
def sync_env(tmp_path, monkeypatch):
    """Isolated JSONL file + pgvector table + app context + fake embedder.
    `calls` records every _embed_texts batch, so a test can assert exactly
    which question strings paid an embedding call."""
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    table = f"seed_sync_test_{uuid4().hex[:8]}"
    url = sa.engine.url.make_url(os.environ["DATABASE_URL"])
    vs = PGVectorStore.from_params(
        database=url.database, host=url.host or "127.0.0.1",
        port=str(url.port or 5432), user=url.username or "",
        password=url.password or "", table_name=table, embed_dim=768,
    )
    monkeypatch.setattr(seed_memory, "QA_FULL_TABLE", f"data_{table}")
    monkeypatch.setattr(seed_memory, "_vs", vs)
    path = tmp_path / "qa.jsonl"
    monkeypatch.setattr(seed_memory, "QA_JSONL_PATH", path)
    monkeypatch.setattr(seed_memory, "_overlay_path", lambda: None)
    monkeypatch.setattr(seed_memory, "_populated", False)
    monkeypatch.setattr(seed_memory, "_entries_by_id", {})
    monkeypatch.setattr(seed_memory, "_alias_table", {})
    monkeypatch.setattr(seed_memory, "_sync_snapshot", None)
    calls: list[list[str]] = []

    def fake_embed(texts):
        calls.append(list(texts))
        return [_fake_vector(t) for t in texts]

    monkeypatch.setattr(seed_memory, "_embed_texts", fake_embed)
    env = SimpleNamespace(path=path, table=f"data_{table}", vs=vs, calls=calls)
    # sync_kb stamps qa.facts_invalidated_at on change; restore it so tests in
    # other modules (the assistant's re-check-facts notice) don't see our stamp.
    prior_stamp = db.get_setting("qa.facts_invalidated_at")
    try:
        yield env
    finally:
        db.db.session.rollback()
        db.set_setting("qa.facts_invalidated_at", prior_stamp)
        with psycopg.connect(db.psycopg_dsn(), autocommit=True) as c, c.cursor() as cur:
            cur.execute(psql.SQL("DROP TABLE IF EXISTS {}").format(
                psql.Identifier(f"data_{table}")))
        ctx.pop()


def _sql(env, query, params=()):
    with psycopg.connect(db.psycopg_dsn(), autocommit=True) as c, c.cursor() as cur:
        cur.execute(psql.SQL(query).format(psql.Identifier(env.table)), params)
        return cur.fetchall() if cur.description else None


def test_table_stamps_empty_when_table_missing(sync_env):
    assert seed_memory._table_stamps() == {}


# --- sync_kb ---------------------------------------------------------------


def _entry_line(qa_id, questions, answer, shield=None, kind="static", handler=None):
    d = {"id": qa_id, "kind": kind, "questions": questions}
    if kind == "static":
        d["answer"] = answer
    else:
        d["handler"] = handler
    if shield:
        d["shield"] = shield
    return json.dumps(d)


def _meta_rows(env, qa_id):
    rows = _sql(env,
                "SELECT metadata_, embedding::text FROM {} WHERE metadata_->>'qa_id' = %s",
                (qa_id,))
    return [(m if isinstance(m, dict) else json.loads(m), e) for m, e in rows]


def test_sync_cold_start_populates_everything(sync_env):
    _write(sync_env.path, [
        _entry_line("a", ["what is a?", "tell me about a"], "answer a"),
        _entry_line("b", ["what is b?"], "answer b"),
    ])
    counts = seed_memory.sync_kb()
    assert counts == {"unchanged": 0, "updated": 0, "embedded": 2, "deleted": 0}
    assert sum(len(c) for c in sync_env.calls) == 3
    assert len(_meta_rows(sync_env, "a")) == 2 and len(_meta_rows(sync_env, "b")) == 1


def test_sync_noop_second_run_embeds_nothing(sync_env):
    _write(sync_env.path, [_entry_line("a", ["q1?"], "ans")])
    seed_memory.sync_kb()
    sync_env.calls.clear()
    counts = seed_memory.sync_kb()
    assert counts == {"unchanged": 1, "updated": 0, "embedded": 0, "deleted": 0}
    assert sync_env.calls == []


def test_answer_edit_is_metadata_only(sync_env):
    _write(sync_env.path, [_entry_line("a", ["q1?"], "old answer")])
    seed_memory.sync_kb()
    ((_, emb_before),) = _meta_rows(sync_env, "a")
    sync_env.calls.clear()
    _write(sync_env.path, [_entry_line("a", ["q1?"], "new answer")])
    counts = seed_memory.sync_kb()
    assert counts == {"unchanged": 0, "updated": 1, "embedded": 0, "deleted": 0}
    assert sync_env.calls == []                      # zero embed calls
    ((meta, emb_after),) = _meta_rows(sync_env, "a")
    assert emb_after == emb_before                   # vector untouched
    assert meta["answer"] == "new answer"            # SQL-visible metadata
    inner = json.loads(meta["_node_content"])
    assert inner["metadata"]["answer"] == "new answer"   # node-visible metadata


def test_shield_edit_is_metadata_only_and_sql_enforceable(sync_env):
    _write(sync_env.path, [_entry_line("a", ["q1?"], "ans")])
    seed_memory.sync_kb()
    sync_env.calls.clear()
    _write(sync_env.path, [_entry_line("a", ["q1?"], "ans", shield="alice.travel")])
    counts = seed_memory.sync_kb()
    assert counts["updated"] == 1 and sync_env.calls == []
    rows = _sql(sync_env,
                "SELECT metadata_->>'shield' FROM {} WHERE metadata_->>'qa_id' = %s",
                ("a",))
    assert rows == [("alice.travel",)]
    # removing the shield removes the SQL-visible key again
    _write(sync_env.path, [_entry_line("a", ["q1?"], "ans")])
    seed_memory.sync_kb()
    rows = _sql(sync_env,
                "SELECT metadata_->>'shield' FROM {} WHERE metadata_->>'qa_id' = %s",
                ("a",))
    assert rows == [(None,)]


def test_question_added_embeds_only_the_new_string(sync_env):
    _write(sync_env.path, [_entry_line("a", ["q1?", "q2?"], "ans")])
    seed_memory.sync_kb()
    before = {m["question"]: e for m, e in _meta_rows(sync_env, "a")}
    sync_env.calls.clear()
    _write(sync_env.path, [_entry_line("a", ["q1?", "q2?", "q3?"], "ans")])
    counts = seed_memory.sync_kb()
    assert counts["embedded"] == 1
    assert sync_env.calls == [["q3?"]]               # only the new question
    after = {m["question"]: e for m, e in _meta_rows(sync_env, "a")}
    assert len(after) == 3
    assert after["q1?"] == before["q1?"] and after["q2?"] == before["q2?"]


def test_deleted_entry_removes_its_nodes(sync_env):
    _write(sync_env.path, [_entry_line("a", ["q1?"], "x"), _entry_line("b", ["q2?"], "y")])
    seed_memory.sync_kb()
    _write(sync_env.path, [_entry_line("a", ["q1?"], "x")])
    counts = seed_memory.sync_kb()
    assert counts["deleted"] == 1
    assert _meta_rows(sync_env, "b") == []


def test_epoch_bump_reembeds_everything(sync_env, monkeypatch):
    _write(sync_env.path, [_entry_line("a", ["q1?"], "x"), _entry_line("b", ["q2?"], "y")])
    seed_memory.sync_kb()
    sync_env.calls.clear()
    monkeypatch.setattr(seed_memory, "KB_EPOCH", "newmodel|2")
    counts = seed_memory.sync_kb()
    assert counts["embedded"] == 2
    assert sum(len(c) for c in sync_env.calls) == 2


def test_failing_row_leaves_others_intact_and_retries(sync_env, monkeypatch):
    _write(sync_env.path, [_entry_line("ok", ["fine?"], "x"),
                           _entry_line("bad", ["boom?"], "y")])

    def flaky(texts):
        if any("boom" in t for t in texts):
            raise RuntimeError("ollama down")
        return [_fake_vector(t) for t in texts]

    monkeypatch.setattr(seed_memory, "_embed_texts", flaky)
    with pytest.raises(RuntimeError, match="1 row"):
        seed_memory.sync_kb()
    assert len(_meta_rows(sync_env, "ok")) == 1     # good row landed
    assert _meta_rows(sync_env, "bad") == []        # bad row still absent
    monkeypatch.setattr(seed_memory, "_embed_texts",
                        lambda ts: [_fake_vector(t) for t in ts])
    counts = seed_memory.sync_kb()                  # heals
    assert counts["embedded"] == 1 and counts["unchanged"] == 1
    assert len(_meta_rows(sync_env, "bad")) == 1


def test_legacy_unstamped_rows_get_one_full_reembed(sync_env):
    _write(sync_env.path, [_entry_line("a", ["q1?"], "x")])
    seed_memory.sync_kb()
    _sql(sync_env,
         "UPDATE {} SET metadata_ = ((metadata_::jsonb - 'row_sha256' - 'kb_epoch')::json)")
    sync_env.calls.clear()
    counts = seed_memory.sync_kb()
    assert counts["embedded"] == 1                  # dirty -> re-synced
    counts = seed_memory.sync_kb()
    assert counts["unchanged"] == 1                 # then increments apply


def test_loader_validation_failure_leaves_table_untouched(sync_env):
    _write(sync_env.path, [_entry_line("a", ["q1?"], "x")])
    seed_memory.sync_kb()
    _write(sync_env.path, ['{"id": "a", "questions": ["q1?"], "answer": }'])
    with pytest.raises(ValueError):
        seed_memory.sync_kb()
    assert len(_meta_rows(sync_env, "a")) == 1


def test_dynamic_entry_handler_rename_is_metadata_only(sync_env):
    _write(sync_env.path,
           [_entry_line("d", ["when?"], None, kind="dynamic", handler="old_h")])
    seed_memory.sync_kb()
    sync_env.calls.clear()
    _write(sync_env.path,
           [_entry_line("d", ["when?"], None, kind="dynamic", handler="new_h")])
    counts = seed_memory.sync_kb()
    assert counts["updated"] == 1 and sync_env.calls == []
    ((meta, _),) = _meta_rows(sync_env, "d")
    assert meta["handler"] == "new_h"


def test_sync_refreshes_in_memory_registry(sync_env):
    _write(sync_env.path, [_entry_line("a", ["q1?"], "old")])
    seed_memory.sync_kb()
    assert seed_memory.get_entry("a")["answer"] == "old"
    _write(sync_env.path, [_entry_line("a", ["q1?"], "new")])
    seed_memory.sync_kb()
    assert seed_memory.get_entry("a")["answer"] == "new"


def test_entry_without_questions_does_not_loop_dirty(sync_env):
    """A question-less entry (e.g. a _meta marker in the overlay) yields no
    embeddable documents, so it can never place a stamp in the vector table.
    It must count as in-sync — not as perpetually 'new', which would mark
    every sync as changed and re-stamp qa.facts_invalidated_at on every
    assistant message."""
    db.set_setting("qa.facts_invalidated_at", None)
    db.db.session.commit()
    _write(sync_env.path, [_entry_line("real", ["q1?"], "x"),
                           _entry_line("marker", [], "meta")])
    counts = seed_memory.sync_kb()
    assert counts["embedded"] == 1                  # only the real entry
    stamp_after_first = db.get_setting("qa.facts_invalidated_at")
    sync_env.calls.clear()
    counts = seed_memory.sync_kb()
    assert counts == {"unchanged": 2, "updated": 0, "embedded": 0, "deleted": 0}
    assert sync_env.calls == []
    # a clean reconcile must NOT re-stamp (that is what posts the notice)
    assert db.get_setting("qa.facts_invalidated_at") == stamp_after_first
    # the entry is still served from the in-memory registry
    assert seed_memory.get_entry("marker") is not None


def test_questions_emptied_deletes_nodes_then_converges(sync_env):
    _write(sync_env.path, [_entry_line("a", ["q1?"], "x")])
    seed_memory.sync_kb()
    _write(sync_env.path, [_entry_line("a", [], "x")])
    counts = seed_memory.sync_kb()                  # nodes removed once
    assert _meta_rows(sync_env, "a") == []
    assert counts["unchanged"] == 0                 # this run did change things
    counts = seed_memory.sync_kb()                  # then it is in sync
    assert counts == {"unchanged": 1, "updated": 0, "embedded": 0, "deleted": 0}


# --- _ensure_populated: automatic reconcile behind a stat snapshot -----------


def test_ensure_populated_syncs_cold_and_skips_when_nothing_moved(sync_env):
    _write(sync_env.path, [_entry_line("a", ["q1?"], "x")])
    seed_memory._ensure_populated(sync_env.vs)
    assert len(_meta_rows(sync_env, "a")) == 1
    sync_env.calls.clear()
    seed_memory._ensure_populated(sync_env.vs)      # nothing moved -> stat() only
    assert sync_env.calls == []


def test_ensure_populated_picks_up_file_edit(sync_env):
    _write(sync_env.path, [_entry_line("a", ["q1?"], "x")])
    seed_memory._ensure_populated(sync_env.vs)
    _write(sync_env.path, [_entry_line("a", ["q1?"], "x"),
                           _entry_line("b", ["q2?"], "y")])
    seed_memory._ensure_populated(sync_env.vs)      # size changed -> syncs
    assert len(_meta_rows(sync_env, "b")) == 1


def test_ensure_populated_sync_failure_with_data_is_nonfatal(sync_env, monkeypatch):
    _write(sync_env.path, [_entry_line("a", ["q1?"], "x")])
    seed_memory._ensure_populated(sync_env.vs)
    _write(sync_env.path, [_entry_line("a", ["q1?"], "x"),
                           _entry_line("b", ["q2?"], "y")])

    def boom(_texts):
        raise RuntimeError("ollama down")

    monkeypatch.setattr(seed_memory, "_embed_texts", boom)
    seed_memory._ensure_populated(sync_env.vs)      # logged, not raised
    assert len(_meta_rows(sync_env, "a")) == 1      # existing rows intact
    monkeypatch.setattr(seed_memory, "_embed_texts",
                        lambda ts: [_fake_vector(t) for t in ts])
    seed_memory._ensure_populated(sync_env.vs)      # snapshot unsaved -> retries
    assert len(_meta_rows(sync_env, "b")) == 1


def test_ensure_populated_cold_failure_raises(sync_env, monkeypatch):
    _write(sync_env.path, [_entry_line("a", ["q1?"], "x")])

    def boom(_texts):
        raise RuntimeError("ollama down")

    monkeypatch.setattr(seed_memory, "_embed_texts", boom)
    with pytest.raises(RuntimeError):
        seed_memory._ensure_populated(sync_env.vs)  # empty table -> fatal


def test_ensure_populated_rebuild_env_forces_full_reembed(sync_env, monkeypatch):
    _write(sync_env.path, [_entry_line("a", ["q1?"], "x")])
    seed_memory._ensure_populated(sync_env.vs)
    sync_env.calls.clear()
    monkeypatch.setattr(seed_memory, "_populated", False)   # fresh process
    monkeypatch.setattr(seed_memory, "_sync_snapshot", None)
    monkeypatch.setenv(seed_memory.REBUILD_ENV, "1")
    seed_memory._ensure_populated(sync_env.vs)
    assert sum(len(c) for c in sync_env.calls) == 1         # re-embedded
