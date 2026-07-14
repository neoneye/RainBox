# Direct chat — design

**Status: built and running.** A direct room (`room_type='direct'`) is a
one-to-one conversation between the operator and a single model, LM
Studio-style: the model sees the entire room history as proper
system/user/assistant chat messages and replies with one streamed plain-text
completion. No structured output, no tools, no memory retrieval, no persona.
This is the deliberate counterpoint to the agents rooms, where responders get
an IRC-style transcript and decide *whether* to reply — the direct room's
model always replies, to everything.

The transcript stays in `chat_message` like any other room; the feature adds
no tables. Everything room-specific lives in four `chatroom` columns —
`model_uuid`, `system_prompt`, `prompt_uuid`, `request_timeout` — plus one
global setting (`chat.default_model`).

## Room shape and creation

`POST /chat/api/rooms` with `room_type: "direct"` creates the room
(`webapp/chat_api.py`). Membership is fixed: the operator plus the
direct-chat responder (`DIRECT_CHAT_UUID` from `agents/config.py`); any
submitted `member_uuids` are ignored. Everything else about the room —
folder placement, renaming, deletion, the left-panel tree — is shared with
agents rooms.

## A turn, end to end

1. The operator posts. `POST /chat/api/rooms/<uuid>/messages` stores the row,
   then `_maybe_trigger_direct_chat` enqueues one inbox item for the
   direct-chat agent, payload `{room_uuid, message_uuid}`. The human-only
   guard (sender must be `user_type='human'`) is what prevents loops: the
   model's reply is posted directly by the agent, never through this
   endpoint's trigger path.
2. The supervisor (`main.py`) spawns the agent process;
   `agents/__main__.py` resolves the `direct_chat` kind to `DirectChatAgent`
   (`agents/direct_chat.py`).
3. `handle()` resolves the model (next section). With no model it posts a
   `kind="notice"` nudge instead of failing the journal — notices are
   excluded from transcripts, so the model never sees it.
4. `build_messages()` builds the LLM message list: optional system message
   (blank = none), then every `kind='message'` row oldest-first — human rows
   as `user`, everything else as `assistant`. No window: the model sees the
   whole room. `thinking` / `notice` / `progress` rows are skipped.
5. `_stream_reply()` streams the completion into live chat rows (see
   Streaming below) and the item journals `completed` with the reply text.

## Model resolution

Per turn, in order:

1. **The room's own pick** — `chatroom.model_uuid`, a `ModelConfig` *or*
   `ModelConfigOverride` uuid (`db.resolved_model_kwargs` accepts either),
   chosen in the room's Settings sidebar. Changing it affects only that room.
2. **The global default** — the `chat.default_model` setting (`db/settings.py`).
   Its own unset fallback is dynamic: the alphabetically earliest model
   config override, by picker label `provider · config — override`
   (`db.model_config.default_chat_model_uuid`). So a fresh install with at
   least one override answers direct rooms with zero configuration. The
   /settings page edits this key with a model dropdown
   (`db.chat_model_choices` supplies the options).
3. **Neither resolves** — the agent posts the no-model notice, and the /chat
   client auto-opens the Settings sidebar for model-less direct rooms only
   when there is no global default either (the tree payload carries
   `default_model_uuid` for this check).

Both layers are read fresh each turn, so changes apply from the next reply
mid-conversation. A stale global default (its model deleted since) degrades
to the notice, not a failed journal.

`DirectChatAgent` sets `uses_model_group = False` (`agents/base.py`): it
never reads an /agentmodel binding, so that page hides it.

## System prompt

`db.resolve_room_system_prompt` (in `db/chat.py`) picks the prompt a turn
actually sends:

- **Linked stored prompt** — `chatroom.prompt_uuid` names a version on the
  /prompt page. It wins over free text and its *content is resolved fresh
  each turn*, so editing the stored version applies from the next reply in
  every room that links it. If the linked version was deleted, the room
  sends **no** system message (rather than silently reviving stale free
  text); the sidebar shows "(deleted prompt)".
- **Free text** — otherwise `chatroom.system_prompt`, the sidebar textarea.
  Empty = no system message.

Linking keeps the free text stored, so Unlink restores it.

## Streaming

