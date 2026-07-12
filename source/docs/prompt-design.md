# Prompt — design (frontend + backend)

**Status:** **Built and running.** The `/prompt` page persists a folder tree of versioned system prompts to Postgres; direct chat rooms link to a prompt version and resolve its content fresh on every model turn. Page at `GET /prompt`.
**Date:** 2026-07-12
**UI scope:** **Desktop-first**, same as the other tree pages.

## The idea

A **prompt** is one version of a system prompt for an LLM persona ("a terse
coding assistant", "a quizmaster", …). Versions are immutable-ish by
convention: **cloning is the only way to make a new version**, and each clone
records which version it was based on, so a prompt's edit history is its
ancestor chain and any two versions in a lineage can be diffed. Prompts are
organized in a folder tree (the app-wide left-panel pattern), and a direct
chat room can **link** to a version — the room then always speaks with that
version's current text.

The page exists to answer two operator questions cheaply: *"what did I change
between the version that worked and this one?"* (clone + diff) and *"which
prompt is this chat actually using?"* (the room's Settings sidebar links here).

## Where things live

| Piece | File |
|-------|------|
| Tables (`PromptFolder`, `Prompt`) | `db/models.py` |
| Tree load/validate/save, content, clone, ancestors, diff | `db/prompt.py` (re-exported from the `db` facade) |
| HTTP endpoints | `webapp/prompt_api.py` |
| Page shell + CSS | `webapp/prompt_views.py` |
| Page logic | `static/prompt.js`, served with an mtime `?v=` cache-buster |
| Direct-chat consumption | `db/chat.py` `resolve_room_system_prompt`, `agents/direct_chat.py` |
| Room ↔ prompt linking UI | `/chat` Settings sidebar (`webapp/chat_template.py`) |
| Tests | `db/test_prompt_tree.py`, `webapp/test_prompt_api.py`, `webapp/test_prompt_views.py`, `db/test_chat_direct.py` |

## Data model

Two tables in the repo's SQLAlchemy-2.0 conventions (`docs/data-model.md`).
Reference columns are **plain UUID columns — no DB foreign keys**; integrity
is enforced in `validate_prompt_tree` before any write.

```
prompt_folder
  id, uuid, name, description,
  parent_uuid (nullable)          -- null = root-level folder (nesting)
  position (int), created_at, updated_at

prompt                            -- ONE VERSION of a system prompt
  id, uuid, name,
  content (text)                  -- the system prompt text itself
  parent_uuid (nullable)          -- the version this was cloned from; null = lineage root
  folder_uuid (nullable)          -- null = unfiled at root
  position (int), created_at, updated_at
```

Two different "parent" notions, deliberately kept apart:

- `folder_uuid` is **location** (where the version sits in the tree).
- `parent_uuid` is **lineage** (which version it was cloned from). It is the
  only history mechanism — there is no separate revisions table. It may
  legitimately **dangle** after the parent version is deleted; the UI degrades
  to "(deleted)" and the validator does not reject it.

### Tree persistence (the /git and /cron pattern)

The page hydrates from `GET /prompt/api/tree` and saves structural changes
back as a debounced **whole-tree PUT** — an upsert by uuid where list order
becomes `position` and rows absent from the payload are deleted. The same two
guards as /cron protect the whole-tree-replace foot-gun:

- **`version`** — an optimistic-concurrency token (sha256 over the structural
  fields only). Stale token → **409**, and the page re-hydrates instead of
  clobbering another writer. `content` is excluded from the hash, so saving a
  prompt's text never invalidates an open page's tree.
- **`deletes`** — the page declares how many deletions it knowingly performed;
  a save that would delete more is refused with 400 (a truncated payload from
  a frontend bug, not an edit).

Validation (`validate_prompt_tree`, before any mutation): well-formed uuids,
no duplicate ids, no dangling/cyclic folder parents, prompt `folderId` must
resolve, and a prompt uuid must never collide with a folder id — a node is
identified globally by uuid (`/prompt?id=<uuid>`), so a collision would make
the deep link ambiguous. `parentUuid` (lineage) is exempt: it may dangle.

Crucially, **the tree save never touches `content`**: new rows start empty,
existing rows keep theirs. Content flows only through the per-prompt PUT.

## Versioning: clone, lineage, diff

- **Clone** (`POST /prompt/api/prompts/<uuid>/clone` → `db.prompt_clone`)
  copies the content into a new row whose `parent_uuid` records the source,
  placed in the same folder immediately after it. The clone's **name is
  derived by incrementing a trailing number** — "Daily quiz 73" → "Daily quiz
  74", zero-padding preserved ("take 09" → "take 10"), " 2" appended when
  there is no number — counting past any name already taken so repeated clones
  stay distinct.
- **Ancestors** (`db.prompt_ancestors`) walk `parent_uuid` upward — parent,
  grandparent, … — stopping at a lineage root, a dangling reference, a cycle,
  or a hop cap (a corrupt loop must not spin).
- **Diff** (`GET /prompt/api/prompts/<uuid>/diff?against=<ancestor>`) returns
  a unified diff (3 context lines) of an ancestor's content against this
  version's; `against` defaults to the immediate parent and must be an
  ancestor. The editor pane renders it with add/del/hunk line colors and a
  picker over the whole ancestor chain.

## Content editing: explicit Edit → Save / Cancel

Prompt text is **read-only by default** (muted background, no cursor). There
is **no autosave** — an accidental keystroke in a system prompt must never
persist on its own. The toolbar's **Edit** button starts an edit:

- The editor (a CodeMirror markdown editor with line numbers, soft wrap, and a
  `⏎` mark on hard line ends) is raised above the shared modal backdrop; the
  rest of the page is grayed out and non-clickable until the edit is resolved.
- **Save** PUTs the content (`PUT /prompt/api/prompts/<uuid>`, last write
  wins) and toasts; **Cancel** restores the snapshot taken at Edit time.
- Esc / backdrop-click follow the `docs/ui-modals.md` dirty guard: they cancel
  only while the text is unchanged. Closing the tab with unsaved changes
  triggers the browser's leave-page warning.

Renaming a prompt or folder follows `docs/ui-modal-rename.md`: the name is a
click-to-rename display opening a Cancel/Rename modal (names live in the tree
payload, so a rename is a tree save, not a content save).

## HTTP API

JSON, same-origin, in `webapp/prompt_api.py`. uuids are the identifiers.

- **`GET /prompt/api/tree`** → `{folders, prompts, version}` (no content).
- **`PUT /prompt/api/tree`** — the guarded whole-tree save (above).
- **`GET /prompt/api/prompts/<uuid>`** → one version incl. `content`,
  `parentUuid`/`parentName`/`parentExists` (for the "Based on" line), and
  timestamps. Also used by the /chat Settings sidebar for its read-only
  preview of a linked prompt.
- **`PUT /prompt/api/prompts/<uuid>`** `{content}` — the editor's explicit Save.
- **`POST /prompt/api/prompts/<uuid>/clone`** → the new version's tree row.
- **`GET /prompt/api/prompts/<uuid>/diff?against=`** → unified-diff lines +
  the ancestor list for the picker.

## Frontend

Layout and behavior follow the app-wide conventions: the left-panel tree
(`docs/ui-left-panel-tree.md` — nested lists, guide lines, one selected node,
drag-and-drop with a "Move to top level" strip), modals (`docs/ui-modals.md`),
kebab menus (`docs/ui-kebab-menu.md`), and modal-confirmed rename
(`docs/ui-modal-rename.md`).

- **Left panel:** an "All prompts" node, **+ Folder** / **+ Prompt** actions
  (creation lives here, not in the kebabs), then the tree. Kebab per node:
  folders get Rename / Delete; prompts get Rename / **Clone** / Delete.
  Delete is a confirm modal; a non-empty folder shows the subtree counts and
  requires typing its name (the cascade deletes the prompts inside too).
- **Right panel, folder view:** the selected subtree as a table (name,
  based-on, updated, Open), plus the folder's click-to-rename name and its
  description (edited in a modal).
- **Right panel, prompt view:** the click-to-rename name; a **"Based on"**
  line linking to the parent version (or "(none — this is an original)" /
  "(deleted)"); created/updated dates; the toolbar (**Edit**, **New chat**,
  **Diff against parent**); then the read-only editor or the diff view.
- **Deep-linking:** `?id=<uuid>` selects that folder or prompt on load; the
  selection is mirrored back into the URL.

## How direct chat uses a prompt

A direct room (`Chatroom.room_type = 'direct'`, see `docs/direct-chat.md`)
has two mutually exclusive system-prompt sources on its row:

- **`prompt_uuid`** — a link to a `/prompt` version, or
- **`system_prompt`** — the room's own free text.

### Linking (the /chat Settings sidebar)

The direct room's Settings sidebar shows the prompt source: either "Custom
text" with an editable textarea, or the linked prompt's name (linking to
`/prompt?id=<uuid>`) with the textarea as a **read-only preview** of that
version's current content. "Choose stored prompt…" opens a picker modal that
renders the /prompt folder tree (read-only, fetched from the same tree API);
clicking a prompt links it. **Unlink** returns to the room's own free text —
linking never destroys it, the free text is kept on the row. The PUT to
`/chat/api/rooms/<uuid>/settings` validates that `prompt_uuid` names a real
stored prompt.

