# Persona — design (frontend + backend)

**Status:** **Built and running.** The `/persona` page persists a folder tree of assistant personas to Postgres, each with an append-only revision history. Page at `GET /persona`.
**Date:** 2026-08-04
**UI scope:** **Desktop-first**, same as the other tree pages.

## The idea

A **persona** is one free-text description of who the assistant is —
voice, attitude, character — addressable at `/persona?id=<uuid>` by a
uuid that stays stable for its whole life. Its text lives in `content`; every
saved state of that text is kept in `persona_revision`, an append-only log
behind every save that actually changes the text. Personas are organized
in a folder tree (the app-wide left-panel pattern).

This is deliberately **not** `/prompt`'s model. On `/prompt`, each version is
its own row with its own uuid, cloning is the only way to make a version, and
an in-place edit genuinely rewrites the text — history exists exactly when the
operator remembers to clone. A persona inverts that: every save that
changes the text appends a revision on its own, so the safety net does not
depend on the operator remembering anything, and the uuid — not the version —
is the stable thing a future binding (a chat room, the assistant's prompt)
would point at, so editing a persona never means re-linking it.

## Where things live

| Piece | File |
|-------|------|
| Tables (`PersonaFolder`, `Persona`, `PersonaRevision`) | `db/models.py` |
| Tree load/validate/save/create/delete, content, revisions, diff, restore | `db/persona.py` (re-exported from the `db` facade) |
| HTTP endpoints | `webapp/persona_api.py` |
| Page shell + CSS | `webapp/persona_views.py` |
| Page logic | `static/persona.js`, served with an mtime `?v=` cache-buster |
| Tests | `db/test_persona_tree.py`, `webapp/test_persona_api.py`, `webapp/test_persona_views.py` |

## Data model

Three tables in the repo's SQLAlchemy-2.0 conventions (`docs/data-model.md`).
Reference columns are **plain UUID columns — no DB foreign keys**; integrity
is enforced in `validate_persona_tree` before any write.

```
persona_folder
  id, uuid, name, description,
  parent_uuid (nullable)          -- null = root-level folder (nesting)
  position (int), created_at, updated_at

persona
  id, uuid, name,
  content (text)                  -- the current text; newest revision mirrors it
  folder_uuid (nullable)          -- null = unfiled at root
  position (int), created_at, updated_at

persona_revision                -- ONE SAVED STATE of a persona's text
  id, uuid,
  persona_uuid                -- owner; plain col, no FK
  content (text)                  -- the full text as saved
  created_at
```

Revisions store the **full text**, not a delta: the texts are small, and a
full snapshot makes restore and diff trivial and immune to a corrupt chain.
Revision order is `id` (monotonic), not `created_at` — two saves inside the
same clock tick must still order deterministically.

### The invariant

**If a persona has any revision, the newest revision's `content` equals
the persona's `content`.** `_append_revision` (`db/persona.py`) is the
only place that maintains it — it sets `content` and appends the mirroring
revision inside one commit, and every write path (`persona_update_content`,
`persona_restore_revision`) goes through it. That makes
`persona.content` the single read point for any future consumer: no join,
no "latest revision" query.

- Saving unchanged text is a no-op: no revision, no `updated_at` churn.
- A new persona starts empty with zero revisions. The first save creates
  revision 1.
- **Restore appends, never rewinds.** Restoring an old revision writes its
  text to `content` *and* appends a new revision holding that text. History is
  never destroyed or rewritten, so a mistaken restore is itself undoable.
- Restoring text that is already current changes nothing (`changed: false`).
- Deleting a persona cascades its revisions; deleting a folder cascades
  the personas beneath it (and their revisions).
- History is unbounded. These are small texts in a single-operator app, and
  keeping everything is the entire point of the feature. A retention cap is a
  separate decision if it ever bites.

### Tree persistence

The page hydrates from `GET /persona/api/tree` and saves structural
changes back as a debounced whole-tree PUT. This is the first page built to
`docs/ui-tree-persistence.md`'s standard: **the tree PUT only updates rows
that already exist** — a payload that omits an existing row, or names one the
database doesn't have, is a 400 (`PersonaTreeError`), not a silent delete
or create. Creation and deletion are their own endpoints
(`persona_create` / `persona_create_folder` /
`persona_delete` / `persona_delete_folder`), and **every tree-structure
endpoint returns a fresh `version` token**, not just the PUT. Content saves
deliberately return no token, since text lives outside the version hash. Because the PUT
cannot delete, there is no `deletes` counter — that tripwire only exists to
catch a payload that lies about its own intent, and this shape cannot express
deletion in the first place.

`persona_tree_version()` hashes structural fields only — uuid, name,
description, parent, position (and, for personas, folder placement).
`content`, revisions and `updated_at` are excluded, so saving a persona's
text never invalidates an open page's tree, and `revisionCount` (derived, for
the folder table and the delete modal) stays out of the hash too.

