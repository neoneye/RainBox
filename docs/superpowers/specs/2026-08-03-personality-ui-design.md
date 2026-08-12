# Assistant personality manager (`/personality`) — design

## Problem

The assistant has no sense of who it is. Its system prompt
(`ASSISTANT_SYSTEM_PROMPT`, `agents/assistant.py`) describes *how to work* —
one step at a time, pick an action, cite sources — and says nothing about
voice, attitude or character. There is nowhere to write that down, so there is
nothing to switch between.

This feature adds a standalone editor at `/personality`: a folder tree of
personalities, one free-text body each, with every save preserved so a bad
edit is always recoverable.

**Not in this step.** The assistant's prompt is untouched, and `/chat` gains
no picker. Inserting a personality into the assistant's prompting, and picking
one from the `/chat` right-side panel, are later features that build on this
one. The store must exist and be pleasant to edit first.

## Concept: stable identity, append-only history

A **personality** is one row with one uuid that never changes, addressable at
`/personality?id=<uuid>`. Its text lives in `content`; every saved state of
that text is kept in `personality_revision`.

This is deliberately **not** `/prompt`'s model. On `/prompt`, each version is
its own row with its own uuid, cloning is the only way to make a version, and
an in-place edit genuinely rewrites the text. That gives history exactly when
the operator remembers to ask for it, and a future binding (a chat room, an
agent) points at *a version*, so editing means re-linking.

Personalities invert both:

- **Every save that changes the text appends a revision.** Nothing depends on
  the operator remembering anything.
- **The uuid is the personality, not the version.** A later `/chat` picker or
  assistant binding points at one row and keeps pointing at it across every
  edit.

### Rules

- Saving unchanged text is a no-op: no revision, no `updated_at` churn.
- **Invariant:** if a personality has any revision, the newest one's `content`
  equals the personality's `content`. So `personality.content` is the single
  read point for consumers — no join, no "latest revision" query.
- A new personality starts empty with zero revisions. The first save creates
  revision 1.
- **Restore appends, never rewinds.** Restoring revision 3 writes its text to
  `content` *and* appends a new revision holding that text. History is never
  destroyed or rewritten, so a mistaken restore is itself undoable.
- Deleting a personality cascades its revisions; deleting a folder cascades
  the personalities beneath it.
- History is unbounded. These are small texts in a single-operator app, and
  keeping everything is the entire point of the feature. A retention cap is a
  separate decision if it ever bites.

## Architecture — a port of `/prompt` (notes/ui-left-panel-tree.md §9)

| piece | file | mirrors |
|---|---|---|
| Page shell + CSS | `webapp/personality_views.py` | `webapp/prompt_views.py` |
| Page JS | `static/personality.js` | `static/prompt.js` |
| JSON API | `webapp/personality_api.py` | `webapp/prompt_api.py` |
| DB layer | `db/personality.py` | `db/prompt.py` |
| Models | `PersonalityFolder`, `Personality`, `PersonalityRevision` | `PromptFolder`, `Prompt` |

Registered in `webapp/__init__.py`, re-exported from `db/__init__.py`, and
added to `NAV_TEMPLATE` in `webapp/core.py` inside the existing **Assistant ▾**
dropdown (Runs / Second opinion / Personality) — the top bar is already at its
width budget. Tables are created by `db.create_all()` in `init_db`; no
migration step.

### Data model

Repo conventions (`notes/data-model.md`): plain UUID reference columns, **no FK
constraints**, integrity enforced in the application layer.

```python
class PersonalityFolder(db.Model):        # == PromptFolder
    __tablename__ = "personality_folder"
    id, uuid, name, description
    parent_uuid: UUID | None              # null = root
    position: int
    created_at, updated_at
    __table_args__ = (Index("personality_folder_children", "parent_uuid", "position"),)


class Personality(db.Model):
    __tablename__ = "personality"
    id, uuid, name
    content: str                          # the current text; newest revision mirrors it
    folder_uuid: UUID | None              # null = unfiled at root
    position: int
    created_at, updated_at
    __table_args__ = (Index("personality_in_folder", "folder_uuid", "position"),)


class PersonalityRevision(db.Model):
    __tablename__ = "personality_revision"
    id, uuid
    personality_uuid: UUID                # owner; plain col, no FK
    content: str                          # the full text as saved
    created_at
    __table_args__ = (Index("personality_revision_of", "personality_uuid", "id"),)
```

