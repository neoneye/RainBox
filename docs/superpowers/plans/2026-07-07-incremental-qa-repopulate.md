# Incremental Q&A Repopulate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the wipe-and-re-embed Q&A repopulate with a per-row SHA-256-stamped reconcile (`sync_kb()`), and make it run automatically from `_ensure_populated()` behind an mtime/size snapshot guard.

**Architecture:** Each embedded node's metadata gains `row_sha256` (hash of its source JSONL line) and `kb_epoch` (embed model + schema version). A differ classifies file rows as new/dirty/deleted/unchanged against one `SELECT DISTINCT` over the table. Dirty rows with an unchanged question set get an in-place metadata UPDATE (zero embed calls); changed questions re-embed only the new strings, reusing stored vectors for unchanged ones (insert-new-then-delete-old, so retrieval never sees an absent row). The `/settings` button switches to `sync_kb()`; a new "Rebuild (full)" button keeps TRUNCATE semantics; `_ensure_populated()` runs the sync per process guarded by a `stat()` snapshot.

**Tech Stack:** Python, psycopg3, llama-index `PGVectorStore`/`TextNode`, Flask, pytest against `rainbox_claude` (conftest-forced).

## Global Constraints

- Tests run against `rainbox_claude` only (conftest already forces this); integration tests must use a throwaway pgvector table (`data_seed_sync_test_<hex>`), never `data_seed_memory`.
- No PII in tests or fixtures — neutral placeholder entries only.
- The settings template is a NON-raw Python string: no backslash escape sequences in new inline JS.
- Docs describe current state only — no "changed from"/migration narration.
- Counts contract: `sync_kb()` returns `{"unchanged": int, "updated": int, "embedded": int, "deleted": int}` (all ROW counts; "updated" = metadata-only rows, "embedded" = rows that needed embed calls).
- Commit style: small `feat:`/`test:`/`docs:` commits straight to `main`.

---

### Task 1: Row hashing + epoch stamps in loader and documents

**Files:**
- Modify: `source/memory/seed_memory.py` (constants near line 50, `_load_jsonl` line 166, `_build_documents` line 275)
- Test: `source/memory/test_seed_sync.py` (new)

**Interfaces:**
- Produces: `KB_SCHEMA_VERSION: int`, `KB_EPOCH: str` (module constants); every entry from `_load_jsonl()` carries `_row_sha256` (hex sha256 of its stripped raw line); every doc from `_build_documents()` carries `row_sha256` + `kb_epoch` metadata.

- [x] **Step 1: Write failing tests** in `source/memory/test_seed_sync.py`:

```python
"""Incremental Q&A sync: row hashing, the differ, and the reconcile."""
import hashlib
import json

import memory.seed_memory as seed_memory


def _write(p, lines):
    p.write_text("".join(l + "\n" for l in lines))


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
    base = tmp_path / "base.jsonl"; _write(base, [base_line])
    overlay = tmp_path / "overlay.jsonl"; _write(overlay, [over_line])
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
```

- [x] **Step 2: Run** `pytest memory/test_seed_sync.py -v` — expect FAIL (`KeyError: '_row_sha256'`, `AttributeError: KB_EPOCH`).

- [x] **Step 3: Implement.** In `seed_memory.py` add `import hashlib`; after `EMBED_DIM`:

```python
# Bump when the node-metadata shape written by _build_documents changes
# (new/renamed keys). Folded into KB_EPOCH, so every stored row goes stale and
# re-embeds on the next sync — no manual full rebuild after an upgrade.
KB_SCHEMA_VERSION: int = 1
# Stored verbatim in every node's metadata next to row_sha256. A mismatch
# (embed-model swap or schema bump) marks the row dirty for sync_kb().
KB_EPOCH: str = f"{EMBED_MODEL_NAME}|{KB_SCHEMA_VERSION}"
```

In `_load_jsonl`, inside the `if entry_id:` block next to `entry["_source"] = source`:

```python
                entry["_row_sha256"] = hashlib.sha256(line.encode("utf-8")).hexdigest()
```

In `_build_documents`, after the `shield` block:

