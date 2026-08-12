# Incremental Q&A repopulate

**Status: implemented** — see `docs/qa-system.md` ("Sync (incremental
reconcile)") for the current mechanics; the rest of this file is the design
rationale. Make "Repopulate Q&A memory" a cheap reconcile instead
of a wipe-and-re-embed, by stamping each embedded row with a SHA-256 of its
source JSONL line and re-embedding only what changed — and then, because a
clean reconcile costs almost nothing, stop requiring the operator to press
the button at all.

## Problem

Editing `question_answer.jsonl` (base or overlay) requires a manual
repopulate, and that repopulate is total: `rebuild_kb()` TRUNCATEs
`data_seed_memory` and re-embeds every question alternate of every entry
(`memory/seed_memory.py`). Today that takes ~3 seconds. Two trajectories
make it worse:

- The overlay grows. The person-schema convention deliberately encourages
  long stories and many question alternates per entry; embedding cost is one
  Ollama call per question alternate, so the rebuild scales with the whole
  corpus even when one answer changed.
- The wipe window. Between TRUNCATE and the last insert, retrieval sees a
  partial (or, on failure, empty) table. Today's contract — "the next
  successful run heals it" — is fine at 3 seconds and annoying at 30.

The irony: the *memory* embeddings already solved this.
`db.upsert_memory_embedding` is keyed on `(memory_uuid, model_name,
text_hash)` and `sync_memory_embeddings` reconciles instead of wiping. The
Q&A table just never got the same treatment.

## Design

### The dirty detector: one SHA-256 per source row

Hash the **entire raw JSONL line** (stripped, UTF-8 bytes) — not just the
answer text. The loader already walks lines to parse entries; it
additionally carries `_row_sha256` on each merged entry (for an id that
appears in both base and overlay, the winning file's line is the one
hashed, matching the existing merge rule).

Hashing the whole line, as opposed to selected fields, is the right
simplicity: any edit — answer, questions, shield, path, kind — makes the
row dirty, with zero schema knowledge in the detector. The cost is that a
whitespace-only edit also counts as dirty; that is rare and harmless.

Fold an **epoch** into the stored stamp:

```
row_stamp = sha256(raw_line)  +  epoch("embeddinggemma:300m" + KB_SCHEMA_VERSION)
```

A change of embedding model, or of the node-metadata shape (e.g. the
`_audience` field the operator-profiles proposal adds), bumps the epoch and
dirties everything automatically — no "remember to full-rebuild after
upgrading" footgun.

### Where the stamp lives

In the embedded node's metadata, next to `qa_id`/`shield`: every node of a
row carries the row's `row_sha256` and `kb_epoch`. No second table — the
stamp lives with the data it describes, so a node without the expected
stamp *is* the definition of stale, and the two can never drift apart.
(Rejected: a separate `qa_row_hash` table — a second source of truth that
can disagree with the vector table after a partial failure.)

### The reconcile

`sync_kb()` replaces the wipe inside the button (and everywhere else):

1. Load merged entries with their `_row_sha256` (existing loader + one
   field).
2. One query: `SELECT DISTINCT metadata_->>'qa_id', metadata_->>'row_sha256',
   metadata_->>'kb_epoch' FROM data_seed_memory`.
3. Diff:
   - **new** (id not in table) → embed its questions, insert.
   - **dirty** (stamp differs) → delete the row's nodes, re-insert (see the
     fast path below).
   - **deleted** (id in table, not in file) → delete its nodes.
   - **unchanged** → skip. This is the common case and costs nothing.
4. Rebuild the in-memory registry as today; stamp
   `qa.facts_invalidated_at` **only when something actually changed** — an
   improvement over today, where every button press posts a re-check-facts
   notice into rooms even when nothing changed.

Per-row failure isolation falls out naturally: each row's delete+insert is
its own small operation, so an embedder failure mid-run leaves every other
row intact and the dirty row still stamped stale (it retries next sync).
The wipe window disappears entirely — retrieval sees old-or-new per row,
never an empty table.

### The metadata-only fast path (most edits need zero embed calls)

The embedded vector derives from the **question text alone**
(`_build_documents` excludes all metadata from the embedding). So for a
dirty row, compare its question list against the table's nodes for that
`qa_id`:

