# Profile — design (frontend + backend)

The `/profile` page persists a folder tree of person profiles to Postgres;
each profile's person fields live in a sparse JSONB validated against a
code-side field registry, and 21 read-only locale templates ship in `data/`
and merge virtually into the tree. Desktop-first, same as the other tree
pages.

## The idea

A **profile** is the structured record of a human — the operator, a friend, a
contact: name, locale and format preferences, contact details. The page is a
folder tree (the app-wide left-panel pattern) whose leaf detail pane is a
**form**, generated from one registry (`profile_fields.PROFILE_FIELDS`) that
also drives the server-side validator, so the form and the validation can
never drift apart. Built-in locale archetypes ("Denmark", "Japan", …) show a
correctly-filled example per region; duplicating one is the one-action way to
start a real profile.

Storage is **full fidelity**: the operator records their own data completely
(a full `YYYY-MM-DD` birthday, a full address). There are no privacy floors
in storage and no audience/visibility layers — the only read/write splits are
built-in templates (read-only) and the connector-owned `dynamic` subtree
(never writable through the human-facing PUT).

## Where things live

| Piece | File |
|-------|------|
| Field registry (the schema) | `profile_fields.py` |
| Tables (`ProfileFolder`, `Profile`) | `db/models.py` |
| Tree load/validate/save, data validation, data read/write, duplication | `db/profile.py` (re-exported from the `db` facade) |
| Built-in templates (shipped data, not DB rows) | `data/profile_templates.json` |
| HTTP endpoints | `webapp/profile_api.py` |
| Page shell + CSS + server-rendered form fieldsets | `webapp/profile_views.py` |
| Page logic | `static/profile.js`, served with an mtime `?v=` cache-buster |
| Tests | `db/test_profile_tree.py`, `webapp/test_profile_api.py`, `webapp/test_profile_views.py` |

## Data model

Two tables in the repo's SQLAlchemy-2.0 conventions (`docs/data-model.md`).
Reference columns are **plain UUID columns — no DB foreign keys**; integrity
is enforced in `validate_profile_tree` before any write.

```
profile_folder
  id, uuid, name, description,
  parent_uuid (nullable)          -- null = root-level folder (nesting)
  position (int), created_at, updated_at

profile                           -- one person
  id, uuid, name,                 -- name = the standalone tree label
  folder_uuid (nullable)          -- null = unfiled at root
  data (JSONB)                    -- ALL person fields, sparse
  position (int), created_at, updated_at
```

- **`name` is the tree label, not a person field.** "Simon" or "Germany" —
  deliberately *not* derived from `data["full_name"]`; renaming the node and
  editing the person's name are independent acts.
- **`data` is sparse.** Keys are the registry's field keys; an **absent key
  means unset — never `""`**. The validator canonicalizes: a submitted blank
  is dropped, so the stored object only ever contains real values.
- **`data["dynamic"]` and `data["calibration"]` are server-owned subtrees**
  (`SERVER_OWNED_SUBTREES`). `dynamic` is the connector namespace
  (machine-written observations, rendered read-only as "Last seen");
  `calibration` holds the knowledge-calibration rows (own endpoint below).
  Neither is a registry field: `validate_profile_data` rejects both in the
  flat human-facing PUT, and `profile_update_data` carries the current row's
  subtrees into the incoming snapshot in the same transaction.
- **Every subtree write goes through `profile_mutate_data`.** All three
  subtrees share one JSONB column, so a writer must never read-modify-write
  race a different subtree's writer: the helper selects the profile row
  `FOR UPDATE`, hands the mutator a copy of the current dict, assigns the
  returned dict, and commits. The flat-field PUT and the calibration PUT
  both use it; any future `dynamic` writer must too.

## The field registry

`profile_fields.PROFILE_FIELDS` is the single source of truth: one frozen
`Field` dataclass row per field (`key`, `group`, `kind`, `label`, `hint`,
`choices`, `multiline`, `datalist`). Every field is optional. Three groups,
rendered as one `<fieldset>` each in registry order:

- **Identity** — names (full, native-script, "address them as", internet
  handle), gender (enum), a multiline "About", birthday (date).