```python
            md["row_sha256"] = e.get("_row_sha256", "")
            md["kb_epoch"] = KB_EPOCH
```

- [x] **Step 4: Run** `pytest memory/test_seed_sync.py memory/test_seed_documents.py memory/test_seed_memory_errors.py -v` — expect PASS.

- [x] **Step 5: Commit** `feat(memory): stamp Q&A rows with source-line hash and kb epoch`

### Task 2: The pure differ

**Files:**
- Modify: `source/memory/seed_memory.py`
- Test: `source/memory/test_seed_sync.py`

**Interfaces:**
- Produces: `_entry_stamp(entry) -> str` (`"<row_sha256>|<KB_EPOCH>"`); `_diff_rows(entries, stamps) -> tuple[list, list, list, int]` = `(new_entries, dirty_entries, deleted_qa_ids, unchanged_count)`; `stamps: dict[str, str | None]` maps qa_id → stamp, `None` = conflicting stamps (force dirty).

- [x] **Step 1: Write failing tests** (append to `test_seed_sync.py`):

```python
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
    entries = [_e("a")]
    new, dirty, deleted, unchanged = seed_memory._diff_rows(entries, {"a": None})
    assert [e["id"] for e in dirty] == ["a"]


def test_diff_rows_legacy_unstamped_row_is_dirty():
    # Pre-stamp tables yield "None|None" stamps -> everything dirty once.
    entries = [_e("a")]
    new, dirty, deleted, unchanged = seed_memory._diff_rows(entries, {"a": "None|None"})
    assert [e["id"] for e in dirty] == ["a"]
```

- [x] **Step 2: Run** `pytest memory/test_seed_sync.py -v` — expect FAIL (no `_diff_rows`).

- [x] **Step 3: Implement** (new section in `seed_memory.py` after `rebuild_kb`):

```python
# --- Incremental sync ----------------------------------------------------------


def _entry_stamp(e: dict[str, Any]) -> str:
    """The stamp an entry's nodes must carry to count as up to date."""
    return f"{e.get('_row_sha256', '')}|{KB_EPOCH}"


def _diff_rows(
    entries: list[dict[str, Any]], stamps: dict[str, str | None],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str], int]:
    """Classify merged JSONL entries against the table's stamps: (new entries,
    dirty entries, deleted qa_ids, unchanged count). A `None` stamp means the
    row's nodes disagree (partial write) — always dirty."""
    new: list[dict[str, Any]] = []
    dirty: list[dict[str, Any]] = []
    unchanged = 0
    ids: set[str] = set()
    for e in entries:
        qa_id = e.get("id")
        if not qa_id:
            continue
        ids.add(qa_id)
        if qa_id not in stamps:
            new.append(e)
        elif stamps[qa_id] != _entry_stamp(e):
            dirty.append(e)
        else:
            unchanged += 1
    deleted = [qa_id for qa_id in stamps if qa_id not in ids]
    return new, dirty, deleted, unchanged
```

Note: `_entry_stamp` reads the module-global `KB_EPOCH` at call time (monkeypatchable).

- [x] **Step 4: Run** `pytest memory/test_seed_sync.py -v` — expect PASS.

- [x] **Step 5: Commit** `feat(memory): pure differ for incremental Q&A sync`

### Task 3: DB helpers — stamps query, node fetch, deletes, metadata update

**Files:**
- Modify: `source/memory/seed_memory.py`
- Test: `source/memory/test_seed_sync.py` (integration fixture + first DB tests)

**Interfaces:**
- Produces:
  - `_table_stamps() -> dict[str, str | None]` — one `SELECT DISTINCT`; `{}` when table missing.
  - `_row_nodes(qa_id) -> list[tuple[str, str, dict, list[float] | None]]` — `(node_id, question_text, metadata_dict, embedding_vector)`.
  - `_delete_nodes(node_ids: list[str])`, `_delete_qa_rows(qa_ids: list[str])`.
  - `_update_node_metadata(node_id: str, old_meta: dict, md: dict)` — rewrites top-level metadata keys AND the copy nested in `_node_content` (plus relationship metadata + excluded-key lists).
  - `_NODE_BOOKKEEPING_KEYS` tuple.

