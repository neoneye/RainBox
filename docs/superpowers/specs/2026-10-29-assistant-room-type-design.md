# Assistant rooms — design

**Date:** 2026-10-29
**Scope:** `/chat` room creation, the room-type model, the right sidebar, one
new `/settings` key.

## Problem

Starting a conversation with the assistant takes four decisions in the
New-chatroom modal: pick "Agents room", tick `assistant` among six agents,
create, then open Settings in the sidebar and link a persona. Forgetting the
last step is silent: the room answers with no persona, which is how a room
ended up giving canned "as a large language model" answers while the
operator's persona sat unused. And every one-to-one assistant conversation
carries a Members panel, a group-chat tool with nothing to do there.

## Decision

A third room type, **`assistant`**: the operator and the assistant, nobody
else, speaking with the default persona from the moment the room exists.

| Modal label | `room_type` | Members | Persona | Sidebar |
|---|---|---|---|---|
| **Assistant** (default) | `assistant` | operator + assistant, fixed | `chat.default_persona`, bound at creation | Settings, Stats, History, Export |
| **Direct** | `direct` | operator + direct-chat responder, fixed | n/a (model + system prompt) | unchanged |
| **Group chat** | `agents` | operator + chosen agents, editable | picked per member in Settings | unchanged (Members, …) |

### Stored value stays `agents` for group chat

The label changes; the stored value does not. Renaming it would mean
rewriting every existing room row, and a room's type is read by value in the
API, the agents and the tests. "Group chat" is what the operator reads;
`agents` is what the code compares.

### The persona is bound at creation, not resolved per turn

Creation copies `chat.default_persona` into the assistant member's
`persona_uuid` (following the newest revision, no pin), exactly as if the
operator had linked it in Settings afterwards. So:

- Changing the default later does not re-voice existing rooms. Each room
  keeps the persona it was made with until the operator changes it there; the
  binding is visible in that room's Settings, never inferred.
- The per-room persona picker still works unchanged: an assistant room is a
  non-direct room, which is all the persona endpoints require.
- Unset default, or a default that names a deleted persona: the room is
  created with no persona. The modal says so before Create (below), so it is
  never a surprise.

### Members are fixed

Like a direct room, the member list belongs to the type. `POST
/chat/api/rooms` ignores `member_uuids` for `assistant`, and the membership
endpoints refuse to add or remove anyone in an assistant room (400). A room
that needs a second agent is a group chat; making it one is a new room.

### Routing needs nothing

Every non-direct room already routes a human post to its responder members
(`_maybe_trigger_chat_agents`), and the assistant is one of them. Nothing in
the agents compares against `"agents"` by value.

## `chat.default_persona`

A registry key in `db/settings.py`: a persona uuid, validated to name an
existing persona, unset by default. The shipped code seeds no persona, so
which character the operator talks to stays the operator's choice; the store
ships empty. `/settings` renders it as a select over the persona tree, like
`profile.current` (persona name as the label, the uuid in the tooltip).

## UI

### New-chatroom modal

- Radios: **Assistant** · **Direct** · **Group chat**, Assistant checked.
  The folder "New room" menu opens the same modal, so it inherits this.
- The agent checklist shows only for Group chat.
- Under Assistant, one line names the persona the room will speak with, or
  says no default persona is set, with a link to `/settings`. It is fetched
  when the modal opens (`GET /chat/api/new-room-defaults` →
  `{"persona": {"uuid", "name"} | null}`), so a default changed in another
  tab is current.

### Sidebar

- **Members** is a group-chat panel: hidden in assistant and direct rooms,
  and a remembered Members mode maps to Settings there (the existing direct
  room behaviour, widened).
- **Bridge** stays direct-only.
- An assistant room is one-to-one, so it gets the text-thread layout
  (`isOneToOneRoom`) regardless of member count.

## Existing rooms

Rooms created before this keep their type: their rows are records of how
they were made. An existing agents room whose only agent is the assistant
still reads as a group chat until it is retyped. That is a deliberate per-room
operator decision, not a migration.

## Tests

- `db`: `create_chatroom` accepts `assistant`, still rejects unknown types.
- API: an assistant room's members are exactly operator + assistant whatever
  was submitted; it is born with the default persona; unset or deleted
  default gives no persona; add/remove member is 400; a post enqueues the
  assistant; `new-room-defaults` reports the persona or null.
- Settings: `chat.default_persona` rejects a non-uuid and an unknown persona.
- Page markers: the three radios, the persona line, Members hidden for
  non-group rooms.
