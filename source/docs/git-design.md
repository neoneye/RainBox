# Git — design (frontend + backend)

The `/git` page persists a folder tree of **registered local repositories**
to Postgres; selecting a repo shows a live, read-only snapshot (current
branch + root listing) read straight off the filesystem. Desktop-first, same
as the other tree pages.

## The idea

A repo node is a **pointer to an existing repository on disk** — added by
typing its path, never by cloning. The page organizes those pointers in the
app-wide folder tree and answers "what repos do I have, where, and what branch
is each on" without ever writing to any repository. Everything git-facing is
read-only by construction: the backend shells out to exactly two `git`
subcommands, both `rev-parse` reads.

## Where things live

| Piece | File |
|-------|------|
| Tables (`GitFolder`, `GitRepo`) | `db/models.py` |
| Tree load/validate/save, path check, repo detail | `db/git.py` (re-exported from the `db` facade) |
| HTTP endpoints | `webapp/git_api.py` |
| Page shell + CSS | `webapp/git_views.py` |
| Page logic | `static/git.js`, served with an mtime `?v=` cache-buster |
| Tests | `db/test_git_tree.py`, `webapp/test_git_api.py`, `webapp/test_git_views.py` |

## Data model

Two tables in the repo's SQLAlchemy-2.0 conventions (`docs/data-model.md`).
Reference columns are **plain UUID columns — no DB foreign keys**; integrity
is enforced in `validate_git_tree` before any write.

```
git_folder
  id, uuid, name, description,
  parent_uuid (nullable)          -- null = root-level folder (nesting)
  position (int), created_at, updated_at
  Index git_folder_children (parent_uuid, position)

git_repo                          -- a POINTER to a repo on disk
  id, uuid, name,                 -- display name only; freely editable
  folder_uuid (nullable)          -- null = unfiled at root
  path (text)                     -- absolute filesystem path; set at add, immutable for now
  description,
  position (int), created_at, updated_at
```

Nothing about the repository's *contents* is stored — branch, listing, and
existence are read live on every detail request, so the page never shows a
stale cached snapshot.

## Tree persistence (the shared pattern)

The left panel is the app-wide left-panel tree — see
`docs/ui-left-panel-tree.md` for the mechanics (flat arrays + parent
pointers, recursive render, drag-and-drop with a "Move to top level" strip,
real-anchor rows so CMD/Ctrl-click opens a new tab, kebab menus). `/git` is
the full-replace variant: the page hydrates from `GET /git/api/tree` and
saves every structural edit as a debounced (250 ms, serialized) **whole-tree
PUT** — an upsert by uuid where list order becomes `position` and rows absent
from the payload are deleted. The two standard guards apply:

- **`version`** — an optimistic-concurrency token (`git_tree_version`: sha256
  over the persisted structural fields, timestamps excluded). Missing → 400;
  stale → **409** + the current token, before any mutation; the page then
  re-hydrates and toasts instead of clobbering another writer. A failed
  initial hydrate leaves the page's token null, so a PUT of the resulting
  empty state is refused rather than wiping the real tree.
