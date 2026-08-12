# Editable chatroom membership — design

**Date:** 2026-06-15
**Status:** Approved, pending implementation

## Problem

On the `/chat` page, chatroom membership (which agents belong to a room) can
only be set while creating a new room. After creation the right-panel "members"
view is read-only (`renderMembers()` in `webapp/chat_template.py`). There is no
backend endpoint to add or remove a member after creation — to change membership
today you must delete and recreate the room.

We want to edit a room's agent membership directly from the right panel.

## Decisions

- **Interaction:** inline toggles. The members panel lists *every* agent with a
  checkbox; toggling a checkbox immediately adds/removes that agent from the
  current room. No separate edit mode or save button.
- **Message history:** keep all history. Removing an agent does not touch its
  past messages (`chat_message.sender_uuid` is not a foreign key, so this is the
  natural behavior). Removal only affects future participation.
- **Human member:** the human creator is fixed — always a member, rendered
  read-only without a toggle. The backend also rejects removing a human as
  defense-in-depth so a room can never be orphaned.

## Backend

### Endpoints (`webapp/chat_api.py`)

Granular add/remove chosen over a single "replace whole set" PUT, because it
maps 1:1 to a checkbox toggle and avoids races between two open tabs.

- `POST /chat/api/rooms/<room_uuid>/members` — body `{user_uuid}`. Adds the
  agent to the room. Idempotent: the existing `(room_uuid, user_uuid)` unique
  index backs an insert-if-absent. Returns `204` (or `200`). `404` if the room
  does not exist; `400` if `user_uuid` is missing/invalid.
- `DELETE /chat/api/rooms/<room_uuid>/members/<user_uuid>` — removes the member.
  Returns `204`. `404` if the room does not exist. `409` if the target user is a
  human (cannot remove the human/creator).

### DB helpers (`db/chat.py`)

Placed next to the existing membership functions
(`list_room_members`, `get_room_member_uuids`, `create_chatroom`):

- `add_room_member(room_uuid, user_uuid)` — insert the join row if absent, in one
  transaction. Idempotent.
- `remove_room_member(room_uuid, user_uuid)` — delete the join row; return whether
  a row was actually deleted.

The human-removal guard lives in the API handler (it needs to look up the target
user's `user_type`); the DB helper stays a thin data operation.

## Frontend (`webapp/chat_template.py`)

`renderMembers()` changes from "list current members" to "list all agents as
toggles":

1. Fetch both the agents list (`GET /chat/api/agents`) and the current room
   members (`GET /chat/api/rooms/<uuid>/members`) together.
2. Render humans first, read-only (no toggle).
3. Render every agent with a checkbox, checked when the agent is currently a
   member.
4. On toggle: optimistically flip the checkbox, fire `POST`/`DELETE`. On failure,
   revert the checkbox. On success, update the room's member-count in the left
   room list locally.

No SSE push for membership changes (YAGNI for a single-operator app). A second
open tab picks up the change the next time it reloads the sidebar. This is a
known, accepted limitation.

## Testing

Extend the existing API test file (`webapp/test_chat_feedback_api.py`, or a new
sibling test module if cleaner) following its patterns:

- add a member → appears in `list_room_members`
- add an already-present member → idempotent, no error, no duplicate
- remove a member → gone from `list_room_members`, past messages untouched
- remove from unknown room → `404`
- add to unknown room → `404`
- remove a human → `409`, human still a member

## Out of scope

- Per-agent roles or permissions
- Membership history / audit log
- Bulk "select all" / "clear all"
- Member reordering
- Real-time (SSE) propagation of membership changes to other clients
