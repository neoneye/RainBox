# Personality — design (frontend + backend)

**Status:** **Built and running.** The `/personality` page persists a folder tree of assistant personalities to Postgres, each with an append-only revision history. Page at `GET /personality`.
**Date:** 2026-08-04
**UI scope:** **Desktop-first**, same as the other tree pages.

## The idea

A **personality** is one free-text description of who the assistant is —
voice, attitude, character — addressable at `/personality?id=<uuid>` by a
uuid that stays stable for its whole life. Its text lives in `content`; every
saved state of that text is kept in `personality_revision`, an append-only log
behind every save that actually changes the text. Personalities are organized
in a folder tree (the app-wide left-panel pattern).

This is deliberately **not** `/prompt`'s model. On `/prompt`, each version is
its own row with its own uuid, cloning is the only way to make a version, and
an in-place edit genuinely rewrites the text — history exists exactly when the
operator remembers to clone. A personality inverts that: every save that
changes the text appends a revision on its own, so the safety net does not
depend on the operator remembering anything, and the uuid — not the version —
is the stable thing a future binding (a chat room, the assistant's prompt)
would point at, so editing a personality never means re-linking it.

## Where things live

| Piece | File |
|-------|------|
| Tables (`PersonalityFolder`, `Personality`, `PersonalityRevision`) | `db/models.py` |
| Tree load/validate/save/create/delete, content, revisions, diff, restore | `db/personality.py` (re-exported from the `db` facade) |
| HTTP endpoints | `webapp/personality_api.py` |
| Page shell + CSS | `webapp/personality_views.py` |
| Page logic | `static/personality.js`, served with an mtime `?v=` cache-buster |
| Tests | `db/test_personality_tree.py`, `webapp/test_personality_api.py`, `webapp/test_personality_views.py` |

## Data model

Three tables in the repo's SQLAlchemy-2.0 conventions (`docs/data-model.md`).
Reference columns are **plain UUID columns — no DB foreign keys**; integrity
is enforced in `validate_personality_tree` before any write.

```
personality_folder
  id, uuid, name, description,
  parent_uuid (nullable)          -- null = root-level folder (nesting)
  position (int), created_at, updated_at

personality
  id, uuid, name,
  content (text)                  -- the current text; newest revision mirrors it
  folder_uuid (nullable)          -- null = unfiled at root
  position (int), created_at, updated_at

personality_revision                -- ONE SAVED STATE of a personality's text
  id, uuid,
  personality_uuid                -- owner; plain col, no FK
  content (text)                  -- the full text as saved
  created_at
```

Revisions store the **full text**, not a delta: the texts are small, and a
full snapshot makes restore and diff trivial and immune to a corrupt chain.
Revision order is `id` (monotonic), not `created_at` — two saves inside the
same clock tick must still order deterministically.

### The invariant

**If a personality has any revision, the newest revision's `content` equals
the personality's `content`.** `_append_revision` (`db/personality.py`) is the
only place that maintains it — it sets `content` and appends the mirroring
revision inside one commit, and every write path (`personality_update_content`,
`personality_restore_revision`) goes through it. That makes
`personality.content` the single read point for any future consumer: no join,
no "latest revision" query.

- Saving unchanged text is a no-op: no revision, no `updated_at` churn.
- A new personality starts empty with zero revisions. The first save creates
  revision 1.
- **Restore appends, never rewinds.** Restoring an old revision writes its
  text to `content` *and* appends a new revision holding that text. History is
  never destroyed or rewritten, so a mistaken restore is itself undoable.
- Restoring text that is already current changes nothing (`changed: false`).
- Deleting a personality cascades its revisions; deleting a folder cascades
  the personalities beneath it (and their revisions).
- History is unbounded. These are small texts in a single-operator app, and
  keeping everything is the entire point of the feature. A retention cap is a
  separate decision if it ever bites.

### Tree persistence

The page hydrates from `GET /personality/api/tree` and saves structural
changes back as a debounced whole-tree PUT. This is the first page built to
`docs/ui-tree-persistence.md`'s standard: **the tree PUT only updates rows
that already exist** — a payload that omits an existing row, or names one the
database doesn't have, is a 400 (`PersonalityTreeError`), not a silent delete
or create. Creation and deletion are their own endpoints
(`personality_create` / `personality_create_folder` /
`personality_delete` / `personality_delete_folder`), and **every tree-structure
endpoint returns a fresh `version` token**, not just the PUT. Content saves
deliberately return no token, since text lives outside the version hash. Because the PUT
cannot delete, there is no `deletes` counter — that tripwire only exists to
catch a payload that lies about its own intent, and this shape cannot express
deletion in the first place.

`personality_tree_version()` hashes structural fields only — uuid, name,
description, parent, position (and, for personalities, folder placement).
`content`, revisions and `updated_at` are excluded, so saving a personality's
text never invalidates an open page's tree, and `revisionCount` (derived, for
the folder table and the delete modal) stays out of the hash too.