- [x] **Step 1: Write the integration fixture + failing tests** (append to `test_seed_sync.py`):

```python
# --- integration: throwaway pgvector table + fake embedder ---------------------

import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from llama_index.vector_stores.postgres import PGVectorStore

import db


def _fake_vector(text: str) -> list[float]:
    v = int(hashlib.sha256(text.encode()).hexdigest()[:8], 16) % 997 / 997.0
    return [v] * 768


@pytest.fixture
def sync_env(tmp_path, monkeypatch):
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
    yield env
    import psycopg
    from psycopg import sql as psql
    with psycopg.connect(db.psycopg_dsn(), autocommit=True) as c, c.cursor() as cur:
        cur.execute(psql.SQL("DROP TABLE IF EXISTS {}").format(psql.Identifier(f"data_{table}")))
    ctx.pop()


def _sql(env, query, params=()):
    import psycopg
    from psycopg import sql as psql
    with psycopg.connect(db.psycopg_dsn(), autocommit=True) as c, c.cursor() as cur:
        cur.execute(psql.SQL(query).format(psql.Identifier(env.table)), params)
        return cur.fetchall() if cur.description else None


def test_table_stamps_empty_when_table_missing(sync_env):
    assert seed_memory._table_stamps() == {}
```

- [x] **Step 2: Run** `pytest memory/test_seed_sync.py::test_table_stamps_empty_when_table_missing -v` — expect FAIL (no `_table_stamps`, no `_sync_snapshot`).

- [x] **Step 3: Implement** in `seed_memory.py` (same new section). Also add module global `_sync_snapshot: dict[str, tuple[int, int]] | None = None` next to `_populated`, and `from llama_index.core.schema import TextNode` import.

```python
# Metadata keys PGVectorStore's node serialization owns; the metadata-only
# update preserves them verbatim and replaces every other key with the row's
# fresh metadata.
_NODE_BOOKKEEPING_KEYS: tuple[str, ...] = (
    "_node_content", "_node_type", "document_id", "doc_id", "ref_doc_id",
)


def _table_exists(cur: Any) -> bool:
    cur.execute("SELECT to_regclass(%s)", (QA_FULL_TABLE,))
    row = cur.fetchone()
    return row is not None and row[0] is not None


def _table_stamps() -> dict[str, str | None]:
    """qa_id -> "row_sha256|kb_epoch" for every row in the vector table. A
    qa_id whose nodes disagree (a past partial write) maps to None so the
    differ treats it as dirty. {} when the table doesn't exist yet."""
    out: dict[str, str | None] = {}
    with psycopg.connect(db.psycopg_dsn(), autocommit=True) as c, c.cursor() as cur:
        if not _table_exists(cur):
            return out
        cur.execute(sql.SQL(
            "SELECT DISTINCT metadata_->>'qa_id', metadata_->>'row_sha256', "
            "metadata_->>'kb_epoch' FROM {}"
        ).format(sql.Identifier(QA_FULL_TABLE)))
        for qa_id, row_sha, epoch in cur.fetchall():
            if not qa_id:
                continue
            stamp = f"{row_sha}|{epoch}"
            out[qa_id] = None if (qa_id in out and out[qa_id] != stamp) else stamp
    return out


def _row_nodes(qa_id: str) -> list[tuple[str, str, dict[str, Any], list[float] | None]]:
    """(node_id, question text, metadata dict, embedding vector) for every node
    of one row. The vector comes back in pgvector's text form ('[0.1,...]'),
    which is valid JSON."""
    with psycopg.connect(db.psycopg_dsn(), autocommit=True) as c, c.cursor() as cur:
        if not _table_exists(cur):
            return []
        cur.execute(sql.SQL(
            "SELECT node_id, text, metadata_, embedding::text FROM {} "
            "WHERE metadata_->>'qa_id' = %s"
        ).format(sql.Identifier(QA_FULL_TABLE)), (qa_id,))
        return [
            (node_id, text, meta if isinstance(meta, dict) else json.loads(meta),
             json.loads(emb) if emb else None)
            for node_id, text, meta, emb in cur.fetchall()
        ]


def _delete_nodes(node_ids: list[str]) -> None:
    if not node_ids:
        return
    with psycopg.connect(db.psycopg_dsn(), autocommit=True) as c, c.cursor() as cur:
        cur.execute(sql.SQL("DELETE FROM {} WHERE node_id = ANY(%s)")
                    .format(sql.Identifier(QA_FULL_TABLE)), (node_ids,))


def _delete_qa_rows(qa_ids: list[str]) -> None:
    if not qa_ids:
        return
    with psycopg.connect(db.psycopg_dsn(), autocommit=True) as c, c.cursor() as cur:
        if not _table_exists(cur):
            return
        cur.execute(sql.SQL("DELETE FROM {} WHERE metadata_->>'qa_id' = ANY(%s)")
                    .format(sql.Identifier(QA_FULL_TABLE)), (qa_ids,))


def _update_node_metadata(node_id: str, old_meta: dict[str, Any], md: dict[str, Any]) -> None:
    """Rewrite one node's stored metadata in place, keeping its vector: the
    top-level keys (what the shield SQL filter reads) and the copies nested in
    _node_content (what retrieval deserializes into the returned node)."""
    new_meta: dict[str, Any] = {
        k: old_meta[k] for k in _NODE_BOOKKEEPING_KEYS if k in old_meta
    }
    new_meta.update(md)
    content = json.loads(old_meta["_node_content"])
    keys = list(md.keys())
    content["metadata"] = md
    content["excluded_embed_metadata_keys"] = keys
    content["excluded_llm_metadata_keys"] = keys
    for rel in content.get("relationships", {}).values():
        if isinstance(rel, dict) and "metadata" in rel:
            rel["metadata"] = md
    new_meta["_node_content"] = json.dumps(content)
    with psycopg.connect(db.psycopg_dsn(), autocommit=True) as c, c.cursor() as cur:
        cur.execute(sql.SQL("UPDATE {} SET metadata_ = %s WHERE node_id = %s")
                    .format(sql.Identifier(QA_FULL_TABLE)),
                    (json.dumps(new_meta), node_id))
```

