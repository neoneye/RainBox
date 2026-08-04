# Tree persistence — the standard

How a left-panel tree page (`docs/ui-left-panel-tree.md`) saves its folders and
items. This doc is the authority on the **save shape**; the tree doc covers
everything else (rendering, selection, drag-drop, modals).

## The rule

> **A tree save never creates and never deletes a row. It only updates rows
> that already exist. Creation and deletion are dedicated endpoints.**

Absence from a payload means nothing. It is not an instruction, not a
deletion, not a create — it is a malformed request.

## Endpoint set

Five endpoints per page. `<page>` is the route prefix, `<item>` the plural leaf
noun (`repos`, `prompts`, `personalities`, …).

| Method + path | Does |
|---|---|
| `GET /<page>/api/tree` | Hydrate: `{folders, <item>s, version}` |
| `PUT /<page>/api/tree` | Update placement, order and name of existing rows |
| `POST /<page>/api/folders` | Create one folder |
| `POST /<page>/api/<item>s` | Create one item |
| `DELETE /<page>/api/folders/<uuid>` | Delete one folder (cascades its subtree) |
| `DELETE /<page>/api/<item>s/<uuid>` | Delete one item (cascades what it owns) |

Item *content* — prompt text, profile `data`, personality text — stays out of
all of these and saves through its own per-item `PUT`, so a content save can
never collide with an open tree (and vice versa).

### What the PUT may do

Only these fields, only on rows already in the database: `name`,
`description`, `parent_uuid` / `folder_uuid`, and `position` (from list order).

### What the PUT must reject — 400, before any write

- A folder or item uuid that **does not exist** (it would be a create).
- An existing folder or item uuid **missing from the payload** (it would be a
  delete). The payload lists exactly the rows the server holds.
- Dangling or cyclic folder parents; an item whose `folderId` does not resolve.
- A uuid used by both a folder and an item — a node is addressed globally as
  `?id=<uuid>`, so a collision makes the deep link ambiguous.

Because the PUT cannot delete, there is **no `deletes` counter**. That
tripwire only exists to make delete-by-omission survivable; a shape without
delete-by-omission does not need it.

## Version token

`<page>_tree_version()` is a hash over **structural fields only** — uuid,
name, description, parent, position. Excluded: item content, and every
volatile field a background process writes (`last_message_id`, `next_run_at`,
`updated_at`), or a scheduler tick would 409 the next save.

- The client sends the token it hydrated with. Stale → **409** plus the
  current token; the client re-hydrates and toasts "tree changed elsewhere —
  reloaded".
- **Every mutating endpoint returns the new `version`** — the two POSTs and
  the two DELETEs included, not just the PUT. A create that does not hand back
  a fresh token leaves the client holding a stale one, and its next drag 409s
  for no reason the operator can see.

## Client rules

These rules govern the tree save (placement, order, name). The per-item
content save is a separate request against a separate endpoint and follows
its own rule, below.

- **Debounce and serialize** the PUT (~250ms), one request in flight; a save
  requested mid-flight is queued and re-sent after.
- **Create and delete are immediate, never debounced** — they are explicit
  operator acts, and their response carries the token the next PUT needs.
- **Flush or await a pending tree PUT before issuing a create or delete.**
  Nothing else orders them against each other, and the two responses race:
  if the older PUT's response lands after the create/delete's fresher token,
  it overwrites that token with a stale one, and the next save 409s for a
  reason the operator can't see.
- **Re-hydrate on any tree-save failure**, 409 or network error alike, so the
  client converges on server truth instead of drifting.
- Delete is confirmed in a modal (`docs/ui-modals.md`); a non-empty folder
  shows its subtree counts and requires typing its name.
- **Surface orphaned rows instead of hiding them.** A row whose parent no
  longer resolves (e.g. its folder was deleted out from under it by another
  path, such as the admin) still exists and is still included in every tree
  save, so validation still rejects it — see below — until it's fixed. If the
  client's normal folder listing simply omits it, the operator can never see
  or reach it to move or delete it, and every tree save 400s forever with no
  visible cause. Render it at root level instead, alongside normally-placed
  rows, so the operator can repair it.
- **A per-item content save must NOT re-hydrate on failure.** Unlike the tree
  save, a failed content save (validation error, network error, conflict)
  keeps the operator in edit mode with their unsaved text intact — re-hydrating
  there would fetch server state over the top of it and discard what they were
  writing. Report the failure and let them retry or cancel.

## Validation

Structural checks run in the DB layer before any mutation
(`validate_<page>_tree`), raising a page-specific error the API maps to 400.
Parent references are plain UUID columns with **no FK constraints**, so this
validator is the only thing standing between a bad payload and a corrupt tree.

Validation stays strict — an orphaned row (dangling `folderId`/`parentId`) is
still a 400, same as any other structural problem. The fix for the operator
being stuck behind that 400 lives on the client (surfacing the row so it can
be repaired, per Client rules above), not by loosening what the server
accepts.

## Why

Delete-by-omission makes a routine frontend bug indistinguishable from a
deliberate act. A truncated array, a filter that dropped a row, a render race —
each arrives as a well-formed request whose meaning is "delete these". The
damage lands in the database before anyone sees a red flag.

The guards that make it survivable (a version token plus a declared-deletes
counter) are load-bearing precisely because the failure mode is severe. The
version token earns its keep regardless — it is concurrency control, not
delete protection. The counter does not: it exists only to catch a payload
that lies about its own intent, and a shape that cannot express deletion has
nothing to lie about.

The cost of the split is four small endpoints. What it buys is a class of
data-loss bug that cannot be written.

This matters most for rows that **own** other rows: deleting a personality
takes its whole revision history, deleting a chat room takes its messages.
Those deletions must be something an operator asked for, never something a
payload implied.

## Where the pages stand

| Page | Tree save shape |
|---|---|
| `/chat` | Placement-only for rooms; folders still upsert-and-delete through the PUT |
| `/kanban` | Placement-only for boards and folders — closest to this standard |
| `/personality` | This standard |
| `/cron`, `/git`, `/prompt`, `/profile` | Full replace: rows absent from the payload are deleted, guarded by a `deletes` counter |
| `/kanban` board contents (columns + tasks) | Full replace with a `deletes` counter — not a tree, same delete-by-omission risk |

A page not yet on the standard converts the next time its persistence layer is
touched for another reason. Converting one is mechanical: add the two POSTs
and two DELETEs, strip `expected_deletes`, and make the save function raise on
a missing or unknown uuid instead of inserting or deleting.

## Checklist for a new page

1. `GET/PUT /<page>/api/tree`, `POST` + `DELETE` for folders and for items.
2. `<page>_save_tree` raises on any uuid that is missing from, or unknown to,
   the payload. No `expected_deletes` parameter.
3. `<page>_tree_version` hashes structural fields only.
4. Every tree-structure endpoint — the PUT and both POSTs and DELETEs —
   returns `{"ok": true, "version": …}`. Per-item content endpoints stay
   outside the tree save (see Client rules) and deliberately carry no token.
5. Deletes cascade in the DB layer and are confirmed in a modal.
6. Tests: a PUT omitting an existing row is 400 and mutates nothing; a PUT
   naming an unknown row is 400; a stale token is 409; delete cascades;
   create and delete return a token the next PUT accepts.
