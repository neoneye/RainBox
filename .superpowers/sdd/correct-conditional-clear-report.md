# correct_belief: conditional conflict-clear fix

## Problem (laundering hole)

`correct_belief` (step 9b) unconditionally cleared `conflicts_with_uuid` on any
replacement claim that carried one. This silently activated a candidate that
conflicted with a DIFFERENT still-active same-scope claim, leaving two active
beliefs for the same subject-predicate key with no conflict marker — a laundering
hole.

Repro: active "X prefers tea"; candidate "X prefers coffee" (conflicts_with_uuid=tea);
unrelated active "X note is stale". Correcting note → "X prefers coffee" corroborated
the coffee candidate, promoted it active, cleared its conflict pointer, superseded only
the note, leaving BOTH tea and coffee active in the same scope/room with no conflict.

## Changes

### `source/db/memory.py`

**Added `_same_bucket(a, b)` helper** (just above `record_belief`):

```python
def _same_bucket(a: MemoryClaim, b: MemoryClaim) -> bool:
    return a.scope == b.scope and a.room_uuid == b.room_uuid and a.agent_uuid == b.agent_uuid
```

**Replaced steps 9/9b in `correct_belief`** with conditional disposition logic:

```
if result.outcome == "conflict_candidate":
    # record_belief only returns this for human when rival is broader-scope.
    # Scoped exception: activate + clear + note "scoped exception over broader..."
else:
    if claim.status == "active" and claim.conflicts_with_uuid is None:
        pass  # nothing to do
    else:
        cw = claim.conflicts_with_uuid
        if cw is None:
            # plain candidate, no conflict -> activate
        elif cw == old.uuid:
            # candidate conflicted with the claim being corrected -> activate + clear
            # + note "conflict resolved by correcting the conflicting claim"
        else:
            rival = get_memory_claim(cw)
            if rival is None or rival.status != "active":
                # stale -> activate + clear + note "stale conflict pointer..."
            elif _same_bucket(rival, claim):
                # REFUSE: different still-active same-scope rival
                raise ValueError(f"cannot correct to {new_text!r}: ...")
            else:
                # broader/different-bucket active rival -> scoped exception
                # activate + clear + note "scoped exception over broader..."
```

The `raise ValueError` happens BEFORE `db.session.commit()` (no commit occurs
between the old-supersede in step 7 and the disposition in steps 9/9b), so the
uncommitted supersede of `old` is automatically rolled back when the exception
propagates.

### `source/memory/ops.py`

`_handle_correct`: wrapped `db.correct_belief(...)` in try/except ValueError.
On refusal: calls `db.db.session.rollback()` to discard uncommitted changes, then
returns a user-facing message: `f"Could not correct {old_text!r} → {new_text!r}: {exc}"`.

### `source/webapp/memory_api.py`

No change needed — the existing `try/except ValueError as exc → 400` at line
207-212 already wraps `_dispatch_action` which calls `correct_belief`.

## Tests added (`source/db/test_record_belief_conflict.py`)

### 1. `test_correct_belief_refuse_same_scope_conflicting_corroboration`
**Bug repro.** Sets up active tea, model-created coffee candidate
(conflicts_with_uuid=tea), and unrelated active note. Calls
`correct_belief(note.uuid, coffee_text, ...)` and asserts `ValueError` is raised.
After rollback, asserts: note still active, tea still active, coffee still candidate
with conflicts_with_uuid==tea, and at most 1 active claim for the
tea/coffee predicate. This test **FAILED before the fix and now PASSES**.

### 2. `test_correct_belief_safe_conflict_with_old`
Correcting the claim that IS the rival (tea) to the coffee candidate value must
succeed: coffee promoted to active, conflicts_with_uuid cleared, tea superseded.

### 3. `test_correct_belief_broader_rival_scoped_exception` (pre-existing)
Unchanged; still passes. Verifies the broader-scope scoped-exception path.

### 4. `test_correct_belief_plain_candidate_corroboration`
Correcting to a value that exists as a plain candidate (no conflict pointer) must
promote it to active with no spurious ValueError.

## Test run output

```
venv/bin/python -m pytest db/test_record_belief_conflict.py db/test_record_belief.py \
  db/test_conflict_resolution.py memory/test_ops.py memory/test_ops_record_belief.py \
  memory/test_embedding_freshness_wiring.py webapp/test_memory_api.py -q

105 passed in 10.95s
```

## Commit

See git log for SHA.

## Concerns / notes

- The refuse path must rely on the absence of any commit between step 7
  (old-supersede) and the disposition raise. Verified: `db.session.commit()` at
  step 11 is the ONLY commit in `correct_belief`; it is only reached on
  non-refusing paths.
- `memory_api.py`'s `_dispatch_action` for the "correct" action does NOT
  explicitly rollback on ValueError — it propagates to the outer try/except which
  returns 400. The Flask/SQLAlchemy session is left dirty; the next request will
  begin a fresh transaction. This is acceptable but callers should be aware.
  The `ops.py` path explicitly rollbacks because the same Python process reuses
  the session across memory commands in a long-lived agent.
- All 4 new disposition paths (conflict_candidate scoped exception, cw==old.uuid,
  stale rival, same-bucket refuse, broader-bucket scoped exception) are exercised
  by the test suite.
