# correct_belief delegation report

## Summary

`correct_belief` in `source/db/memory.py` now delegates replacement-claim creation
to `record_belief` (with `commit=False`), gaining dedupe, conflict, exact+global
tombstone handling, and dual-key advisory locking for free.

---

## Step 1: `record_belief` commit param

Added `commit: bool = True` to `record_belief`'s signature. Changed every terminal
`db.session.commit()` inside the function body to `if commit: db.session.commit()`.
There are 7 branches: corroborated, global-tombstone-human, global-tombstone-refused,
exact-tombstone-refused, conflict-same-scope-supersede, conflict-candidate, and plain
create. All default to `commit=True`, so existing callers are unaffected.

## Step 2: `correct_belief` rewrite

The old steps 5–11 were replaced with:

**5. Derive keys for BOTH old and new texts** (not just new_text):
```python
new_sp, new_val = belief_keys(None, None, None, new_text)
old_sp, old_val = belief_keys(old.subject, old.predicate, old.object, old.text)
```

**6. Dual-key advisory lock** — locks both old and new keys (each with their global
counterpart), sorted to avoid deadlock:
```python
lock_keys = sorted({
    advisory_key(old.scope, old.room_uuid, old.agent_uuid, old_sp, old_val),
    advisory_key("global", None, None, old_sp, old_val),
    advisory_key(old.scope, old.room_uuid, old.agent_uuid, new_sp, new_val),
    advisory_key("global", None, None, new_sp, new_val),
})
for k in lock_keys: db.session.execute(sa.text("SELECT pg_advisory_xact_lock(:k)"), {"k": k})
```

**7. Supersede + tombstone old claim** (unchanged):
```python
old.status = "superseded"; old.updated_at = ...; write_tombstone(old, ...); delete_memory_embeddings(old.uuid, ...)
```

**8. Delegate replacement creation to `record_belief(commit=False)`** — passes
`parse_structured(new_text)` result as subject/predicate/object so structured
columns are fully populated.

**9. Promote candidate if corroborated** — if `record_belief` returned a candidate
claim (dedupe matched a pre-existing candidate, not an active), promote it to active:
```python
if result.claim is not None and result.claim.status != "active":
    result.claim.status = "active"; result.claim.updated_at = ...
```

**10. Link lineage without overwriting**:
```python
if result.claim is not None and result.claim.supersedes_uuid is None:
    result.claim.supersedes_uuid = old.uuid
```

**11. Single commit, return result.claim**

## Tests added

File: `source/db/test_record_belief_conflict.py`

### P1 dedupe (repro the bug)
`test_correct_belief_dedupes_against_existing_active_claim`

Creates A (text X) and B (text Y) both active in the same scope. Calls
`correct_belief(A.uuid, Y, ...)`. Asserts:
- A is superseded
- Exactly ONE active claim with text Y (B was corroborated, not duplicated)
- `result_claim.uuid == b_uuid` (returned claim is B)

This test FAILED against pre-fix code (found 2 active claims with text Y).

### P3 global tombstone scoped-exception
`test_correct_belief_global_tombstone_scoped_exception`

Creates a global claim with text_global, rejects it (leaves global tombstone).
Creates a room-scoped claim old_room. Calls `correct_belief(old_room.uuid,
text_global, actor="explicit_human_command", ...)`. Asserts:
- (a) old_room is superseded
- (b) new room-scoped claim exists and is active
- (c) global tombstone still exists (not cleared by correct_belief)
- (d) evidence on new claim contains "scoped exception" annotation

This test FAILED against pre-fix code (evidence had no "scoped exception" note).

## Existing behavior preserved (regression caught and fixed)

`test_correct_via_candidate_leaves_active_replacement` (in `memory/test_ops_record_belief.py`)
was already existing and passing before. It caught a regression: when `record_belief`
corroborates a pre-existing **candidate** (status="candidate"), the candidate stays
as "candidate" — a human correction must leave an **active** belief. Fixed by step 9
above (promote candidate to active after record_belief returns).

## Test command and output

```
cd source && venv/bin/python -m pytest db/test_record_belief.py db/test_record_belief_conflict.py db/test_conflict_resolution.py memory/test_ops.py memory/test_ops_record_belief.py memory/test_embedding_freshness_wiring.py webapp/test_memory_api.py -q
```

Output: `99 passed in 10.77s`

## Callers verified

- `memory/ops.py::_handle_correct`: calls `db.correct_belief(...)`, uses returned
  claim for `refresh_claim_embedding(new)`. `result.claim` is always a MemoryClaim
  for human actors (never refused_tombstone). No change needed.
- `webapp/memory_api.py` correct branch: `new = db.correct_belief(...)` then
  `new.uuid`. Same: human actors always get a non-None claim. StaleWriteError still
  propagates to 409 handler unchanged.

## Commit

`fix(memory): correct_belief delegates to record_belief (dedupe/conflict/global-tombstone/dual-lock)`

## Concerns

1. **Candidate promotion side effect**: when `correct_belief` corroborates a candidate
   and promotes it to active, the candidate's `supersedes_uuid` points to old (the
   superseded claim), not to its original lineage if any. This is correct behavior for
   a human correction but worth noting.

2. **record_belief re-acquires the new key lock** inside its own `_lock_belief` call.
   This is a no-op (PostgreSQL re-entrant advisory xact locks), but adds a tiny overhead.
   Acceptable.

3. **`result.claim` for non-human actors**: `correct_belief` guards `actor not in
   TOMBSTONE_OVERRIDE_ACTORS`, so by the time we reach step 8, actor is always a human.
   `refused_tombstone` (claim=None) can never happen for human actors. The `if result.claim
   is not None` guards are defensive but safe.