`_stream_reply` drives `StreamingReplyWriter` (`chat/streaming.py`), which
owns up to two in-place rows per turn: a `kind="thinking"` row for the
model's reasoning channel and a `kind="message"` row for the answer. Each
row is created lazily on its stream's first token, grown in place, and
flushed (persist + Postgres NOTIFY) on a throttle, so open tabs render a
smooth live update without a write per token. Oversized NOTIFY payloads fall
back to an id-only notification and the browser refetches the row
(`GET /chat/api/rooms/<uuid>/messages/<id>`).

Details that live here:

- `extract_stream_deltas` covers both provider stream shapes (OpenAI-compat
  `reasoning_content`/`content` deltas and native Ollama `thinking_delta`).
- Reasoning-only models that emit the answer inside `</think>` tails get it
  recovered into the message row at finish (`_answer_from_reasoning`).
- `decode_byte_escape_runs` repairs byte-fallback notation
  (`<0xE2><0x96><0xA8>` → `▨`) once at finish.
- One model, no fallback list. The wall-clock deadline is a soft bound
  checked between chunks: the room's `request_timeout` override when set,
  else the model config's `request_timeout`/`timeout`, else 60s. The
  override is also handed to the HTTP client under the field each path
  reads (`request_timeout` for native Ollama, `timeout` for the
  OpenAI-compat clients), so the socket read timeout grows with it. Any
  failure closes the streaming rows (no stuck cursor), posts a
  `kind="notice"` failure message into the room — exception type, model,
  elapsed seconds — and the journal records `failed`. The notice matters for
  errors that strike before the first token (a `ReadTimeout` while a cold
  model loads, a room model that no longer resolves): without it the room
  would stay silent.

## Settings sidebar

The right panel remembers which panel is selected and whether it is shown as
two separate localStorage keys (`chat.sidebarMode`, `chat.sidebarVisible`),
so hiding the sidebar — or Ctrl+1, which toggles visibility — never
loses the panel choice. The two room-type-specific panels map to each other
when navigating (a direct room shows Settings where an agents room shows
Members, and the inapplicable option is hidden from the select); Stats and
Export carry over unchanged. Switching room types therefore never closes
the sidebar.

The right panel's Settings mode (direct rooms only;
`webapp/chat_template.py` `renderDirectSettings`) edits the room's knobs via
`GET/PUT /chat/api/rooms/<uuid>/settings`: the model picker (its empty
option reads `(default — <model>)` when a global default exists, `(no
model)` otherwise), the request-timeout field (empty = the model config's
default; raise it for long conversations), the prompt source (linked
version vs custom text), and Save. The PUT validates that `model_uuid`
names a real config/override, `prompt_uuid` a real stored prompt, and
`request_timeout` is a positive integer or null. Settings apply from the
next reply.

## Editing the transcript

Direct rooms are the operator's scratchpad, so history is rewritable —
`PUT`/`DELETE /chat/api/rooms/<uuid>/messages/<id>` are refused in agents
rooms but allowed here (`edit_chat_room_message`):

- **Edit** (`db.edit_chat_message`): `kind='message'` rows only, not while
  streaming. Open tabs update the bubble in place via the existing streaming
  upsert path.
- **Delete** (`db.delete_chat_message`): every kind (notices and thinking
  rows included), not while streaming. Open tabs drop the bubble live.

Neither triggers a model turn; the rewritten history is simply what the
model sees on the *next* operator message.

## Key files

| Area | File |
| --- | --- |
| Agent | `agents/direct_chat.py` |
| Streaming writer + delta extraction | `chat/streaming.py` |
| Room settings, triggers, editing (HTTP) | `webapp/chat_api.py` |
| Sidebar + client behavior | `webapp/chat_template.py` |
| Prompt resolution, edit/delete, tree | `db/chat.py` |
| Model choices + alphabetical default | `db/model_config.py` |
| `chat.default_model` setting | `db/settings.py` |

Tests: `agents/test_direct_chat.py` (message building, model fallback,
notice), `webapp/test_chat_direct_api.py` (HTTP surface),
`db/test_chat_direct.py` (room/settings persistence),
`db/test_model_config_default.py` (the alphabetical pick),
`chat/test_streaming.py` (writer + delta shapes).
