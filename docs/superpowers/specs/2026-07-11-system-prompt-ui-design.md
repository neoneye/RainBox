# System prompt manager (`/prompt`) — design

## Problem

Direct chat rooms (`/chat`) each hold a `system_prompt` textarea. Switching
between personas means re-pasting prompt text; there is no library of prompts,
no history, and no way to see how a prompt evolved. This feature adds a
standalone prompt manager at `/prompt`. The `/chat` page is **not** touched in
this iteration — the manager must mature first; wiring rooms to stored prompts
is a later feature.

## Concept: every prompt row is a version

A "system prompt" is an immutable-ish lineage of rows. Each row has its own
uuid and is addressable at `/prompt?id=<uuid>`.

- **New prompt** ("+ Prompt" button): a fresh row, empty content,
  `parent_uuid = null` (a lineage root).
- **Clone** (button in the editor pane / kebab menu): the only way to make a
  new version. Copies name + content into a new row whose `parent_uuid` points
  at the source. The clone lands in the same folder, positioned right after
  its source.
- **Edit in place**: the textarea autosaves (debounced) into the current row.
  History is preserved by cloning *before* significant edits — the parent
  chain is the history, and rolling back = opening (or re-cloning) an
  ancestor.
- **Based-on link**: the editor pane shows "Based on: <parent name>" linking
  to `/prompt?id=<parent uuid>`. Roots show "(none)"; a deleted parent shows
  "(deleted)".
- **Diff**: a 2-way line diff of an ancestor's content → the open prompt's
  content, default against the immediate parent, with a dropdown listing the
  whole ancestor chain. Computed server-side with `difflib.unified_diff`.
- **Delete**: allowed (kebab → type-to-confirm modal like /git). Children of a
  deleted row keep their `parent_uuid` (dangling allowed — the validator does
  not check it); their UI degrades to "(deleted)" and diff-vs-parent becomes
  unavailable.

## Architecture — a port of `/git` (docs/ui-left-panel-tree.md §9)

| piece | file | mirrors |
|---|---|---|
| Page shell + CSS | `webapp/prompt_views.py` | `webapp/git_views.py` |
| Page JS | `static/prompt.js` | `static/git.js` |
| JSON API | `webapp/prompt_api.py` | `webapp/git_api.py` |
| DB layer | `db/prompt.py` | `db/git.py` |
| Models | `PromptFolder`, `Prompt` in `db/models.py` | `GitFolder`, `GitRepo` |

Registered in `webapp/__init__.py` and re-exported from `db/__init__.py`;
"Prompts" nav link added to `NAV_TEMPLATE` in `webapp/core.py` (after Chat).

### Data model

```python
class PromptFolder(db.Model):        # == GitFolder
    __tablename__ = "prompt_folder"
    id, uuid, name, description, parent_uuid, position, created_at, updated_at

class Prompt(db.Model):
    __tablename__ = "prompt"
    id: int PK
    uuid: UUID unique
    name: Text default ""
    content: Text default ""            # the system prompt text
    parent_uuid: UUID | None            # the prompt this was cloned from; no FK
    folder_uuid: UUID | None            # left-panel placement; no FK
    position: int
    created_at / updated_at
```

House style throughout: plain uuid columns, no FKs, app-side validation.
Tables come from `db.create_all()` (fresh columns need no migration hook).

### API

- `GET /prompt/api/tree` → `{folders, prompts, version}` — prompts carry
  `uuid,name,folderId,parentUuid,created_at,updated_at` but **not content**
  (kept light; content loads per-prompt).
- `PUT /prompt/api/tree` — full-replace of structure (the /git shape:
  version-guarded 409, declared-deletes tripwire 400). Never writes `content`:
  new rows start empty, existing rows keep their content.
- `GET /prompt/api/prompts/<uuid>` → `{ok, uuid, name, content, parentUuid,
  parentName, parentExists, created_at, updated_at}`.
- `PUT /prompt/api/prompts/<uuid>` body `{content}` — content autosave
  (last-write-wins; content is excluded from the tree version hash so
  keystrokes never 409 an open tree).
- `POST /prompt/api/prompts/<uuid>/clone` → `{ok, prompt:{…tree fields}}` —
  server-side copy (name, content, folder), `parent_uuid` = source, inserted
  after the source in position order.
- `GET /prompt/api/prompts/<uuid>/diff?against=<ancestor uuid>` →
  `{ok, against:{uuid,name}, ancestors:[{uuid,name,created_at}…], lines:[…]}` —
  `against` defaults to the parent; must be an ancestor of `<uuid>`.
  `lines` is `difflib.unified_diff(ancestor, current)` output (n=3 context).
  Ancestor-chain walk is cycle-safe (seen-set) and caps at 100 hops.

### Frontend (static/prompt.js)

State/tree/drag-drop/modals/save-plumbing are a rename-port of `git.js`
(`prompt*` prefix). Page-specific parts:

- **Editor pane** (leaf selected): rename field (like /git), a muted
  "Based on: <link> · created <date>" line, buttons `Clone` and
  `Diff against parent ▾` (ancestor dropdown), then the textarea —
  monospace, no markdown preview, `flex:1` fill, debounced (600ms) content
  PUT with a small "Saving… / Saved" indicator. Content loads fresh on every
  select (uuid-guarded against stale responses, like git's detail fetch).
- **Diff view**: toggles in place of the textarea; unified-diff lines
  rendered `+` green / `−` red / `@@` blue / context gray, monospace `<pre>`
  rows. "Back to editor" closes it.
- **Folder view / All prompts**: the /git recursive table — Name (indented) /
  Type / Based on / Updated / Open link.
- **Clone flow**: POST clone → re-hydrate tree → select the new uuid.
- Deep-link `?id=<uuid>` (folder or prompt), localStorage-persisted expand
  state, root-drop strip, type-to-confirm folder/prompt delete — all as /git.

### Tests

- `db/test_prompt_tree.py` — validate/save/load/version round-trip (content
  survives a structural save), clone (content copy + parent link + position),
  content update, diff (vs parent, vs grandparent, non-ancestor rejected,
  missing parent).
- `webapp/test_prompt_api.py` — endpoint status codes: tree GET/PUT (409 on
  stale version, 400 on undeclared deletes), prompt GET/PUT, clone, diff.
- `webapp/test_prompt_views.py` — `/prompt` renders, key markers present
  (tree ids, modal ids, script tag), nav contains the link.

Layout/CSS is copied rule-for-rule from `git_views.py` per the hard-won §8
guidance, and drag/selection is verified in a real browser before merge.

## Out of scope (deliberate)

- Any `/chat` change (picking a stored prompt for a room) — later feature.
- Prompt immutability enforcement, tags, search, children ("clones of this")
  listing, 3-way merge.