- **Locale & formats** — units (`metric|imperial|uk` — `uk` is the hybrid:
  kg and °C but miles on roads; `imperial` is US customary), temperature
  (`celsius|fahrenheit`, derived from units when unset), date/time format,
  number format, first day of week (enums), timezone, primary + secondary
  language and currency (datalist-assisted free text). The `number_format` enum's five values
  double as previews — every choice renders the same `1234567.89` sample
  (seven integer digits are what disambiguate Indian from Western grouping),
  differing only in separators. `first_day_of_week`
  (`monday|sunday|saturday`) feeds the formatting guide's Calendar
  directive; Monday additionally pins ISO 8601 week numbering.
- **Contact & location** — country, city, address, email.

Four kinds — `text`, `enum`, `date`, `email` — the complete set for v1.
Validation (`validate_profile_data`) is **strict on shape, soft on
membership**: unknown keys and non-strings are rejected, enum values must be
in `choices`, dates must be real ISO calendar dates (regex pins the extended
`YYYY-MM-DD` shape, `date.fromisoformat` rejects impossible dates) — but
IANA/BCP-47/ISO-4217 membership is deliberately *not* enforced, so an
uncommon-yet-valid timezone/language/currency is never blocked. `email` is an
input type, not a server check. The client adds **advisory** warnings
(`static/profile.js` `PROFILE_SOFT_CHECKS`) under the datalist-backed fields
when a value is provably invalid — amber text, never blocking a save.

`SUMMARY_KEYS` (`full_name`, `language`, `units`, `time_format`, `country`)
project onto every tree row as the read-only `summary`, sizing the folder
detail table without a per-profile fetch fan-out. The derived `summary` must
never ride a tree save — the validator rejects it.

## Built-in templates

`data/profile_templates.json` ships **21 locale archetypes** (US … Australia)
plus the virtual **Templates** folder — all with fixed uuids so deep links
survive releases. They are **never DB rows**: the file is parsed once per
process (`lru_cache`) and merged into the tree GET after the user's own
content, each row tagged `builtin: true`. A new rainbox release serves new
template content on the next page load — no re-seed logic, no drift between
installs. Example people are deceased notable figures (with fictional
birthdays/cities), per the dead-namesakes convention.

Built-ins are read-only everywhere: the tree validator rejects any save
carrying a built-in uuid, the data PUT refuses them with 400, the form
renders disabled with a "Duplicate to make an editable copy" hint, and the
Templates folder is neither draggable, droppable, renamable, nor a valid home
for user rows. **Duplicate is the only write that touches them** — it mints a
real editable row (fresh uuid, deep-copied data, named after the template) at
the end of the user-owned top level.

## Knowledge calibration

One server-owned subtree per profile, `data["calibration"]["topics"]`: the
operator's self-declared per-topic calibration, edited in its own fieldset
and injected into the assistant prompt as reference data
(`user_profile/calibration.py` renders it; see `assistant-design.md`).

Each row: `topic` (free text, 1–80 chars, display form trimmed with internal
whitespace collapsed), `level` (`expert|intermediate|beginner|none`),
optional `stance` (`prefer|neutral|avoid`), optional `depth`
(`concise|standard|teach`), optional `note` (≤400 chars — nuance like usage
recency belongs here, not in a fourth enum), plus two server-owned fields:
a stable `id` (UUIDv4) and `updated_at` (RFC 3339 UTC, whole seconds, `Z`).
The axes are deliberately orthogonal — expertise, preference, and desired
explanation depth answer different questions.

Validator rules (`db/profile_calibration.py`, error → 400 naming the row):
at most 100 rows; duplicates detected by NFKC-normalized, whitespace-
collapsed, casefolded topic key with both conflicting positions named;
existing row ids round-trip, new rows omit `id`; client-supplied
`updated_at`, unknown ids, and unknown keys are rejected; blank optional
values are removed; an all-blank row is dropped before validation, a row
with content but no topic or level is an error; the canonical subtree
serialized as UTF-8 JSON stays under 64 KiB. `updated_at` advances only when
a row's semantic fields change — reordering restamps nothing. Row order is
priority order (the editor reorders with up/down buttons, not drag-and-drop).

Writes are **last-acknowledged-write-wins within the subtree** — the same
call the flat fields and `/prompt` content already made; a single-operator
preference list does not pay a conflict-dialog tax. Cross-subtree safety is
the row lock above, which is not optional: without it a flat autosave could
write back a stale calibration copy, which is cross-feature data loss, not a
lost keystroke. An empty snapshot removes the subtree (absent reads as no
topics). Duplication copies semantic fields and order but mints fresh ids
and the duplication timestamp — server identity never survives a copy — and
locks a user-owned source row so the copy is a coherent snapshot.