- [x] **Step 4: Run** `pytest memory/test_seed_sync.py -v` — expect PASS.

- [x] **Step 5: Commit** `feat(memory): DB helpers for incremental Q&A sync`

### Task 4: `_sync_row`, `_sync_locked`, `sync_kb()`

**Files:**
- Modify: `source/memory/seed_memory.py`
- Test: `source/memory/test_seed_sync.py`

**Interfaces:**
- Consumes: Task 2's differ, Task 3's DB helpers, existing `_build_documents`/`_load_jsonl`/`_lock`/`_load_kb`.
- Produces:
  - `_embed_texts(texts: list[str]) -> list[list[float]]` (module fn, monkeypatchable).
  - `_sync_row(vs, entry) -> tuple[int, bool]` — `(embed_call_count, metadata_only)`.
  - `_sync_locked(vs) -> tuple[dict[str, int], bool]` — `(counts, table_had_rows_before)`; assumes `_lock` held; raises `RuntimeError` after processing all rows if any row failed.
  - `sync_kb() -> dict[str, int]` — public; counts per Global Constraints; stamps `qa.facts_invalidated_at` only when changed.

- [x] **Step 1: Write failing tests** (append; uses `sync_env` fixture):

```python
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
    rows = _sql(env, "SELECT metadata_, embedding::text FROM {} WHERE metadata_->>'qa_id' = %s",
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
    (_, emb_before), = _meta_rows(sync_env, "a")
    sync_env.calls.clear()
    _write(sync_env.path, [_entry_line("a", ["q1?"], "new answer")])
    counts = seed_memory.sync_kb()
    assert counts == {"unchanged": 0, "updated": 1, "embedded": 0, "deleted": 0}
    assert sync_env.calls == []                      # zero embed calls
    (meta, emb_after), = _meta_rows(sync_env, "a")
    assert emb_after == emb_before                   # vector untouched
    assert meta["answer"] == "new answer"            # SQL-visible metadata
    inner = json.loads(meta["_node_content"])
    assert inner["metadata"]["answer"] == "new answer"   # node-visible metadata


def test_shield_edit_is_metadata_only_and_sql_enforceable(sync_env):
    _write(sync_env.path, [_entry_line("a", ["q1?"], "ans")])
    seed_memory.sync_kb()
    _write(sync_env.path, [_entry_line("a", ["q1?"], "ans", shield="alice.travel")])
    counts = seed_memory.sync_kb()
    assert counts["updated"] == 1 and sync_env.calls[-1:] == []
    rows = _sql(sync_env, "SELECT metadata_->>'shield' FROM {} WHERE metadata_->>'qa_id' = %s", ("a",))
    assert rows == [("alice.travel",)]
    # removing the shield removes the SQL-visible key again
    _write(sync_env.path, [_entry_line("a", ["q1?"], "ans")])
    seed_memory.sync_kb()
    rows = _sql(sync_env, "SELECT metadata_->>'shield' FROM {} WHERE metadata_->>'qa_id' = %s", ("a",))
    assert rows == [(None,)]


def test_question_added_embeds_only_the_new_string(sync_env):
    _write(sync_env.path, [_entry_line("a", ["q1?", "q2?"], "ans")])
    seed_memory.sync_kb()
    before = dict((json.loads(m["_node_content"])["text"] if False else m["question"], e)
                  for m, e in _meta_rows(sync_env, "a"))
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
    _write(sync_env.path, [_entry_line("d", ["when?"], None, kind="dynamic", handler="old_h")])
    seed_memory.sync_kb()
    sync_env.calls.clear()
    _write(sync_env.path, [_entry_line("d", ["when?"], None, kind="dynamic", handler="new_h")])
    counts = seed_memory.sync_kb()
    assert counts["updated"] == 1 and sync_env.calls == []
    (meta, _), = _meta_rows(sync_env, "d")
    assert meta["handler"] == "new_h"
```