The /prompt page's **New chat** toolbar button runs the same flow from the
other side: it creates a direct room named after the prompt, links the version
via the settings PUT, and navigates to `/chat?id=<room>`.

### Resolution — fresh on every turn

When the direct-chat responder handles a turn (`agents/direct_chat.py`), it
calls `db.resolve_room_system_prompt(room)`:

1. If `prompt_uuid` is set, the linked version's `content` is read **fresh
   from the DB at that moment** — editing the version on /prompt applies from
   the room's next reply, with no re-linking step. If the linked version was
   **deleted**, the room sends **no system message** (empty) rather than
   silently reviving the stale free text.
2. Otherwise the room's own `system_prompt` free text is used ('' = none).

`build_messages` then assembles the LLM call: the resolved prompt as the
system message (omitted when blank), followed by the room's `kind='message'`
rows oldest-first (human rows as `user`, everything else as `assistant`).

Two consequences worth knowing:

- **A linked prompt is a live reference, not a snapshot.** All rooms linking
  the same version pick up an edit simultaneously. To freeze what a room uses,
  clone the version and link the clone (which is also what preserves the
  diffable history).
- The full-metadata **chat Export** includes the room's *resolved* system
  prompt, so an exported conversation records what the model was actually
  told at export time.

## Deliberate tradeoffs

