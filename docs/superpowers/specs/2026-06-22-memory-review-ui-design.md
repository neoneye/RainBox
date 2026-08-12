# Memory Review UI — design

**Date:** 2026-06-22
**Status:** approved, ready for implementation
**Related:** `docs/memory-architecture.md` (§8 "Build A Memory Review UI"),
`docs/memory-commands.md`, `docs/ui-left-panel-tree.md`, `docs/ui-modals.md`,
the `/cron` page (the mature reference for chrome).

## Goal

A dedicated `/memory` page to **inspect** the memory store (which claims are
active, which are candidates, which were superseded/rejected/expired) and run
**provenance-safe lifecycle actions** on them. This realizes the "memory review
UI" the architecture doc has been asking for, and gives the operator a place to
see *what the assistant believes and why* — and correct it — without dropping to
Flask-Admin.

This is a memory **inspector / belief editor**, comparable to Letta/MemGPT's
memory-block editor and mem0's memory list, but richer because Rainbox models
**provenance + lifecycle** explicitly. The differentiator to lean into is
showing *why* a memory exists (evidence) and *what happened to it* (lifecycle +
supersession lineage), which the flat "manage memory" lists elsewhere lack.

## Two decisions that shaped this design

1. **Left panel = facet groups, not drag-drop folders.** The live data is small
   and faceted by intrinsic axes (status, scope, sensitivity), with no
   user-created folders and no meaningful ordering. So we mirror `/cron`'s
   *visual chrome* but not its folder data model.
2. **Edit = lifecycle actions + correct, never raw text mutation.** The whole
   architecture rests on "user confirmation does not erase earlier evidence." A
   text change therefore goes through **correct = supersede** (old kept as
   history, new active), preserving lineage and evidence.

## Future direction: an optional folder tree

The operator may later add a **user-created folder tree** in the left panel
(the full `ui-left-panel-tree.md` pattern: folders, drag-drop placement,
positions). For now the left panel groups by status facets only. The design is
kept **forward-compatible** so the tree can be added later without a rewrite:

- The render layer is **grouping-agnostic**: it renders "group node → leaf
  claims," and the grouping function (currently "group by status") is a single
  swappable seam. A future folder tree becomes an *additional* grouping mode
  (e.g. a toggle: "Group by: Status | Folder"), not a replacement.
- Folders, if added, are an **additive layer**: a `memory_folder` table plus a
  nullable `folder_uuid` on the claim (null = unfiled), exactly like
  `/cron`'s folders/`folder_uuid`. The facet view ignores it; a folder view
  reads it. No facet code needs to change.
- The detail pane and all lifecycle actions are independent of grouping, so they
  carry over unchanged.

We are **not** building the folder tree now (YAGNI); we are only not painting
ourselves into a corner that would block it.

## Architecture — mirror `/cron`'s two-file split

| Concern | File | Mirrors |
|---|---|---|
| Route + markup + page CSS | `webapp/memory_views.py` | `webapp/cron_views.py` |
| Behavior (render / select / filter / modals) | `static/memory.js` | `static/cron.js` |
| REST API | `webapp/memory_api.py` | `webapp/cron_api.py` |
| DB helpers | `db/memory.py` (extend) | `db/cron.py` |
| Nav link "Memory" | `webapp/core.py` | the existing plain links |
| Modal CSS | `static/ui-modal.css` (shared, already exists) | per `ui-modals.md` |

**Divergence from `ui-left-panel-tree.md` (intentional):** no folders, no
`position`, no drag-drop, no version-guarded *whole-tree* PUT. We keep the
chrome — sidebar list, static "All memories" root node, select-first /
toggle-expand, kebab-only-on-selected, detail pane, single `?id=<uuid>`
deep-link, the `/cron` layout CSS (block-flow tree panel, `#fbfbfb` panel,
`.sel` tint **and** bold on group and leaf, kebab via `visibility`, `box-shadow`
triple-dot kebab, `.tree-sep` separators, `pointer-events:none` on node
children) — but the left panel is **facet groups by status**, computed
read-only.