Revisions store the **full text**, not a delta: the texts are small, and a
full snapshot makes restore and diff trivial and immune to a corrupt chain.
Revision order is `id` (monotonic), not `created_at` — two saves inside the
same clock tick must still order deterministically.

### DB layer (`db/personality.py`)

```
personality_tree_version()                    structural fields only
personality_load_tree()                       {folders, personalities, version}
validate_personality_tree(folders, ps)        raises PersonalityTreeError
personality_save_tree(folders, ps, *, base_version)
personality_create(name, folder_uuid)         → the new row
personality_create_folder(name, parent_uuid)  → the new folder
personality_delete(uuid)                      cascades revisions
personality_delete_folder(uuid)               cascades the subtree
personality_get(uuid)                         incl. content + revision_count
personality_update_content(uuid, content)     appends a revision iff changed
personality_revisions(uuid)                   newest first, with previews
personality_revision_diff(uuid, rev_uuid)     unified diff, revision → current
personality_restore_revision(uuid, rev_uuid)  appends a new revision
```

`personality_save_tree` follows `notes/ui-tree-persistence.md`: it **only**
updates rows that already exist, and raises `PersonalityTreeError` when the
payload omits an existing folder or personality, or names one that does not
exist. It has no `expected_deletes` parameter — the shape cannot express a
deletion. A stale `base_version` raises `PersonalityTreeConflict`, checked
before structural validation so a concurrent edit surfaces as 409, not 400.

`personality_tree_version()` hashes structural fields only — uuid, name,
description, parent, position. `content`, revisions and `updated_at` are
excluded, so saving text never invalidates an open page's tree.

`validate_personality_tree` rejects: malformed uuids, duplicate ids, dangling
or cyclic folder parents, a personality whose `folderId` does not resolve, and
a uuid shared by a folder and a personality (a node is addressed globally as
`?id=<uuid>`, so a collision makes the deep link ambiguous).

## HTTP API (`webapp/personality_api.py`)

JSON, same-origin, uuids as identifiers. **Every tree-structure endpoint
returns the new tree `version`** (the tree PUT, folder/personality create,
folder/personality delete), so the client never holds a stale token. The
per-personality content PUT and the revision restore below don't touch
placement, so their responses deliberately carry no version token.

| Endpoint | Behavior |
|---|---|
| `GET /personality/api/tree` | `{folders, personalities, version}`; no content. Each personality row carries `revisionCount` — derived, so outside the version hash — which feeds the folder table's Revisions column and the delete modal's "this destroys N revisions" |
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

A revision uuid belonging to a different personality is a 404, not a diff
against a stranger's text.

## Frontend

Layout and behavior follow `notes/ui-left-panel-tree.md` (tree),
`notes/ui-tree-persistence.md` (saving), `notes/ui-modals.md`,
`notes/ui-kebab-menu.md` and `notes/ui-modal-rename.md`. The CSS is diffed
rule-by-rule against `/prompt`'s rather than eyeballed — §8 of the tree doc
lists what that catches (block-flow tree panel so the root-drop strip stays
under the tree, `/cron`'s exact node padding and icon sizes, the kebab
rendered on every row at `visibility:hidden`, 16px main-pane padding).

- **Left panel:** an "All personalities" node → `<hr>` → **+ Folder** /
  **+ Personality** → the tree → the drag-only "Move to top level" strip.
  Every row is an `<a href="/personality?id=<uuid>">` so CMD/middle-click
  opens a tab. Kebabs: Rename / Delete on both folders and personalities.
  Creation lives in the action buttons, not the kebabs. Renaming goes through
  the tree PUT (the name is a structural field), not a content save.
- **Delete confirmation:** a non-empty folder shows its subtree counts and
  requires typing its name. A personality **that has revisions** does too —
  it owns history, and the count of what is about to go is the point of the
  dialog. An empty personality (no revisions) is a plain confirm.