`validate_persona_tree` rejects: malformed uuids, duplicate ids, dangling
or cyclic folder parents, a persona whose `folderId` does not resolve, and
a uuid shared by a folder and a persona — a node is addressed globally as
`?id=<uuid>`, so a collision would make the deep link ambiguous.

## HTTP API (`webapp/persona_api.py`)

JSON, same-origin, uuids as identifiers.

| Endpoint | Behavior |
|---|---|
| `GET /persona/api/tree` | `{folders, personas, version}`; no content. Each persona row carries `revisionCount` |
| `PUT /persona/api/tree` | Name/placement/order of existing rows. 409 stale token; 400 on a missing or unknown uuid |
| `POST /persona/api/folders` | `{name, parentId}` → 201 `{folder, version}` |
| `POST /persona/api/personas` | `{name, folderId}` → 201 `{persona, version}` |
| `DELETE /persona/api/folders/<uuid>` | Cascades the subtree → `{ok, version}` |
| `DELETE /persona/api/personas/<uuid>` | Cascades its revisions → `{ok, version}` |
| `GET /persona/api/personas/<uuid>` | One persona incl. `content`, `revisionCount`, timestamps |
| `PUT /persona/api/personas/<uuid>` | `{content}` → `{ok, changed, revision}`; `changed: false` when the text was identical |
| `GET /persona/api/personas/<uuid>/revisions` | Newest first: `{uuid, created_at, bytes, lines, preview, current}` |
| `GET /persona/api/personas/<uuid>/revisions/<rev>/diff` | Unified diff (3 context lines), that revision → current |
| `POST /persona/api/personas/<uuid>/revisions/<rev>/restore` | Appends a new revision → `{ok, changed, content, revision}`; `changed: false` when the revision's text was already current |

A revision uuid belonging to a different persona is a 404 on both the
diff and restore routes, never a diff or restore against a stranger's text.

## Frontend

Layout and behavior follow `docs/ui-left-panel-tree.md` (tree),
`docs/ui-tree-persistence.md` (saving), `docs/ui-modals.md`,
`docs/ui-kebab-menu.md` and `docs/ui-modal-rename.md`.

- **Left panel:** an "All personas" node → `<hr>` → **+ Folder** /
  **+ Persona** → the tree → the drag-only "Move to top level" strip
  directly under the tree. Every row is an `<a href="/persona?id=<uuid>">`
  so CMD/middle-click opens a tab. The kebab appears only on the selected row
  (Rename / Delete on both folders and personas); creation lives in the
  action buttons, not the kebabs. Renaming goes through the tree PUT (the name
  is a structural field), not a content save.
- **Delete confirmation:** a non-empty folder shows its subtree counts and
  requires typing its name. A persona **that has revisions** does too —
  it owns history, and the count of what is about to go is the point of the
  dialog. An empty persona (no revisions) is a plain confirm.
- **Folder table:** the selected folder's subtree (or the whole tree at the
  root), depth-indented — Name / Revisions / Updated / Open — plus the
  folder's click-to-rename name and its description.
- **Editor:** the text is **read-only until Edit** — CodeMirror 5 in markdown
  mode, muted background, no cursor, until the toolbar's **Edit** button
  raises the editor above the shared modal backdrop and greys out the rest of
  the page. **No autosave**: an accidental keystroke in a persona must
  never persist on its own. **Save** PUTs the content and toasts
  "saved — version N"; a save of identical text toasts "no changes" and
  appends nothing. **Cancel** restores the snapshot taken at Edit time. Esc
  and backdrop-click follow the `docs/ui-modals.md` dirty guard — they cancel
  only while the text is unchanged.
