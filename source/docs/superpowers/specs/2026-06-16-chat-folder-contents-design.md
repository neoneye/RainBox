# /chat folder-contents table — design

## Goal

On `/chat`, selecting a folder shows a table of what's inside it (its full
recursive subtree), with columns: chatroom name, agent names, message count,
last message time. This mirrors the `/cron` page's folder-details pattern.

## Reference: how `/cron` already does this

- `cronSelectedFolder` (null = root "All jobs"). First click on a folder in the
  left tree *selects* it; clicking the already-selected folder toggles
  expand/collapse (`cronFolderClick`, static/cron.js).
- The right pane shows a "Folder details" table (`#cron-rows`) of the selected
  folder's flattened subtree (`cronFlattenTree`), depth-indented. Folder rows
  carry a **Details** link that drills into the subfolder; job rows carry a
  **Details** link that opens the job.
- The kebab on a tree item is hidden unless that item is selected
  (`.cron-node.sel .cron-kebab{visibility:visible}`).

## Behavior on `/chat`

### Selection model
- New state `selectedFolder` (null = none).
- First click on a folder selects it (right pane → folder table); clicking the
  already-selected folder toggles its expand/collapse. (Today a folder click
  only toggles expand; this changes to select-first, like cron.)
- Selecting a folder clears `currentRoom`; opening a room clears
  `selectedFolder`. The right pane shows exactly one of: a chat, or a folder
  table. (Mutually exclusive.)
- The selected folder row gets a `sel` highlight.

### Kebab visibility
- Folder kebabs become visible only on the selected folder (today they are
  always visible — `buildFolderMenu`). Rooms already show their kebab only when
  active; leave that as-is.

### Folder-details table (right pane)
Rendered into the room-main area, replacing chat-log + compose while a folder is
selected. Lists the selected folder's **recursive subtree**, depth-indented:
- **Subfolder row:** folder icon + name; Agents/Messages/Last-message cells
  blank; **Details** link → select that subfolder (drill-in).
- **Room row:** `# name` · agent names (comma-separated) · message count · last
  message time; **Details** link → open that chatroom (`currentRoom`, chat view).
- Empty folder → an "empty folder" note.

The client already knows folder nesting (`folders` / `rooms` arrays,
`childFolders`, `roomsInFolder`), so the subtree is computed client-side.

## Backend: new lazy stats endpoint

`list_chatrooms()` exposes only `member_count` + `last_message_id` — not agent
names, message count, or last-message time. To avoid making the frequently
re-fetched tree load heavier (see `docs/chat-frontend-rules.md`: idle/tree load
must stay light), add a **separate** endpoint fetched only when a folder is
selected:

- **Route:** `GET /chat/api/rooms/details`
- **Returns:** JSON list of `{uuid, agents: [name, …], message_count,
  last_message_at}` for all rooms.
  - `agents`: names of the room's members whose `ChatUser.user_type != "human"`
    (join `ChatroomMember` → `ChatUser`).
  - `message_count`: `COUNT(ChatMessage.id)` grouped by `room_uuid`.
  - `last_message_at`: `MAX(ChatMessage.created_at)` grouped by `room_uuid`,
    formatted `"%Y-%m-%d %H:%M"` (matching existing message timestamps); null /
    empty string when the room has no messages.
- **DB helper:** `db.list_chatroom_details()` in `db/chat.py`, returning the
  same shape (one query per aggregate, mirroring `list_chatrooms`).

Client fetches this on folder selection and caches it in memory, refreshing on
each folder selection so counts/times stay current.

**Alternative considered:** fold these fields into `list_chatrooms()` — rejected
to keep the idle/tree load light.

## Files touched

- `db/chat.py` — add `list_chatroom_details()`.
- `webapp/chat_api.py` — add `GET /chat/api/rooms/details`.
- `webapp/chat_template.py` — selection state, folder-table view, kebab
  visibility CSS, Details links, folder-click select-vs-toggle.
- Tests: extend `webapp/test_chat_*` (or add one) covering the new endpoint
  (agents exclude the human; count + last time correct; empty room).

## Out of scope (YAGNI)

- Sorting / filtering the table.
- A root "All rooms" table (cron has "All jobs"; not requested here).
- Live-updating the table from the SSE stream (it refreshes on re-selection).
