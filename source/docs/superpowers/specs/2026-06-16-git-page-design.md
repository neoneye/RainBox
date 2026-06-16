# `/git` page — git repository support

**Date:** 2026-06-16
**Branch:** `git-impl`

## Purpose

Add a `/git` page that mirrors the `/cron` page's left-tree + right-panel
layout, for organizing **git repositories** into **nested folders**. Repos are
added by pointing at an existing repository path on disk (no cloning from
remote). Every folder and repo is deep-linkable via a `?id=<uuid>` URL
parameter, so links survive renames (we never link by name or path).

This is the first cut. It deliberately ships a thin repo view (path, current
branch, root file/dir listing) with room to grow into richer detail later.

## Decisions captured during brainstorming

- **Path validation on add:** require the path to be a valid git repository
  (reject non-repos with an inline error).
- **Repo root listing:** OS-level listing of the top-level entries in the repo's
  working directory, **including `.git` and other dotfiles**, directories first.
- **Kebab actions now:** Add (repo / subfolder) + Rename only. **No Delete**
  (deferred). Rename changes only the RainBox display name, never the directory
  on disk.
- **Drag & drop:** full `/cron` parity — reorder, nest into folders, move to top
  level, with cycle guards.
- **Repo detail now:** filesystem path + current git branch, above the root
  listing.

## Architecture

The page follows the established `/cron` pattern exactly: a server-rendered
Jinja shell, a vanilla-JS client that holds the whole tree in memory, and a
whole-tree bulk GET/PUT API with optimistic-concurrency versioning. Live repo
data (branch, root listing) is fetched per-repo on demand, the same way `/cron`
fetches per-job health separately from the tree.

### Data model — `db/models.py`

Two new tables, created automatically by `db.create_all()` in
`db.init_db` (brand-new tables need no manual migration; only ALTERs to
pre-existing tables do). They mirror `CronFolder`/`CronJob` minus the
firing-specific columns. **No `enabled` column** — repos have no firing concept,
so an enable/disable toggle would be dead UI.

**`GitFolder`** (`__tablename__ = "git_folder"`):

| column        | type                | notes                                   |
|---------------|---------------------|-----------------------------------------|
| `id`          | int PK              | auto-increment                          |
| `uuid`        | UUID unique         | the frontend id (`?id=`)                |
| `name`        | Text                | folder name                             |
| `description` | Text                | notes about child nodes                 |
| `parent_uuid` | UUID \| None        | null = root; plain column, no FK        |
| `position`    | int                 | sort order among siblings               |
| `created_at`  | DateTime(tz)        | auto-set                                |
| `updated_at`  | DateTime(tz)        | auto-update                             |

Index: `git_folder_children` on `(parent_uuid, position)`.

**`GitRepo`** (`__tablename__ = "git_repo"`):

| column        | type                | notes                                            |
|---------------|---------------------|--------------------------------------------------|
| `id`          | int PK              | auto-increment                                   |
| `uuid`        | UUID unique         | the frontend id (`?id=`)                          |
| `name`        | Text                | RainBox display name; freely editable            |
| `folder_uuid` | UUID \| None        | null = unfiled at root; plain column, no FK      |
| `path`        | Text                | absolute filesystem path; set at add, immutable for now |
| `description` | Text                | notes                                            |
| `position`    | int                 | sort order                                       |
| `created_at`  | DateTime(tz)        | auto-set                                          |
| `updated_at`  | DateTime(tz)        | auto-update                                       |

Index: `git_repo_in_folder` on `(folder_uuid, position)`.

### Server — `db/git.py`

Mirrors `db/cron.py`:

- `git_load_tree() -> dict` — returns
  `{"folders": [...], "repos": [...], "version": "<16-char hash>"}`.
  - folder dict: `{id, name, description, parentId, created_at, updated_at}`
  - repo dict: `{uuid, name, folderId, path, description, created_at, updated_at}`
- `git_tree_version() -> str` — sha256-derived opaque token over the persisted
  tree (optimistic concurrency).
- `validate_git_tree(folders, repos) -> None` — app-side validation: no dangling
  `parentId`/`folderId`, no cyclic folder parents (mirrors
  `validate_cron_tree`).