- **History view** replaces the editor: one row per revision — Saved at /
  Size / first line — newest labeled *current*, each with **Diff** and
  **Restore** (Restore hidden on the current row). Diff renders the unified
  diff inline with add/del/hunk colors. Restore is confirmed in a modal that
  states plainly that it appends a new version rather than deleting anything.
- **Deep-linking:** `?id=<uuid>` selects a folder or a persona on load and
  mirrors the selection back into the URL.

## Deliberate tradeoffs

- **A revision per save, not per clone.** The cost is a busier history list
  than `/prompt`'s ancestor chain; the benefit is that no edit is ever
  unrecoverable without the operator having to think to protect it.
- **Full snapshots, not deltas.** Simplest correct thing at this size, and no
  chain to corrupt.
- **`content` duplicated on the persona row.** Denormalization bought
  deliberately: consumers read one row with no join, and the invariant that
  keeps it honest is enforced in exactly one function, `_append_revision`.
- **Restore appends.** Non-destructive throughout — the history is a log, and
  nothing in the UI can rewrite it.
- **Uuid-stable identity, not version-stable.** A future binding points at the
  persona, not a point-in-time text, so editing never requires re-linking
  — the tradeoff `/prompt` deliberately did not make, because its consumers
  want to freeze a specific version.
- **No seeded default.** The store ships empty; what the assistant should be
  is the operator's to write.

## Wired to the assistant

The binding lives on the room **member**, not the room: `chatroom_member`
carries `persona_uuid` (which persona this participant speaks with; null =
none) and `persona_revision_uuid` (null = follow the persona's newest
revision, the default; set = pinned to that exact revision, so further edits
on `/persona` stop reaching this member until the pin is released). It sits
on the member row rather than the room because a room can hold more than one
assistant, and each speaks with its own voice — one room, several personas.

`db.resolve_member_persona(room_uuid, user_uuid)` (`db/chat.py`) resolves the
binding fresh on every turn — no caching, no re-linking needed after an edit.
A non-member, an unlinked member, a deleted persona, a pin whose revision is
gone, and a persona that was never saved all resolve to no persona block,
matching `resolve_room_system_prompt`'s fail-obvious shape (see
`docs/direct-chat.md`): the member visibly has no voice rather than quietly
using text the operator thought they had replaced. Following members get the
persona's current `content` (the newest revision, by the invariant above);
either way the resolution stamps the `PersonaRevision.uuid` that produced the
text, so the turn always records exactly which revision it used.

In the assistant's per-turn prompt (`agents/assistant.py`), a non-empty
resolution renders as a `<persona authority="voice">` element holding the
persona's text, ranked in `SOURCE_PRIORITY_SECTION` next to
`formatting_guide` — below the current request and this turn's observations,
above profile and conversation history. The system prompt carries one
code-owned sentence policing the boundary: a persona changes voice and
manner, never which actions are available, and never overrides the working
rules or the source priority — operator-authored text, but still data inside
the prompt, not a command. Every step's turn log (`_build_turn_log`) carries
a `persona` entry — name, uuid, `href` to `/persona?id=<uuid>`, and the
revision used — or `(none)` when the member carries no persona.

The picker lives in the `/chat` sidebar's Settings mode (`renderAgentsSettings`
in `webapp/chat_template.py`), one section per persona-capable member:
link/change a persona, pin to an older revision from that persona's history,
release the pin with Follow newest, or unlink back to none. It reads and
writes through `GET /chat/api/rooms/<uuid>/personas` and
`PUT /chat/api/rooms/<uuid>/members/<user_uuid>/persona`
(`webapp/chat_api.py`), gated to `PERSONA_CAPABLE_UUIDS` (today, the
assistant). A deleted linked persona renders as a red `(deleted)` link to
`/persona?id=<uuid>` in the sidebar rather than silently reverting to none —
the operator has to notice and relink or unlink. Direct rooms are untouched:
they keep their own model + system-prompt Settings panel, no persona section.

## Open questions

- **Usage back-references.** A persona can now be bound from a room member,
  so deleting one can leave a room without a voice mid-conversation with no
  warning. The page still has no "what uses this?" view — until it does,
  delete is safe only while the operator remembers what points at a persona,
  and that risk is higher now than when nothing could bind to one.