- **`deletes`** — the page declares how many deletions it knowingly performed
  (a repo, or a folder-cascade's subtree count); a save that would delete more
  rows than declared raises `GitTreeError` (400), the truncated-payload
  tripwire.

Validation (`validate_git_tree`, raises `GitTreeError` before any DB write):
well-formed uuids; no duplicate folder ids or repo uuids; folder `name`/
`description` and repo `name`/`path`/`description` must be strings; folder
parents and repo `folderId`s must resolve within the payload; **acyclic**
folder nesting; and a repo uuid must never collide with a folder id — a node
is identified globally by uuid (`/git?id=<uuid>`), so a collision would make
the deep link ambiguous.

## HTTP API

JSON, same-origin, in `webapp/git_api.py`. uuids are the identifiers.

| Method + path | Semantics | Guards |
|---|---|---|
| `GET /git/api/tree` | `{folders, repos, version}` in the frontend's field names (folder `id`/`parentId`, repo `uuid`/`folderId`/`path`), ordered by `position` then `id`; rows carry read-only `created_at`/`updated_at` | — |
| `PUT /git/api/tree` | the guarded whole-tree save (above); returns the new `version` | `version` token (400 missing / 409 stale), `deletes` tripwire, `validate_git_tree` → 400 |
| `POST /git/api/check-path` | validate a typed path is an existing git repo *before* the Add-repo flow creates a node → `{ok, path, branch}` or `{ok: false, error}` | always 200 (the `ok` flag carries validity) unless the body isn't a JSON object |
| `GET /git/api/repos/<uuid>/detail` | live snapshot for the repo pane → `{ok, path, exists, isRepo, branch, entries}` | 400 malformed uuid, 404 unknown repo |

## Repo inspection — what is read, and how

`git_repo_detail` reads, per request: whether `path` still exists on disk;
`git rev-parse --is-inside-work-tree` (is it still a repo); `git rev-parse
--abbrev-ref HEAD` (current branch; empty when detached or unborn); and the
**root directory listing** via `os.listdir` — all entries including `.git`
and dotfiles, directories first then files, each group name-sorted
case-insensitively. That is the whole inspection surface today: no status, no
log, no branch list, no file contents.

**Subprocess safety.** Every git call goes through `_git()`: a fixed argv
`["git", "-C", path, …]` — no shell, so the path is never interpreted — with
`capture_output` and a **5-second timeout** (`_GIT_TIMEOUT`), so a hung or
slow repo (e.g. a dead network mount) can't wedge a web request. Filesystem
and subprocess errors never raise out of `git_repo_detail`; they surface as
`exists`/`isRepo` flags so the UI can message ("The path no longer exists on
disk." / "This path is no longer a git repository.") instead of a 500.

**Path validation** (`git_check_path`, the Add-repo gate): the typed path is
`~`-expanded and `os.path.realpath`-resolved (symlinks collapse to the real
location); it must be an existing directory, and `rev-parse
--is-inside-work-tree` must print `true`. The **resolved absolute path** is
what the ok response returns and the frontend stores — the DB never holds a
relative or symlinked path.

## Deliberately NOT there

- **No mutations.** Neither endpoint nor helper runs any state-changing git
  command — the only subcommands in `db/git.py` are the two `rev-parse`
  reads. No clone, fetch, commit, checkout, or file writes.
- **Deleting a node removes only the RainBox row** — the delete modals say so
  explicitly; the repository on disk is untouched.
- **`path` is immutable after add.** Rename edits the display name only
  (`docs/ui-modal-rename.md`); moving a repo on disk means delete + re-add.

## Frontend

`static/git.js` follows the shared tree conventions
(`docs/ui-left-panel-tree.md`); differences from the /cron reference:

- **No enable/disable layer** — repos have no on/off state, so no dimming or
  effective-enabled logic; repo leaves render **without an icon** (every leaf
  is a repo, the icon would be noise).
- **Kebab is minimal**: Rename and Delete only. Creation lives in the tree's
  **+ Folder** / **+ Repo** buttons; there is no Duplicate or Copy-id.
- **Add repository modal**: Path + optional Name (auto-filled from the path's
  last component until the user edits it); **Add** first POSTs `check-path`
  and shows the server's error inline — a node is only created for a verified
  repo, filed into the currently selected folder.
- **Right pane, folder view**: the selected subtree (or the whole tree at
  "All repositories") as a depth-indented table — Name, Type (Folder/Repo),
  Path, Description, Open — plus the click-to-rename heading and the
  description (edited in a modal, on folders and repos alike).
- **Right pane, repo view**: Path and Branch header, then the root listing.
  The fetch is uuid-guarded — a stale response for a previously selected repo
  is dropped.
- **Deep-linking**: `?id=<uuid>` selects that folder or repo on load; the
  selection is mirrored back into the URL via `history.replaceState`; unknown
  id falls back to *All repositories*.
- Modals follow `docs/ui-modals.md` (shared backdrop, dirty-guarded Esc /
  backdrop dismissal, typed-name gate on non-empty-folder delete); toasts
  replace native alerts.

## Open questions

- **Inspection depth.** Branch + root listing is thin; status (dirty/clean),
  recent log, and a branch list are the obvious next reads — all fit the same
  bounded read-only `_git()` shape.
- **Work-tree subdirectories.** `--is-inside-work-tree` accepts any directory
  *inside* a repo, not just its root, so a subdirectory can be registered as
  a "repo"; `rev-parse --show-toplevel` would normalize this. Duplicate
  registrations of the same path are likewise not prevented.
- **Immutable `path`.** Fine until a repo moves on disk; an "edit path"
  (re-validated via `check-path`) would beat delete + re-add.