- `git_save_tree(folders, repos, *, base_version=None, expected_deletes=None)` —
  version-guarded (stale `base_version` → conflict) + delete-count-guarded
  (declared deletes must match actual, the truncated-payload tripwire). Array
  order becomes the `position` column. Persists `path`/`name` as opaque strings;
  does **not** touch the filesystem, so a temporarily-unavailable repo never
  blocks a save.
- `check_git_path(path) -> dict` — `{ok, path: <abs>, branch, error}`. Validates
  the path is a git repo via `git -C <path> rev-parse --git-dir` (bounded
  timeout). Returns the absolute/canonical path and current branch on success.
- `repo_detail(uuid) -> dict | None` — live read for the right panel:
  `{path, exists, isRepo, branch, entries: [{name, isDir}]}`. `entries` is an OS
  listing of all top-level entries (including `.git` and dotfiles), directories
  first then files, each sorted by name. Branch via
  `git -C <path> rev-parse --abbrev-ref HEAD`.

All `git`/filesystem subprocess calls use bounded timeouts, consistent with the
recent subprocess-hardening work (`harden-lms-subprocess`).

### API — `webapp/git_api.py`

Mirrors `webapp/cron_api.py`:

| endpoint                        | method | purpose                                            |
|---------------------------------|--------|----------------------------------------------------|
| `/git/api/tree`                 | GET    | hydrate the whole tree                             |
| `/git/api/tree`                 | PUT    | bulk save (version + delete-count guarded); 409 on stale version |
| `/git/api/check-path`           | POST   | `{path}` → validate it is a git repo; returns abs path + branch |
| `/git/api/repos/<uuid>/detail`  | GET    | live `{path, exists, isRepo, branch, entries}`     |

### Page shell — `webapp/git_views.py`

`@app.route("/git")` renders `GIT_TEMPLATE` (inline CSS,
`{% include "_nav.html" %}`), copied/adapted from `cron_views.py`. Split-view
grid: left `#git-tree` sidebar + right `#git-main` panel, plus modal overlays
(add repo, new folder, rename) sharing `ui-modal.css`. A `_git_js_version()`
mtime cache-buster feeds `<script src="/static/git.js?v=...">`.

### Client — `static/git.js`

Adapted from `cron.js` (vanilla JS, no framework). Whole-tree client state,
250ms debounced save, 409 → re-hydrate.

- **Left tree:** nested folders + repos, expand/collapse, folder icon / repo
  icon, depth indentation.
- **Drag & drop:** reorder, nest into folders, move-to-top (root drop zone),
  with before/into/after zones and cycle guards (`/cron` parity).
- **Kebab menus:** Folder → *New repo*, *New subfolder*, *Rename*.
  Repo → *Rename*. No Delete.
- **Click a folder** → right panel: a table of its **direct** contents
  (subfolders + repos) with columns: *name*, *type*, *path* (repos only),
  *description*, Details link.
- **Click a repo** → right panel: header showing filesystem **path** + current
  **branch**, then the root file/dir listing (directories first). Data fetched
  from `/git/api/repos/<uuid>/detail`.
- **Add repo modal:** name + path fields. On confirm, calls `check-path`; on
  failure shows the error inline; on success creates the repo node using the
  returned absolute path.
- **Rename:** right-panel rename field (folders and repos); updates display
  `name` only.
- **Deep link:** `?id=<uuid>` selects the matching folder or repo on load.

### Wiring

- `webapp/__init__.py`: import `git_views` and `git_api` (registers routes).
- `webapp/core.py`: add a "Git" nav link next to "Cron"/"Kanban". Optional light
  Flask-Admin model views for `GitFolder`/`GitRepo` for parity (low priority).

## Testing

Mirror the existing cron test files. Tests run on `rainbox_claude` automatically
via `conftest.py`.

- `db/test_git_tree.py` — load/save round-trip, version conflict (stale
  `base_version` → conflict), delete-count tripwire, `validate_git_tree`
  rejecting dangling/cyclic refs.
- `webapp/test_git_api.py` — tree GET/PUT round-trip; `check-path` against a
  freshly `git init`-ed temp dir (ok) and a non-repo dir (error); `repo/detail`
  returns branch + root entries.
- `webapp/test_git_views.py` — `/git` renders, key HTML/JS markers present.

## Out of scope (future)

Delete; clone-from-remote; editing a repo's path after add; richer repo detail
(commit log, status, diff, per-file view); enable/disable.