Germany and US ship three fictional fixture rows between them (chosen to
exercise all axes, not inferred from the archetypes' namesakes); the editor
hides built-in row ages since the shipped stamps are schema filler.

## Tree + persistence (the /prompt and /cron pattern)

The left panel follows the app-wide tree conventions
(`docs/ui-left-panel-tree.md`): nested lists with guide lines, one selected
node, drag-and-drop with a drag-only "Move to top level" strip, kebab menus,
and modal-confirmed rename (`docs/ui-modal-rename.md` — the pane heading is
the click-to-rename control, so the kebab has no Rename item; it offers
Duplicate and Delete only). Folder delete cascades with the typed-name gate
for non-empty folders. Expand/collapse state persists in `localStorage`.

Structural changes save as a debounced (250 ms, serialized) **whole-tree
PUT** — an upsert by uuid where list order becomes `position` and rows absent
from the payload are deleted. The same two guards as /prompt and /cron:

- **`version`** — optimistic-concurrency token (sha256 over structural fields
  of *user-owned* rows only; `data` and the derived summary are excluded, so
  a form autosave never invalidates an open page's tree, and the virtual
  built-ins are excluded by construction). Stale → **409** + the current
  token; the page re-hydrates and toasts instead of clobbering. A failed
  initial hydrate leaves the token null, so a PUT of the resulting empty
  state is refused rather than wiping the real tree.
- **`deletes`** — the declared-deletions tripwire; a save that would delete
  more rows than declared is refused with 400.

Validation (`validate_profile_tree`, before any mutation): well-formed uuids,
no duplicate/dangling/cyclic folder references, profile `folderId` must
resolve, no built-in uuids, no submitted `summary`, and a profile uuid must
never collide with a folder id (`/profile?id=<uuid>` must be unambiguous).

Crucially, **the tree save never touches `data`**: new rows start empty,
existing rows keep theirs. Person data flows only through the per-profile PUT.

## HTTP API

JSON, same-origin, in `webapp/profile_api.py`. uuids are the identifiers.

| Endpoint | Semantics | Guards |
|----------|-----------|--------|
| `GET /profile/api/tree` | `{folders, profiles, version}` — user rows + merged built-ins, each profile with its `summary`, no `data` | — |
| `PUT /profile/api/tree` | guarded whole-tree save (structural only) | `version` (409), `deletes` (400), `validate_profile_tree` (400) |
| `GET /profile/api/profiles/<uuid>` | one profile's editable fields + `dynamic` projection (built-ins served from the shipped file); the `calibration` subtree is **projected out** — it has its own endpoint | 404 unknown |
| `PUT /profile/api/profiles/<uuid>` | `{data}` — the form's autosave: a **complete editable snapshot**, canonicalized + validated; answers the fresh `summary` | built-in → 400, `validate_profile_data` → 400 (`calibration` rejected as read-only), `dynamic` + `calibration` preserved server-side |
| `GET /profile/api/profiles/<uuid>/calibration` | `{ok, builtin, topics}` — the canonical calibration rows | 404 unknown |
| `PUT /profile/api/profiles/<uuid>/calibration` | `{topics}` — a complete snapshot; answers the canonical rows (the client needs server-assigned ids/stamps before its next edit) | built-in → 400, validator → 400, 404 unknown |
| `POST /profile/api/profiles/<uuid>/duplicate` | copy the whole data blob into a new row — "<name> copy" right after a user-owned source, a top-level editable row for a built-in; calibration rows get fresh ids + the duplication stamp | 404 unknown |

## The save flow

Two independent channels, mirroring the storage split:

- **Tree** (structure, names, descriptions): every mutation funnels into the
  debounced whole-tree PUT above; the browser projects its state back to
  structural keys only (built-ins and summaries stripped).
- **Data** (the form): autosave, debounced **400 ms per profile**, one
  in-flight PUT per profile, a queued re-send always carrying the newest
  snapshot. The PUT is **last write wins** — no version token; the payload is
  a complete snapshot, so editable keys omitted from it are deleted, not
  retained. Failures retain the dirty snapshot and retry with capped
  exponential backoff (1 s → 30 s) for as long as the page is open; a
  `beforeunload` guard warns while anything is pending; an `online` listener
  retries immediately. Status renders inline: `Saving…` / `Saved ✓` /
  `Save failed — retrying`.
- **Calibration** (its own fieldset): the same autosave pattern with its own
  per-profile state map and status line. Response handling is by class:
  network error or 5xx retains the draft and retries with capped backoff; a
  400 shows the server validation message and waits for the next edit (an
  unchanged invalid snapshot is never retried forever); success adopts the
  canonical response (server-assigned ids and stamps) unless a newer local
  edit is queued, in which case that edit resends immediately. Adopting the
  canonical snapshot never steals focus mid-edit; topicless drafts stay
  local until a topic is typed. Pending and failed-validation states
  participate in the `beforeunload` guard, and late GET/PUT results are
  keyed by uuid so they never populate the wrong pane.

**Duplicate** flushes both channels first (the pending tree save, then the
source's pending data PUT) so the copy always includes the latest edit; either
flush failing aborts with a toast. No version lineage is recorded —
duplication is a convenience, not ancestry (unlike /prompt's clone).

## Frontend details

- **Folder view** (right pane): the selected subtree as a depth-indented
  table — Name / Person / Language / Time / Country / Open — folders marked
  by the tree's folder icon; plus the folder's click-to-rename heading and a
  modal-edited description.
- **Form pane**: fieldsets generated **server-side** from the registry
  (`webapp/profile_views.py` `_form_fields_html`), inputs keyed by
  `data-key`; enum selects get a leading blank option (blank = unset).
  Datalists assist timezone (from `Intl.supportedValuesOf('timeZone')` — no
  list to maintain), language, currency, and country (small static arrays).
  A **"Use my timezone"** button fills the browser's zone; a live datetime
  preview documents the format enums; the read-only **Last seen** fieldset
  renders `data["dynamic"]` when present.
- **Deep-linking:** `?id=<uuid>` selects that folder or profile on load; the
  selection is mirrored back into the URL via `history.replaceState`. Tree
  rows are real anchors, so CMD/Ctrl-click opens a node in a new tab.
- **Flask-Admin:** both tables under an **Admin → Profile** category
  (`ProfileFolderView`, `ProfileView` in `webapp/core.py`), with an
  `inspect ↗` column deep-linking to `/profile?id=<uuid>`.

## Deliberate tradeoffs

- **One sparse JSONB, not columns.** The registry is the schema; adding a
  field is one dataclass row, no migration. The validator keeps the blob
  honest (unknown keys rejected, blanks dropped).
- **Strict shape, soft membership.** Enums and dates are checked hard; open
  vocabularies (IANA, BCP-47, ISO 4217) are advisory-only warnings — a valid
  but uncommon value must never be blocked.
- **Data PUT is last-write-wins.** A single-operator form autosaving complete
  snapshots doesn't need a per-profile version token; the one real hazard —
  clobbering connector writes — is closed by the server-side `dynamic` merge.
- **Templates in a shipped file, not seeded rows.** Read-only by
  construction, updated by release, zero re-seed/drift logic; the price is
  the virtual-merge and the built-in-uuid checks on every save path.
- **Full-fidelity storage, no privacy layers.** The operator's own data is
  stored completely; audience/visibility controls would be a presentation
  concern layered on later, not a reason to store less.

## Prompt rendering

The profile selected by `profile.current` feeds three assistant prompt
blocks, all rendered from one per-turn context snapshot (see
`assistant-design.md`): the identity JSON (`user_profile/identity.py`), the
deterministic formatting guide (`user_profile/formatting.py` — lookup-driven
directives with examples compiled from the locale fields, strict
prompt-boundary validation so free-text values can never become
instructions), and the knowledge-calibration block
(`user_profile/calibration.py` — JSONL rows under a shared guidance budget).
Switching `profile.current` changes identity, formatting, and calibration;
it is **not an audience boundary** — handing the screen to another audience
uses a fresh room and the demo database.

## Open questions

- **`dynamic` writers.** The namespace is reserved and defended, but no
  connector writes it yet; the "Last seen" group only renders for rows given
  such data by hand or by future connectors.
- **Profile descriptions.** Folders have a description field; profiles do
  not — the multiline `about` field covers the person, but there is no
  operator-facing note *about the record itself*.