- [x] **Step 2: Run** `pytest memory/test_seed_sync.py -v` — new tests FAIL (no `sync_kb`).

- [x] **Step 3: Implement** in `seed_memory.py`:

```python
def _embed_texts(texts: list[str]) -> list[list[float]]:
    """Batch-embed question strings. Module-level so tests can monkeypatch a
    fake embedder under both sync_kb() and _ensure_populated()."""
    return _embed_model().get_text_embedding_batch(texts)


def _sync_row(vs: PGVectorStore, entry: dict[str, Any]) -> tuple[int, bool]:
    """Reconcile one new/dirty row. Returns (embedded question count,
    metadata_only). The vector derives from the question text alone, so when
    the question multiset is unchanged the nodes' metadata is rewritten in
    place — zero embed calls. Otherwise new nodes are inserted BEFORE the old
    ones are deleted, so retrieval sees old-or-new, never an absent row;
    unchanged question strings keep their stored vectors."""
    docs = _build_documents([entry])
    old = _row_nodes(entry["id"])
    old_questions = sorted(q for _, q, _, _ in old)
    new_questions = sorted(d.text for d in docs)
    if old and old_questions == new_questions and all(e is not None for *_, e in old):
        by_q: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for node_id, q, meta, _ in old:
            by_q.setdefault(q, []).append((node_id, meta))
        for d in docs:
            node_id, meta = by_q[d.text].pop()
            _update_node_metadata(node_id, meta, dict(d.metadata))
        return 0, True
    vectors = {q: emb for _, q, _, emb in old if emb is not None}
    need = sorted({d.text for d in docs} - set(vectors))
    if need:
        for q, vec in zip(need, _embed_texts(need), strict=True):
            vectors[q] = vec
    nodes = [
        TextNode(
            text=d.text,
            metadata=dict(d.metadata),
            excluded_embed_metadata_keys=list(d.metadata.keys()),
            excluded_llm_metadata_keys=list(d.metadata.keys()),
            embedding=vectors[d.text],
        )
        for d in docs
    ]
    if nodes:
        vs.add(nodes)
    _delete_nodes([node_id for node_id, *_ in old])
    return len(need), False


def _sync_locked(vs: PGVectorStore) -> tuple[dict[str, int], bool]:
    """The reconcile body; assumes _lock is held. Returns (counts, whether the
    table already had stamped rows). Loader validation errors raise before any
    write — the table is left untouched, not emptied. A row that fails to
    embed is skipped (its old nodes stay; it remains dirty and retries next
    sync); after all rows are attempted, any failures raise one RuntimeError."""
    global _populated, _entries_by_id, _alias_table
    entries = _load_jsonl()
    stamps = _table_stamps()
    new, dirty, deleted, unchanged = _diff_rows(entries, stamps)
    counts = {"unchanged": unchanged, "updated": 0, "embedded": 0,
              "deleted": len(deleted)}
    _delete_qa_rows(deleted)
    errors: list[str] = []
    embedded_questions = 0
    for e in new + dirty:
        try:
            n_embedded, metadata_only = _sync_row(vs, e)
        except Exception as exc:  # noqa: BLE001 — isolate per row; report below
            errors.append(f"{e['id']}: {exc}")
            continue
        embedded_questions += n_embedded
        counts["updated" if metadata_only else "embedded"] += 1
    if counts["updated"] or counts["embedded"] or counts["deleted"]:
        # Invalidate the registry caches even on partial failure — whatever
        # succeeded is live and _load_kb() rebuilds from the same file.
        _entries_by_id = {}
        _alias_table = {}
        logger.info("sync_kb: %s (%d question embeds)", counts, embedded_questions)
    if errors:
        raise RuntimeError(
            f"sync_kb: {len(errors)} row(s) failed to embed "
            f"(they stay stale and retry next sync) — first: {errors[0]}"
        )
    _populated = True
    return counts, bool(stamps)


def _stamp_facts_if_changed(counts: dict[str, int]) -> None:
    """Post the one-time re-check-facts signal, but only when the sync actually
    changed something — a clean reconcile stays silent."""
    if not (counts["updated"] or counts["embedded"] or counts["deleted"]):
        return
    try:
        db.mark_facts_invalidated()
    except Exception:  # pragma: no cover — no app context; sync still succeeded
        logger.warning("sync_kb: could not stamp qa.facts_invalidated_at", exc_info=True)


def sync_kb() -> dict[str, int]:
    """Reconcile data_seed_memory with the merged JSONL instead of wiping it:
    unchanged rows are skipped, metadata-only edits (answer/shield/path/handler)
    update nodes in place with zero embed calls, question edits embed only the
    changed strings, and vanished ids are deleted. Returns row counts
    {"unchanged", "updated", "embedded", "deleted"}. Stamps
    qa.facts_invalidated_at only when something changed. Raises on loader
    validation errors (table untouched) and on embed failures (other rows
    intact; failed rows stay stale and retry on the next sync).

    Lock order matters: _vector_store() and _load_kb() both take _lock, which
    is non-reentrant — the store is resolved BEFORE the locked section and the
    registry is rebuilt AFTER it (same pattern as rebuild_kb)."""
    vs = _vector_store()
    with _lock:
        counts, _ = _sync_locked(vs)
    _load_kb()
    _stamp_facts_if_changed(counts)
    return counts
```