`validate_personality_tree` rejects: malformed uuids, duplicate ids, dangling
or cyclic folder parents, a personality whose `folderId` does not resolve, and
a uuid shared by a folder and a personality — a node is addressed globally as
`?id=<uuid>`, so a collision would make the deep link ambiguous.

## HTTP API (`webapp/personality_api.py`)

JSON, same-origin, uuids as identifiers.

| Endpoint | Behavior |
|---|---|
| `GET /personality/api/tree` | `{folders, personalities, version}`; no content. Each personality row carries `revisionCount` |
| `PUT /personality/api/tree` | Name/placement/order of existing rows. 409 stale token; 400 on a missing or unknown uuid |
| `POST /personality/api/folders` | `{name, parentId}` → 201 `{folder, version}` |
| `POST /personality/api/personalities` | `{name, folderId}` → 201 `{personality, version}` |
| `DELETE /personality/api/folders/<uuid>` | Cascades the subtree → `{ok, version}` |
| `DELETE /personality/api/personalities/<uuid>` | Cascades its revisions → `{ok, version}` |
| `GET /personality/api/personalities/<uuid>` | One personality incl. `content`, `revisionCount`, timestamps |
| `PUT /personality/api/personalities/<uuid>` | `{content}` → `{ok, changed, revision}`; `changed: false` when the text was identical |
| `GET /personality/api/personalities/<uuid>/revisions` | Newest first: `{uuid, created_at, bytes, lines, preview, current}` |
| `GET /personality/api/personalities/<uuid>/revisions/<rev>/diff` | Unified diff (3 context lines), that revision → current |
| `POST /personality/api/personalities/<uuid>/revisions/<rev>/restore` | Appends a new revision → `{ok, changed, content, revision}`; `changed: false` when the revision's text was already current |

A revision uuid belonging to a different personality is a 404 on both the
diff and restore routes, never a diff or restore against a stranger's text.

## Frontend

Layout and behavior follow `docs/ui-left-panel-tree.md` (tree),
`docs/ui-tree-persistence.md` (saving), `docs/ui-modals.md`,
`docs/ui-kebab-menu.md` and `docs/ui-modal-rename.md`.

- **Left panel:** an "All personalities" node → `<hr>` → **+ Folder** /
  **+ Personality** → the tree → the drag-only "Move to top level" strip
  directly under the tree. Every row is an `<a href="/personality?id=<uuid>">`
  so CMD/middle-click opens a tab. The kebab appears only on the selected row
  (Rename / Delete on both folders and personalities); creation lives in the
  action buttons, not the kebabs. Renaming goes through the tree PUT (the name
  is a structural field), not a content save.
- **Delete confirmation:** a non-empty folder shows its subtree counts and
  requires typing its name. A personality **that has revisions** does too —
  it owns history, and the count of what is about to go is the point of the
  dialog. An empty personality (no revisions) is a plain confirm.
- **Folder table:** the selected folder's subtree (or the whole tree at the
  root), depth-indented — Name / Revisions / Updated / Open — plus the
  folder's click-to-rename name and its description.
- **Editor:** the text is **read-only until Edit** — CodeMirror 5 in markdown
  mode, muted background, no cursor, until the toolbar's **Edit** button
  raises the editor above the shared modal backdrop and greys out the rest of
  the page. **No autosave**: an accidental keystroke in a personality must
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
- **Deep-linking:** `?id=<uuid>` selects a folder or a personality on load and
  mirrors the selection back into the URL.

## Deliberate tradeoffs

- **A revision per save, not per clone.** The cost is a busier history list
  than `/prompt`'s ancestor chain; the benefit is that no edit is ever
  unrecoverable without the operator having to think to protect it.
- **Full snapshots, not deltas.** Simplest correct thing at this size, and no
  chain to corrupt.
- **`content` duplicated on the personality row.** Denormalization bought
  deliberately: consumers read one row with no join, and the invariant that
  keeps it honest is enforced in exactly one function, `_append_revision`.
- **Restore appends.** Non-destructive throughout — the history is a log, and
  nothing in the UI can rewrite it.
- **Uuid-stable identity, not version-stable.** A future binding points at the
  personality, not a point-in-time text, so editing never requires re-linking
  — the tradeoff `/prompt` deliberately did not make, because its consumers
  want to freeze a specific version.
- **No seeded default.** The store ships empty; what the assistant should be
  is the operator's to write.

## Open questions

- **Where a personality lands in the assistant's prompt.** `_system_prompt()`
  (`agents/assistant.py:3484`) is the seam, but whether a personality sits
  above the working rules, below them, or inside a dedicated block — and how
  it ranks against everything else already assembled there — is a question
  for the wiring step, when it can be answered by trying it.
- **Selecting the active personality.** A `personality.current` app setting
  (mirroring `profile.current`) is the simplest binding, but a per-room picker
  in `/chat`'s right-side panel may be what operators actually want. Neither
  exists yet.
- **Usage back-references.** Nothing binds to a personality yet, so nothing
  needs to warn before delete. Once something does, the page will need a
  "what uses this?" view — until then, delete is only safe while nothing
  points at a personality.
