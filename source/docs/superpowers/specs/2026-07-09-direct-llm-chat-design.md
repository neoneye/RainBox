# Direct LLM chat rooms — design

## Purpose

A second kind of chatroom on `/chat`: a plain one-to-one conversation between
the operator and a single LLM, the way LM Studio's chat works. The model sees
the **entire room history** as proper `system`/`user`/`assistant` chat
messages (not the IRC-style transcript the agents get), makes one plain-text
completion per turn (no structured output, no tools, no memory retrieval),
and streams its reply into the room.

What it enables that agent rooms don't:

- **Editable messages.** The operator can edit any earlier message — their
  own *and* the model's — to steer the conversation. Editing is exclusive to
  direct rooms: the server rejects edits in agent rooms.
- **Editable system prompt, mid-conversation.** Stored per room, applied on
  the next turn.
- **Model switching, mid-conversation.** The room stores which model it
  talks to; switching applies on the next turn.

Out of scope for now (future work, listed for orientation only): forking a
chat, deleting earlier messages, regenerate-from-edit.

## Data model

Three new columns on `chatroom` (idempotent `ADD COLUMN IF NOT EXISTS` in
`db/__init__.py init_db`, matching the house pattern):

- `room_type` Text NOT NULL default `'agents'` — `'agents'` (existing
  behavior) or `'direct'`.
- `system_prompt` Text NOT NULL default `''` — the direct room's system
  prompt. Empty means *no system message is sent*.
- `model_uuid` UUID nullable — a `ModelConfig` **or** `ModelConfigOverride`
  uuid (same duality as model-group members; resolved via
  `db.resolved_model_kwargs`). Null means the room has no model yet and
  cannot reply.

No new tables. Messages, folders, membership, SSE all stay as they are.
`chat_tree_version()` is untouched — the new fields are room settings, not
tree structure, so editing them never 409s an open page.

## The direct-chat agent

A new runnable role `direct_chat` (fixed `DIRECT_CHAT_UUID` in
`agents/config.py`, class `agents/direct_chat.py:DirectChatAgent`, wired in
`agents/__main__.py._AGENT_CLASS_PATHS`). It subclasses `Agent` directly —
**not** `ModelGroupAgent` — because the model comes from the room row, not
from an agent→model-group binding.

Per turn (inbox payload `{room_uuid, message_uuid}`):

1. Load the room; verify `room_type == 'direct'`.
2. If `model_uuid` is null, post a `kind="notice"` row telling the operator
   to pick a model in the Settings sidebar, and complete. (`notice` is
   excluded from transcripts, so the model never sees it.)
3. Build the message list: optional system message (`system_prompt` if
   non-blank), then every `kind == "message"` row in the room, oldest first,
   mapped by sender: human → `user`, anything else → `assistant`.
   **Full history, no window.**
4. `prepare_llm(*resolved_model_kwargs(room.model_uuid))`, `stream_chat`,
   stream into the room via `StreamingReplyWriter` +
   `extract_stream_deltas` (thinking row + answer row, same as the
   unstructured chat agent). Single model — no fallback list; a failure
   journals the item `failed` and closes any streaming rows.

Triggering: `POST /chat/api/rooms/<uuid>/messages` checks the room type. In
a direct room a human post enqueues `DIRECT_CHAT_UUID` (and nothing else);
`_maybe_trigger_chat_agents` is only called for agent rooms. The human-only
guard is preserved, so the model's own reply never re-triggers a turn.

Seeding: `seed_chat_defaults` already creates a `chat_user` per
`agent_config` entry, so `direct_chat` gets its identity for free. Direct
rooms are created with exactly two members: the human and the `direct_chat`
user.

## HTTP API (chat_api.py)

- `POST /chat/api/rooms` — accepts optional `room_type` (`'agents'` default,
  `'direct'`). A direct room ignores `member_uuids` and gets the human +
  `direct_chat` user as members.
- `GET /chat/api/rooms/<uuid>/settings` — `{room_type, system_prompt,
  model_uuid}`.
- `PUT /chat/api/rooms/<uuid>/settings` — body may carry `system_prompt`
  (string) and/or `model_uuid` (uuid string, or null to clear). 400 on an
  agents room — settings are a direct-room concept.
- `PUT /chat/api/rooms/<uuid>/messages/<int:message_id>` — edit a message's
  text. Guards: room must be direct (403), message in the room (404),
  `kind == "message"` (400), not currently streaming (409). Both human and
  model messages are editable. Re-runs `detect_content_type`, NOTIFYs with
  the row's kind + `streaming:false` + text so open tabs update the bubble
  in place via the existing `applyStreamingUpdate` path — no new SSE
  machinery. Editing never triggers a model turn.
- `GET /chat/api/models` — the selectable models for the settings dropdown:
  every `ModelConfig` (label `provider · display_name`) and every
  `ModelConfigOverride` (label `provider · model — override`), with
  `available` flags; available first.

## UI (chat_template.py — inline, non-raw string; obey
docs/chat-frontend-rules.md)

- **New-room modal**: a room-type radio — "Agents room" (default) / "Direct
  LLM chat". Choosing direct hides the agent checkbox list. POST carries
  `room_type`.
- **Tree payload**: `list_chatrooms()` rooms gain `room_type` and
  `model_uuid` so the client knows both without extra fetches.
- **Right panel**: the sidebar dropdown gains a "Settings" option. For a
  direct room it renders: model `<select>` (populated from
  `/chat/api/models` on open — user activity, not polling), a system-prompt
  `<textarea>`, and a Save button (PUT settings, toast on success). For an
  agents room it shows a short "only for direct LLM rooms" note. Opening a
  direct room that has **no model yet** auto-switches the sidebar to
  Settings for that visit (not persisted) so a fresh room is immediately
  configurable.
- **Edit button**: in the message actions row, a pencil button — only in
  direct rooms, only on `kind == "message"` rows that aren't streaming.
  Clicking swaps the rendered body for a textarea with Save/Cancel; Save
  PUTs the edit and re-renders the bubble from the returned row. Agent
  rooms never show the button (and the server would refuse anyway).

All updates keep riding the single SSE stream; no new timers, no polling.

## Error handling

- Direct room with no model: friendly `notice` row, not a failed journal.
- Model/provider failure mid-stream: streaming rows are closed
  (`writer.finish()`), the exception journals the item `failed` — same
  contract as the unstructured agent.
- Edit conflicts with streaming: 409, client alerts.
- Settings PUT validates the uuid resolves via `resolved_model_kwargs`
  (400 on an unknown model).

## Testing

- `db/test_chat_direct.py`: room_type default + create direct room
  (membership = human + direct agent), settings roundtrip incl. clearing
  the model, `edit_chat_message` (text + content_type re-detect), edits
  rejected on missing rows.
- `webapp/test_chat_direct_api.py`: POST room with room_type; direct post
  enqueues only `DIRECT_CHAT_UUID`; agents-room post doesn't enqueue it;
  edit endpoint guards (403 agents room / 400 kind / 404); settings
  GET/PUT guards; `/chat/api/models` shape.
- `agents/test_direct_chat.py`: message-list building (system prompt
  present/absent, role mapping, full history, non-message kinds excluded);
  no-model notice path; a stubbed stream produces a posted reply.
- `webapp/test_chat_views.py` additions: markers for the room-type radio,
  Settings sidebar, and edit button symbols.