- **Folder view:** the recursive subtree as a table — Name / Revisions /
  Updated / Open — plus the folder's click-to-rename name and its description.
- **Personality view:** the click-to-rename name; a meta line with created,
  updated and "N revisions"; the toolbar **[Edit] [History]**; then the
  editor or the history view.
- **Editor:** CodeMirror 5 in markdown mode, line numbers, soft wrap, the `⏎`
  hard-line-end mark — read-only with a muted background until **Edit**, which
  raises it above the shared modal backdrop and greys out the rest of the page
  until **Save** or **Cancel**. No autosave: an accidental keystroke in a
  personality must never persist on its own. Esc and backdrop-click cancel
  only while the text is unchanged (`notes/ui-modals.md` dirty guard).
- **History view** replaces the editor: one row per revision — Saved at /
  Size / first line of the text — newest labeled *current*, each with **Diff**
  and **Restore**. Diff renders the unified diff inline with `/prompt`'s
  add/del/hunk colors. Restore is confirmed in a modal that states plainly
  that it appends a new revision rather than deleting anything.
- **Deep-linking:** `?id=<uuid>` selects a folder or a personality on load and
  mirrors the selection back into the URL. No per-kind params.

## Also wired

- **Flask-Admin** (`webapp/core.py`): `PersonalityFolderView`,
  `PersonalityView`, `PersonalityRevisionView` under a "Personality" category,
  each with a link to the page — mirroring the Prompt views.
- **`db/find_uuid.py`**: folder, personality and revision sources, so any of
  those uuids resolves on `/find` to `/personality?id=<personality uuid>`,
  plus a text source over `personality.content`.

## Testing

- `db/test_personality_tree.py` — tree validation (dangling, cyclic, folder/
  item uuid collision); a save omitting an existing row raises and mutates
  nothing; a save naming an unknown row raises; stale `base_version` conflicts;
  cascade deletes remove revisions and subtrees; **revision semantics**: a
  no-op save appends nothing, the newest revision always equals `content`,
  restore appends rather than rewinds, restore of a foreign revision fails.
- `webapp/test_personality_api.py` — status codes (201 / 400 / 404 / 409) and
  that the `version` returned by each POST and DELETE is accepted by the very
  next PUT.
- `webapp/test_personality_views.py` — the page shell's marker strings.
- **A real-browser pass** (headless Chrome over the DevTools Protocol, no new
  deps): drag a personality onto the root strip, open a kebab on the selected
  row, type-to-confirm a folder delete, and run Edit → Save → History → Diff →
  Restore end to end. The tree doc is explicit that marker tests and code
  review both passed `/git` while it was visibly broken.

## Deliberate tradeoffs

- **A revision per save, not per clone.** The operator asked for a safety net,
  not a discipline. The cost is a busier history list than `/prompt`'s
  ancestor chain; the benefit is that no edit is ever unrecoverable.
- **Full snapshots, not deltas.** Simplest correct thing at this size, and no
  chain to corrupt.
- **`content` duplicated on the personality row.** Denormalization bought
  deliberately: consumers read one row with no join, and the invariant that
  keeps it honest is enforced in one function.
- **Restore appends.** Non-destructive throughout — the history is a log, and
  nothing in the UI can rewrite it.
- **Separate page and tables from `/prompt`.** The two have genuinely
  different version semantics; merging them would put two history models in
  one JS file. Separate from the file-backed *persona* (`agents/persona.py`,
  `agent_profiles/personas.jsonl`) too: that is a set of runnable conversation
  agents, and this is text describing the assistant's character.
- **No seeded default.** The store ships empty; what the assistant should be
  is the operator's to write.

## Open questions

- **Where the personality lands in the assistant's prompt.** `_system_prompt()`
  (`agents/assistant.py:3484`) is the seam, but whether a personality sits
  above the working rules, below them, or inside a dedicated block — and how it
  ranks in `<source_priority>` — is a question for the wiring step, when it can
  be answered by trying it.
- **Selecting the active personality.** A `personality.current` app setting
  mirrors `profile.current`, but the `/chat` picker may want per-room
  selection. Deferred to the picker step.
- **Usage back-references.** Once something binds to a personality, the page
  will need a "what uses this?" view before delete stops being safe.
