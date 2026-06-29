# Final Hardening Report — Memory Trust Fixes

## FIX (a) — `_handle_correct` free-text atomicity

**File:** `source/memory/ops.py`, `_handle_correct` function (~line 233–253)

**What changed:** The post-`record_belief` supersession of the old claim (setting
`status="superseded"`, `write_tombstone`, `supersedes_uuid` link, and
`db.session.commit()`) is now wrapped in a `try/except`. On any exception the
session is rolled back, both claims remain in their original state, and a clear
error message is returned. A snapshot of `old.text` is captured before the
mutation block so the error message remains accurate even if the session rolls
back the attribute.

The success message still uses `old_text_snapshot` (same text) and returns
`f"Corrected: {old_text_snapshot} → {new_text}"`.

**Tests verified:** `memory/test_ops.py::test_correct_supersedes_old_and_creates_new`,
`memory/test_ops.py::test_correct_refused_tombstone_returns_message_without_crash`,
`memory/test_embedding_freshness_wiring.py::test_correct_command_embeds_new_and_prunes_old`
— all pass.

---

## FIX (b) — `record_belief` actor validation

**Files:**
- `source/db/memory.py` — new module-level constant `ACTORS` (tuple of five valid
  actor strings) added immediately before `TOMBSTONE_OVERRIDE_ACTORS` (~line 602).
  Guard `if actor not in ACTORS: raise ValueError(...)` inserted at the top of
  `record_belief`, before `validate_evidence`.
- `source/db/test_record_belief.py` — new test
  `test_record_belief_raises_for_unknown_actor` asserts that `actor="bogus"` raises
  `ValueError` matching `"unknown actor"`. Cleanup uses the same `_cleanup(room)`
  helper as all other tests in that file (by `room_uuid`).

**`ACTORS` tuple (line ~602 of `db/memory.py`):**
```python
ACTORS = (
    "human_review_ui",
    "explicit_human_command",
    "human_confirmed_write_intent",
    "assistant_interpreted",
    "model_inferred",
)
```

`TOMBSTONE_OVERRIDE_ACTORS` is unchanged (it remains the human-actor subset).
`ACTORS` is re-exported via `from db.memory import *` in `db/__init__.py`.

**Tests verified:** new test passes; all pre-existing `db/test_record_belief.py`
tests still pass.

---

## FIX (c) — `_action_activate_memory` stale no-op status

**File:** `source/agents/assistant.py`, `_action_activate_memory` (~line 511)

**What changed:** The observation's `"status"` field was hard-coded as `"active"`.
Changed to `activated.status` (the status of the claim returned by
`db.resolve_conflict` or `db.activate_memory_claim`). When `resolve_conflict`
returns a stale no-op (rival already gone, candidate still in `candidate`), the
observation now correctly reports `"candidate"` instead of misleadingly claiming
`"active"`. The non-conflict path (`activate_memory_claim`) sets `status="active"`
before returning, so normal behavior is unchanged.

**Tests verified:** `memory/test_embedding_freshness_wiring.py::test_assistant_activate_memory_embeds_the_claim`,
all `agents/test_assistant_actions.py` and `agents/test_assistant_writes.py` pass.

---

## Test run

```
cd source && venv/bin/python -m pytest db/test_record_belief.py memory/test_ops.py \
  memory/test_embedding_freshness_wiring.py agents/test_assistant_actions.py \
  agents/test_assistant_writes.py -q
```

**Result:** 90 passed in ~10.5 s (was 89 before the new test was added).

---

## Commit

`fix(memory): harden _handle_correct atomicity, validate actor, fix activate no-op status`

---

## Deviations / concerns

- None. The keyed supersession path in `_handle_correct` (`outcome=="superseded"`)
  was already atomic inside `record_belief`; only the free-text path was changed.
- `ACTORS` is exported from `db` via the existing `import *` in `db/__init__.py`;
  callers that previously used `from db.memory import record_belief` already had
  access to everything they need.
- FIX (c) is a one-word change (`"active"` → `activated.status`); the normal
  activate path still resolves to `"active"` because `activate_memory_claim` sets
  that before returning.