- [x] **Step 4: Run** `pytest memory/test_seed_sync.py -v` — expect PASS. Also `pytest memory/ -v` for the whole package.

- [x] **Step 5: Commit** `feat(memory): sync_kb incremental reconcile for Q&A embeddings`

### Task 5: `_ensure_populated` runs the sync behind a snapshot guard

**Files:**
- Modify: `source/memory/seed_memory.py` (`_ensure_populated`, new `_source_snapshot`)
- Test: `source/memory/test_seed_sync.py`

**Interfaces:**
- Consumes: `_sync_locked`, `_stamp_facts_if_changed`, `_source_snapshot`.
- Produces: `_ensure_populated(vs)` — same signature; syncs on first call and whenever a source file's `(mtime_ns, size)` changes; `QUERY_AGENT_REBUILD_KB=1` still forces TRUNCATE + full re-embed; sync failure is fatal only when the table is left empty.

- [x] **Step 1: Write failing tests**:

```python
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
```

- [x] **Step 2: Run** — new tests FAIL (old `_ensure_populated` skips when non-empty).

- [x] **Step 3: Implement** — replace `_ensure_populated` with:

```python
def _source_snapshot() -> dict[str, tuple[int, int]]:
    """(mtime_ns, size) per source file — the cheap has-anything-moved guard
    for _ensure_populated. A missing file maps to (0, 0), so the overlay
    appearing or disappearing changes the snapshot too."""
    paths = [QA_JSONL_PATH]
    overlay = _overlay_path()
    if overlay is not None:
        paths.append(overlay)
    snap: dict[str, tuple[int, int]] = {}
    for p in paths:
        try:
            st = p.stat()
            snap[str(p)] = (st.st_mtime_ns, st.st_size)
        except OSError:
            snap[str(p)] = (0, 0)
    return snap


def _ensure_populated(vs: PGVectorStore) -> None:
    """Reconcile the pgvector table with the JSONL (sync_kb semantics) on the
    first call of the process, and again whenever a source file's mtime/size
    changes — every agent runs in a freshly spawned process, so JSONL edits
    become visible on the next message with no button press. When nothing
    moved, a call costs one stat() per source file. QUERY_AGENT_REBUILD_KB=1
    still forces a TRUNCATE + full re-embed on the first call. A sync failure
    (e.g. Ollama down) is fatal only when it leaves the table empty; with
    existing rows it is logged and retried on the next call, and retrieval
    keeps serving the intact rows."""
    global _populated, _sync_snapshot
    with _lock:
        if not _populated and os.environ.get(REBUILD_ENV) == "1":
            logger.info("%s=1 set; truncating %s and repopulating", REBUILD_ENV, QA_FULL_TABLE)
            try:
                _truncate_table()
            except Exception as e:
                logger.warning("truncate %s failed (%s); falling through to sync", QA_FULL_TABLE, e)
        snapshot = _source_snapshot()
        if _populated and snapshot == _sync_snapshot:
            return
        try:
            counts, had_rows = _sync_locked(vs)
        except Exception:
            if _table_row_count() > 0:
                logger.warning("_ensure_populated: sync failed; serving existing rows "
                               "and retrying on the next call", exc_info=True)
                return
            raise
        _sync_snapshot = snapshot
    _load_kb()
    if had_rows:
        # The initial populate of an empty table has no prior facts to re-check.
        _stamp_facts_if_changed(counts)
```

