# Chat write-proposal UI + step provenance

## Problem

When the assistant proposes a confirm-tier write (e.g. `set_reminder`), it records
an `AssistantWriteIntent` in `proposed` state and posts a plain chat reply saying it
awaits confirmation. The operator can only confirm/reject from the `/assistant`
inspector page — there is no control in chat, where the conversation actually
happens. And once a reminder exists there is no trail from the created cron job back
to the assistant step that produced it.

Two asks:

1. **In-chat confirm/reject** for any confirm-tier write proposal.
2. **A deep link to the exact `/assistant` step** that created the reminder, shown
   **in chat** and **persisted on the cron job** (visible on `/cron`).

## What already exists (reuse, don't rebuild)

- `AssistantWriteIntent` (`db/models.py`): bound to its producing step via
  `step_uuid` + `run_uuid`; state machine `proposed → confirmed → executing →
  completed | failed`, plus `rejected` / `undone`. Payload is hash-bound so a
  confirmed intent executes exactly what was previewed.
- Endpoints already drive the lifecycle (`webapp/chat_api.py`):
  - `POST /chat/api/assistant/write-intents/<uuid>/confirm` → `execute_write_intent`
  - `POST /chat/api/assistant/write-intents/<uuid>/reject` → `reject_write_intent`
  - `POST /chat/api/assistant/write-intents/<uuid>/undo`
  These are the same endpoints the `/assistant` page buttons use; the chat UI reuses
  them verbatim.
- Step deep-link format: `/assistant?id=<run_uuid>#step-<step_uuid>` (the `/assistant`
  page scrolls to and `:target`-highlights `id="step-<uuid>"`). Currently built
  ad-hoc in `webapp/core.py:_format_step_trace_link`.
- `_propose_write` (`agents/assistant.py`) has `self._run.uuid` and `ctx.step_uuid`
  in scope, and already returns `data={"write_intent_uuid", "state"}`.
- The run loop already harvests per-turn extras from observations
  (`result_links`) and attaches them to the terminal `REPLY` message via
  `_append_result_links` — the proposal card rides the same rail.

## Design

### Shared primitive — `assistant_step_path`

A pure helper in `db/assistant.py`:

```python
def assistant_step_path(run_uuid: UUID, step_uuid: UUID) -> str:
    """The /assistant deep link to one step of one run: the run page scrolled to
    (and :target-highlighting) id="step-<step_uuid>"."""
    return f"/assistant?id={run_uuid}#step-{step_uuid}"
```

Used by the chat `meta` and the cron serialization. `webapp/core.py:_format_step_trace_link`
is refactored to build its `href` from this helper (one source of truth).

### Part 1 — In-chat confirm/reject (generic)

**Carrier — `chat_message.meta JSONB`.** `ChatMessage` gains a nullable JSONB
`meta` column (default `{}`). For a proposal message:

```json
{ "write_intent": "<uuid>", "capability": "set_reminder",
  "step_link": "/assistant?id=<run>#step-<step>" }
```

`meta` is a general structured attachment (not proposal-specific), so future
interactive messages can reuse it.

**Producing the card.** No new message type — the card rides the turn's existing
terminal `REPLY`:

1. `_propose_write` adds to its observation `data` a `"proposal"` dict:
   `{"write_intent": str(intent.uuid), "capability": cap.name.value,
   "step_link": assistant_step_path(self._run.uuid, ctx.step_uuid)}`.
2. The run loop captures it into a per-turn `pending_proposal` alongside the
   existing `result_links` harvest (around `agents/assistant.py:1418`).
3. The terminal `REPLY` posts with `meta=pending_proposal`:
   `db.post_chat_message(room_uuid, self.agent_uuid, text, kind="message",
   meta=pending_proposal or None)` (`agents/assistant.py:1366`).

`post_chat_message` gains an optional `meta: dict | None = None` param, stored on the
row. If the turn ends without a `REPLY` (step-limit), the card is simply absent —
the `/assistant` page remains the fallback. (At most one confirm-tier write is
proposed per turn — the loop steers the model to `reply` immediately after a
proposal — so a single `pending_proposal` slot is sufficient; if a second proposal
ever occurred it overwrites, and both stay confirmable on `/assistant`.)

**Live state enrichment.** A stored message can't know the intent was later
confirmed/rejected on `/assistant`. `list_room_messages` (`db/chat.py:531`) emits
`meta` and, for messages whose `meta.write_intent` is set, enriches it with the
intent's current `state` via one batched lookup (collect all `write_intent` uuids in
the page, one `IN` query, splice `meta.intent_state`). Mirrors the existing batched
`latest_feedback` pass.

**Rendering (chat).** `makeMessage` (`webapp/chat_template.py`) renders a proposal
card below the text when `m.meta && m.meta.write_intent`:

- `intent_state === 'proposed'` → **Confirm** (primary) + **Reject** (danger)
  buttons, and a `View step ↗` link (`m.meta.step_link`).
- `completed` → `✓ Confirmed · View step ↗`
- `rejected` → `✕ Rejected · View step ↗`
- `failed` → `⚠ Failed · View step ↗`
- anything else (`confirmed`/`executing`) → a disabled `… working` chip + link.

Buttons POST to the existing confirm/reject endpoints; on `ok` the card re-renders to
the resulting state in place (confirm → `completed`, showing `obs.text` such as
"Reminder set for …"; a stale confirm returns `ok:false` and the card refreshes to
the real state). The `/assistant`-page buttons are unchanged; both surfaces share the
endpoints, so confirming in one is reflected in the other on next load.

### Part 2 — Step provenance on the cron job

**Schema.** `cron_job` gains two nullable columns: `origin_run_uuid uuid`,
`origin_step_uuid uuid`. Manual jobs leave them null.

**Write.** `cron_create_one_shot_message` gains
`origin_run_uuid: UUID | None = None, origin_step_uuid: UUID | None = None` and stores
them. `_action_set_reminder`, on real (non-dry-run) execution, derives the origin from
`ctx.step_uuid` (the proposing step, threaded through `execute_write_intent`'s
`AssistantActionContext`): look up the step to get its `run_uuid`, then pass both. If
`step_uuid` is absent (e.g. a future programmatic caller) origin stays null — a clean
no-op.

**Read.** `cron_load_tree` (`db/cron.py`) adds a derived, read-only
`origin_step_link` to each job dict: `assistant_step_path(origin_run_uuid,
origin_step_uuid)` when both are set, else `null`. The `/cron` job-details panel
(`static/cron.js`) renders an `Origin: created by assistant — View step ↗` row when
`origin_step_link` is present. Read-only; never sent back on save (the tree save path
ignores it, like other scheduler-owned read-only fields).

### Migration

Established pattern: model columns cover fresh DBs via `create_all`; existing DBs get
idempotent `_add_column_if_missing` calls in `init_db` (`db/__init__.py`):

- `_add_column_if_missing("chat_message", "meta", "meta jsonb NOT NULL DEFAULT '{}'::jsonb")`
- `_add_column_if_missing("cron_job", "origin_run_uuid", "origin_run_uuid uuid")`
- `_add_column_if_missing("cron_job", "origin_step_uuid", "origin_step_uuid uuid")`

## Components & boundaries

| Unit | Responsibility | Depends on |
|---|---|---|
| `assistant_step_path` (`db/assistant.py`) | Build the `/assistant` step deep link | nothing (pure) |
| `chat_message.meta` + `post_chat_message(meta=)` | Carry structured attachment on a message | model column |
| `list_room_messages` enrichment | Splice live `intent_state` onto proposal messages | `AssistantWriteIntent` |
| `_propose_write` / run-loop harvest | Stash `pending_proposal`, attach to terminal reply | `assistant_step_path` |
| proposal card (`chat_template.py` JS) | Render card + wire buttons to existing endpoints | confirm/reject routes |
| `cron_job.origin_*` + `cron_create_one_shot_message` | Persist provenance | model columns |
| `cron_load_tree` `origin_step_link` + cron.js row | Surface provenance on `/cron` | `assistant_step_path` |

## Testing

- **`assistant_step_path`**: exact string for a known run/step pair.
- **`post_chat_message` / `list_room_messages`**: a message posted with `meta`
  round-trips; a proposal message is enriched with the intent's live `intent_state`,
  and the state tracks a `proposed → completed` / `→ rejected` transition.
- **Proposal harvest**: a confirm-tier turn attaches `meta` (with `write_intent`,
  `capability`, `step_link`) to the terminal reply message; a read-only turn attaches
  nothing.
- **Cron provenance**: `_action_set_reminder` real execution stores
  `origin_run_uuid` / `origin_step_uuid` on the created job; `cron_load_tree` exposes
  the matching `origin_step_link`; a manually-created job exposes `null`.
- **Regression**: existing reminder, write-intent, and cron suites stay green.
- JS card rendering and the `/cron` origin row are verified manually (no JS test
  harness in repo).

## Out of scope (YAGNI)

- No new message `kind`; proposals reuse `kind="message"` + `meta`.
- No realtime push of intent-state changes into an open chat tab — state is correct on
  load and after the user's own click; cross-surface live sync is not required.
- No provenance on recurring cron jobs or non-reminder writes beyond what the generic
  `meta` / `origin_*` columns already allow.
- No change to the confirm/reject/undo endpoints or the intent state machine.