- **Lineage by `parent_uuid`, not a revisions table.** Clone-to-edit gives
  history exactly when the operator wants it, with no hidden auto-versions;
  the cost is that editing in place (Edit → Save) genuinely rewrites a
  version. The convention: tweak in place while iterating, clone before
  meaningful changes.
- **Dangling `parent_uuid` is legal.** Deleting an old version must not be
  blocked by its descendants; the UI shows "(deleted)" and the diff endpoint
  reports "no available ancestor".
- **Bulk whole-tree PUT, not per-node PATCH** — same reasoning as /cron: the
  version token + delete tripwire remove the data-loss failure modes, and a
  single-operator app doesn't need concurrent-editor granularity.
- **Content outside the tree payload/version.** Keeps the frequently-saved
  tree light, and means a content save can never 409 an open tree (and vice
  versa).
- **Deleted linked version ⇒ empty system message.** Fail-obvious was chosen
  over fail-soft: the room visibly behaves like it has no prompt, instead of
  quietly using text the operator thought was replaced by the link.

## Open questions

- **Usage back-references.** The Settings sidebar links room → prompt; the
  /prompt page has no "which rooms link this version?" view yet, so deleting
  a version doesn't warn about rooms that would lose their system message.
- **Non-chat consumers.** Only direct chat resolves stored prompts today;
  agents defined in `agent_profiles/` carry their own prompt files.