- [x] **Step 4: Run** `pytest memory/ agents/ -x -q` — expect PASS (agents tests exercise `_ensure_populated` wiring).

- [x] **Step 5: Commit** `feat(memory): _ensure_populated reconciles automatically behind a stat snapshot`

### Task 6: Settings endpoints + buttons

**Files:**
- Modify: `source/webapp/settings_views.py` (endpoint ~line 380, JS ~lines 185–240)
- Test: `source/webapp/test_settings_views.py`

**Interfaces:**
- Consumes: `seed_memory.sync_kb()`, `seed_memory.rebuild_kb()`.
- Produces: `POST /settings/api/repopulate_memory` → `{"ok": true, "unchanged": n, "updated": n, "embedded": n, "deleted": n}`; new `POST /settings/api/rebuild_memory` → `{"ok": true, "entries": n, "documents": n}`.

- [x] **Step 1: Update/add endpoint tests** in `test_settings_views.py`: repopulate tests monkeypatch `sync_kb` (counts dict) instead of `rebuild_kb`; add success/failure tests for `/settings/api/rebuild_memory` (mirror the existing repopulate pair); template test asserts the `data-rebuild-full` marker and `rebuild_memory` URL appear in the page. Adapt to the file's existing fixtures on execution.

- [x] **Step 2: Run** `pytest webapp/test_settings_views.py -v` — expect FAIL.

- [x] **Step 3: Implement** — endpoint changes:

```python
@app.route("/settings/api/repopulate_memory", methods=["POST"])
def settings_repopulate_memory() -> tuple[Response, int] | Response:
    """Reconcile the Q&A vector table with the merged JSONL (base +
    customize.dir overlay) — the 'Repopulate Q&A memory' button. Only changed
    rows re-embed (sync_kb), and the facts-invalidated stamp happens inside
    sync_kb only when something actually changed. 502 carries the error
    (typically Ollama being down, or a JSONL error with file:line); synced
    rows stay intact and the stale ones retry on the next press."""
    import memory.seed_memory as seed_memory

    try:
        counts = seed_memory.sync_kb()
    except Exception as exc:  # noqa: BLE001 — any backend failure → 502 + message
        # Not dead code: sync_kb reads the customize.dir setting via db.session
        # (get_setting); a failure there leaves the session in a failed state
        # that must be rolled back before responding.
        db.db.session.rollback()
        logger.warning("repopulate_memory failed: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 502
    return jsonify({"ok": True, **counts})


@app.route("/settings/api/rebuild_memory", methods=["POST"])
def settings_rebuild_memory() -> tuple[Response, int] | Response:
    """TRUNCATE + re-embed everything — the 'Rebuild (full)' escape hatch for
    genuine table corruption. 502 carries the error; the table may then be
    empty or partial, and pressing again after fixing the cause heals it."""
    import memory.seed_memory as seed_memory

    try:
        counts = seed_memory.rebuild_kb()
    except Exception as exc:  # noqa: BLE001 — any backend failure → 502 + message
        db.db.session.rollback()
        logger.warning("rebuild_memory failed: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 502
    # A full rebuild always re-embeds, so prior conversation facts are always
    # due for a re-check.
    db.mark_facts_invalidated()
    return jsonify({"ok": True, **counts})
```

JS: the `customize.dir` row adds `<button data-rebuild-full>Rebuild (full)</button>` after the repopulate button (shared `data-repopulate-result` span). Repopulate handler shows `'synced: ' + d.unchanged + ' unchanged, ' + d.updated + ' updated, ' + d.embedded + ' embedded, ' + d.deleted + ' deleted'`; new rebuild handler posts to `/settings/api/rebuild_memory` and keeps the old `'re-embedded N entries / M questions'` text. NO backslash escapes in the JS.

- [x] **Step 4: Run** `pytest webapp/test_settings_views.py -v` — expect PASS.

- [x] **Step 5: Commit** `feat(settings): incremental Q&A sync button + full-rebuild escape hatch`

### Task 7: Docs

**Files:**
- Modify: `source/docs/qa-system.md` (repopulate/lifecycle sections, ~lines 61–62, 111–112, 190–215)
- Modify: `source/docs/proposals/2026-07-07-incremental-qa-repopulate.md` (status line)

- [x] **Step 1:** Rewrite the qa-system.md lifecycle section to describe the sync: row stamps (`row_sha256`, `kb_epoch`), the diff, the metadata-only fast path, per-row failure isolation, automatic reconcile in `_ensure_populated` behind the stat snapshot, the two buttons, `QUERY_AGENT_REBUILD_KB=1`, and facts stamping only on change. Present tense, current state only.

- [x] **Step 2:** Proposal header → `**Status: implemented.**` (one line; keep the rest as design rationale).

- [x] **Step 3:** Run full test suite `pytest -x -q` (accept pre-existing failures per memory), commit `docs: describe incremental Q&A sync in qa-system.md`.

## Self-Review Notes

- Spec coverage: hashing (T1), epoch (T1/T2), differ (T2), stamp-in-metadata (T1/T3), reconcile + per-row isolation + no wipe window (T4), metadata-only fast path incl. shields + dynamic handlers (T4), cached-embedding reuse (T4), facts stamped only on change (T4/T5/T6), Phase 1 buttons (T6), Phase 2 auto-sync + env override (T5), cold start / legacy tables / validation-leaves-table-untouched (T4 tests), docs (T7).
- `rebuild_kb()` stays untouched (already emits stamped docs via `_build_documents`).
- Types consistent: `_sync_locked` returns `(counts, had_rows)`; `sync_kb` discards `had_rows`, `_ensure_populated` uses it to skip facts-stamping on initial populate.