- **Question set unchanged** (the operator edited the answer, shield, or
  path — the overwhelmingly common edit): update the nodes' metadata in
  place (`answer`/`shield`/`row_sha256`), keep the vectors. **Zero
  embedding calls.**
- **Questions changed**: re-embed only the added/changed question strings;
  unchanged question strings keep their vectors (insert nodes with the
  cached embedding — llama-index nodes carry an optional precomputed
  `embedding`, so this avoids the embed call).

Shield edits *must* go through this path correctly: `shield` lives in the
pgvector metadata that the retrieval filter reads, so the metadata update is
what makes the new shield enforceable — covered by extending the existing
shield tests to the sync path.

### Expected costs

| Edit | Today | After |
|---|---|---|
| nothing changed | full re-embed (~3 s) | one SELECT + file hash (≪100 ms) |
| one answer/shield edited | full re-embed | metadata UPDATE, 0 embed calls |
| one new entry (5 questions) | full re-embed | 5 embed calls |
| one question rephrased | full re-embed | 1 embed call |
| embed model / schema bump | full re-embed | full re-embed (epoch dirties all) — same as today, now automatic |

### From "cheap" to "automatic"

Once a clean sync is ~free, the manual step can disappear:

1. **Phase 1** — the `/settings` button runs `sync_kb()` and reports
   `{unchanged, updated, embedded, deleted}` counts. A separate **"Rebuild
   (full)"** escape hatch keeps today's TRUNCATE semantics for genuine
   corruption.
2. **Phase 2** — `_ensure_populated()` runs the sync (not just the
   is-empty check), guarded by an mtime/size snapshot of the source files
   so the hot path pays a `stat()` rather than a file read when nothing
   moved. Because every agent runs in a freshly spawned process,
   `_load_kb`/`_ensure_populated` already execute once per message — so
   JSONL edits become visible on the next turn **with no button press at
   all**. `QUERY_AGENT_REBUILD_KB=1` keeps forcing the full rebuild.

(Rejected: a file watcher — another thread and OS-specific machinery to
solve what per-process startup already solves; and mtime-only dirty
detection without hashes — editors and git checkouts touch mtimes without
changing content, and mtime says *which file* changed, not *which rows*.)

## Compatibility and edge cases

- **Cold start / missing table**: every row is "new"; behavior identical to
  today's first populate.
- **Existing tables without stamps**: every node lacks `row_sha256` → all
  rows dirty → one final full re-embed, after which increments apply. No
  migration step.
- **Duplicate-id and duplicate-path validation** stays in the loader,
  unchanged — a validation failure still fails the sync loudly with
  `file:line`, and (better than today) leaves the table untouched rather
  than emptied.
- **Dynamic entries** (`kind="dynamic"`): same treatment; their nodes carry
  `handler` instead of `answer` in metadata, and the metadata-only path
  covers handler renames.
- **`rebuild_kb()` callers**: the function stays (the full path); the
  button and `_ensure_populated` switch to `sync_kb()`.

## Testing

- Unit: the differ (new/dirty/deleted/unchanged) against a fake table; the
  epoch bump; winning-file hashing for overlay overrides.
- Integration (fake embedder, as in `memory/test_embeddings.py`): answer
  edit → 0 embed calls and updated metadata; question edit → exactly the
  changed alternates embed; deletion removes nodes; shield edit is
  enforceable immediately after sync; a failing embedder leaves other rows
  intact and retries.
- The existing `test_seed_memory_errors.py` / `test_seed_shields.py`
  suites run against the sync path.

## See also

- `docs/qa-system.md` — the current registry/repopulate mechanics this
  changes.
- `db/memory.py::upsert_memory_embedding` + `sync_memory_embeddings` — the
  in-house precedent (hash-keyed reconcile for memory embeddings).
- `2026-07-07-operator-profiles-and-working-context.md` — adds `_audience`
  to node metadata; lands as one `KB_SCHEMA_VERSION` bump here.
- `2026-07-04-qa-overlay-person-schema.md` — the authoring conventions that
  will grow the overlay past the point where 3 seconds stays 3 seconds.