## Left panel — facets

- **Top-level groups = status**, fixed order with counts:
  Active · Candidate · Superseded · Rejected · Expired. Each expands to its
  claims; a claim leaf shows its text (masked if `secret`).
- **Static "All memories" root pseudo-node** (like `/cron`'s "All jobs"): a
  static markup element, not the first rendered row; selecting it (no `?id=`)
  shows a flat table of every claim in the right pane.
- **Filter bar** above the tree: free-text (matches text / subject / object) +
  scope, kind, sensitivity dropdowns. Filtering hides non-matching leaves and
  collapses-to-empty groups; counts reflect the filtered set.
- An **active** claim whose `expires_at` is in the past gets a **"stale"** badge
  (its DB status stays `active`; retrieval already treats it as expired). It
  stays in the Active group — we do not synthesize a separate group for it.

## Selection model (from `/cron`)

- Click a group header: select-first (shows its claim list / table), second
  click toggles expand.
- Click a claim leaf: opens the detail pane; `?id=<uuid>` deep-link (groups and
  the "All memories" node have no uuid → no param).
- `.sel` highlight is tint **and** bold, on group nodes **and** leaf rows.
- Kebab rendered on every row at `visibility:hidden`, shown only on `.sel`.
- Folder-vs-item mutual exclusivity → here, group-vs-claim mutual exclusivity.

## Detail pane (right)

1. **Header** — full text (Reveal button when `secret`); badges: status, scope,
   kind, sensitivity, confidence; room name when room-scoped.
2. **Timestamps** — created · updated · expires (with stale warning when past).
3. **Lineage** — "supersedes →" and "superseded by →" links to the related
   claim (select it on click).
4. **Evidence timeline** — read-only rows: provenance · source_type ·
   source_id · excerpt · created_at, newest first.
5. **Retrieval** — "used in last answer?" plus recent `RetrievalEvent` stages
   (retrieved / used / downvoted) for this uuid; best-effort from existing
   telemetry. Absent telemetry → "no retrieval recorded."
6. **Embedding** — fresh / stale / absent + model name (ties to `memory_sync`).
7. **Actions** (contextual by status), all provenance-safe:
   - `candidate` → **Activate**, **Correct…**, **Reject**, Sensitivity, Expiry
   - `active` → **Correct…**, **Forget** (reject), Sensitivity, Expiry
   - `rejected` / `expired` → **Reactivate**
   - `superseded` → read-only (link to successor)

## API (`/memory/api/...`)

- `GET /claims` → list, each claim with derived fields: uuid, text (masked when
  `secret`), `secret` flag, status, scope, kind, sensitivity, confidence,
  room_uuid + room name, created_at, updated_at, expires_at, `stale` (active &
  past expiry), evidence_count, embedding_state (`fresh`|`stale`|`absent`),
  supersedes_uuid, superseded_by_uuid, used_recently.
- `GET /claims/<uuid>` → full detail: the above + evidence rows, lineage claims
  (short), embedding detail, recent retrieval events.
- `POST /claims/<uuid>/activate` — candidate/expired → active (re-embed).
- `POST /claims/<uuid>/reject` — → rejected (prune embedding).
- `POST /claims/<uuid>/reactivate` — rejected/expired → active (re-embed).
- `POST /claims/<uuid>/correct` — body `{new_text}` → `supersede_memory` (old →
  superseded, new active with `confirmed_by_user` evidence).
- `POST /claims/<uuid>/sensitivity` — body `{sensitivity}` → field update.
- `POST /claims/<uuid>/expiry` — body `{expires_at | null}` → field update.

**Per-row optimistic concurrency:** every mutating POST carries
`expected_updated_at`. The server compares it to the claim's current
`updated_at`; on mismatch it returns **409** and the client re-hydrates and
shows a "changed elsewhere — reloaded" toast. This is the `/cron` version-guard
discipline scaled from a whole tree to a single row (no tree → no tree version).

**Secret masking** is server-side: `GET /claims` returns `text` masked (e.g.
`"•••••• (secret)"`) with `secret:true`; the unmasked text is returned only by
`GET /claims/<uuid>` and revealed client-side on demand. Keeps the list view
shoulder-surf-safe.

## New DB helpers (`db/memory.py`)

- `set_memory_sensitivity(memory_uuid, sensitivity, *, expected_updated_at)`
- `set_memory_expiry(memory_uuid, expires_at, *, expected_updated_at)`
- `reactivate_memory_claim(memory_uuid, *, confirmed_by_uuid)` — rejected/expired
  → active, re-embed (a thin DB-level sibling of the assistant's internal
  `reactivate_memory` inverse).
- `memory_claim_detail(memory_uuid)` — assemble claim + evidence + lineage +
  embedding state for the detail endpoint.
- A small `claim_stale(claim)` predicate (active & `expires_at` past).

Reuse existing `activate_memory_claim`, `reject_memory`, `supersede_memory`,
`list_memory_claims`, `get_memory_embedding`, and `memory.embeddings`
refresh/prune.

The `expected_updated_at` guard lives in the helpers (raise a typed
`StaleWriteError`) so both the API and any future caller get it; the API maps it
to 409.

## Error handling

- Stale `expected_updated_at` → `StaleWriteError` → 409 + client re-hydrate.
- Invalid enum value (sensitivity not in public/private/secret; bad ISO expiry)
  → 400 with a message surfaced in the modal.
- Action not valid for current status (e.g. activate an already-active claim) →
  400 with a clear message; the UI only offers status-appropriate actions, so
  this is a backstop.
- Missing claim → 404.

## Testing (model-free, pinned to `rainbox_claude` by conftest)

- `db/test_memory_ui_helpers.py` — `set_memory_sensitivity`,
  `set_memory_expiry`, `reactivate_memory_claim`, `memory_claim_detail`,
  `claim_stale`, and the `StaleWriteError` guard (each mutate refuses a stale
  `expected_updated_at`).
- `webapp/test_memory_api.py` — list shape + secret masking; detail includes
  evidence + lineage; activate/reject/reactivate/correct/sensitivity/expiry
  happy paths; 409 on stale guard; 400 on bad enum; 404 on missing.
- `webapp/test_memory_views.py` — markup assertions (nav link, sidebar facet
  scaffold, "All memories" node, detail-pane element ids, modal markup), in the
  style of `webapp/test_cron_views.py`.
- One **live-browser** pass (headless Chrome + DevTools Protocol, no extra deps)
  for selection → detail render → one lifecycle action → 409 recovery. Lighter
  than `/cron`'s because there is no drag-drop, but the `ui-left-panel-tree.md`
  "verify in a real browser, never assert it mirrors /cron" lesson still holds
  for selection/detail/kebab/modals.

## Out of scope (v1, YAGNI)

- Creating brand-new memories from the page (that is `remember`, via chat / the
  assistant write surface).
- The user-created folder tree (future direction above).
- Candidate auto-extraction, conflict detection, bulk operations, raw field
  editing of text/status/scope/confidence.

## Implementation sequence

1. DB helpers + `StaleWriteError` + tests (`db/test_memory_ui_helpers.py`).
2. `webapp/memory_api.py` + tests (`webapp/test_memory_api.py`).
3. `webapp/memory_views.py` (markup + CSS copied rule-for-rule from `/cron`) +
   nav link in `webapp/core.py` + markup tests.
4. `static/memory.js` (hydrate, grouping-agnostic render, facet grouping,
   selection, filter, detail, modals, secret reveal, 409 re-hydrate).
5. Live-browser verification pass; fix CSS/layout divergences against `/cron`.
6. Update `docs/memory-architecture.md` §8 / `docs/memory-commands.md` operator
   notes to point at `/memory` (current-state docs, not change history).
