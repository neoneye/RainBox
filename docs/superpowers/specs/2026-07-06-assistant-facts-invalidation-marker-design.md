# Assistant facts-invalidation marker

## Problem

The assistant answers factual questions from the conversation transcript instead
of from a fresh `query_memory` call. When the operator changes a setting that can
stale prior facts — unlocking/locking a Q&A shield, or repopulating the Q&A
knowledge base — an answer given earlier in the room is still visible in the
transcript (`format_history` feeds the last N `kind="message"` rows to the
model), so the model repeats it. `query_memory` itself filters correctly; the
leak is the durable chat history, which shields do not gate.

A system-prompt directive alone ("earlier messages are context, not facts")
proved insufficient — the model still reused a prior answer.

## Approach

A soft, non-destructive marker. Nothing is removed from history (the assistant
keeps its full conversational memory). When facts may have changed, drop a
one-time visible notice into the room the next time the assistant runs there,
telling the model that earlier answers may be out of date and to re-check via
`query_memory`. This pairs with the existing prompt directive by giving it the
recency a static prompt lacks.

Chosen over hard alternatives (dropping prior answers from the transcript,
shield-tagging every message) because the operator explicitly wants the
assistant to retain conversational memory — no "lobotomy" on setting changes.

## Design

### Invalidation timestamp

New setting `qa.facts_invalidated_at` (type `string`, default `None`): the ISO
timestamp of the last change that can stale prior facts.

A helper `db.mark_facts_invalidated() -> str` sets it to
`datetime.now(UTC).isoformat()` and returns the value.

Stamped from two existing endpoints in `source/webapp/settings_views.py`:

- `POST /settings/api/set` — when `key == "qa.unlocked_shields"` **and** the
  stored value actually changed (compare before/after; re-saving the same value
  does not stamp).
- `POST /settings/api/repopulate_memory` — on a successful `rebuild_kb()`.

### Lazy marker in the assistant loop

In `AssistantAgent.handle()` (`source/agents/assistant.py`), before posting the
"working on it" progress row, call a new helper:

```python
FACTS_INVALIDATION_NOTICE = (
    "Notice: a setting changed since earlier in this conversation, so stored "
    "facts and the Q&A knowledge base may now differ. Re-check any fact with "
    "query_memory before relying on it — earlier answers in this conversation "
    "may be out of date."
)

def _maybe_post_facts_marker(self, room_uuid: UUID) -> None:
    """Post a one-time re-check-facts notice when facts were invalidated since
    the last marker in this room. Dedup is by the exact invalidation timestamp
    stored in the marker's meta, so at most one marker per invalidation per
    room."""
    try:
        stamp = db.get_setting("qa.facts_invalidated_at")
    except Exception:
        return
    if not stamp:
        return
    msgs = db.list_room_messages(room_uuid)
    if any((m.get("meta") or {}).get("facts_invalidation") == stamp for m in msgs):
        return
    db.post_chat_message(
        room_uuid, self.agent_uuid, FACTS_INVALIDATION_NOTICE,
        kind="message", meta={"facts_invalidation": stamp},
    )
```

`kind="message"` so it enters the transcript (the loop filters to
`kind == "message"`) and shows in the UI. The call sits before the progress-row
post so the marker's `kind="message"` side effect (clearing the sender's
`progress` rows) does not reap this turn's own progress bubble.

### Keep the operator's message as "Current message"

The marker is posted after the operator's triggering message, so it becomes the
newest `kind="message"` row. `format_history` treats the last row as the
Current message, so a trailing marker must be moved into history:

```python
raw = [m for m in db.list_room_messages(room_uuid) if m.get("kind") == "message"]
# A marker we just posted is the newest row; the operator's message must remain
# the Current message, so move a trailing marker back into history.
if len(raw) >= 2 and (raw[-1].get("meta") or {}).get("facts_invalidation"):
    raw[-1], raw[-2] = raw[-2], raw[-1]
transcript = format_history(raw, context_limit=self.MAX_RECENT_MESSAGES)
```

### Scope

- Assistant loop only (where the bug was reported). The chat agent has the same
  transcript shape but is out of scope for now.
- No schema change (uses the existing `meta` JSON column and the settings
  registry).
- Retroactive: works on existing rooms immediately (no backfill) — the first
  time the assistant runs in a room after an invalidation, the marker appears.

## Testing

- `db.mark_facts_invalidated()` sets `qa.facts_invalidated_at` to a non-empty
  ISO string and returns it.
- `POST /settings/api/set` with `qa.unlocked_shields` stamps
  `qa.facts_invalidated_at` when the value changes, and does **not** stamp when
  the value is unchanged.
- `POST /settings/api/repopulate_memory` stamps it on success (stub
  `rebuild_kb`).
- `_maybe_post_facts_marker`: posts one `kind="message"` marker with
  `meta.facts_invalidation == stamp` when a room has none for the current stamp;
  a second call posts nothing (dedup); posts nothing when the setting is unset.
- Transcript reorder: with a trailing marker, `format_history`'s Current message
  is the operator's message, and the marker appears in the history section.

## Out of scope

- The chat agent (`StructuredChatAgent`).
- Removing or redacting any existing history.
- Overlay-file edits that do not go through repopulate.
