# User profile page (`/profile`)

**Status: proposal.** A new left-panel-tree page where each leaf is a **person
profile** — the structured record of a human (name, locale, formats, contact),
editable through a form pane. The immediate use is demoing rainbox to friends:
the operator creates a profile per friend in seconds, and a seeded `Example/`
folder ships five locale archetypes (European, US, Canadian, Chinese,
Australian) that double as documentation of what a filled-in profile looks
like. The longer arc is multi-user preparation: this table is the person
record that real accounts will eventually hang off.

## Relationship to the two existing "profile" concepts

The word is already taken twice; this proposal is a third, distinct thing, and
the three converge rather than collide:

| Concept | What it is | Where |
|---|---|---|
| `user_profile/` package | memory-derived prompt block ("about the operator") | `2026-06-20-phase3-user-profile.md` |
| Operator profiles (lens) | named visibility preset: audiences + shields + ceiling | `2026-07-07-operator-profiles-and-working-context.md` |
| **Person profile (this)** | **editable structured record of a human** | `/profile` page, `profile` table |

Convergence path (all future, none required now): the lens JSON gains a
`profile_uuid` pointing at a person profile, so "who is at the keyboard"
(this record) and "what may they see" (the lens) become one switch; the
working-context block's line 1 quotes the active person profile instead of a
Q&A card; and the assistant's output formatting (units, timezone, date/time
style) reads from the active profile instead of being implicit. This page
deliberately ships **none** of that wiring — it is a data page first, so the
demo use case lands without touching prompt assembly.

## Page structure

A rule-for-rule port of the `/prompt` page (the tree-with-editor-pane
variant): left panel is the standard folder tree
([`ui-left-panel-tree.md`](../ui-left-panel-tree.md)), right pane is the
selected profile's **form** (where `/prompt` has a textarea). Everything the
tree doc mandates applies verbatim and is not re-specified here: flat
arrays + parent pointers, whole-tree PUT with version guard and `deletes`
counter, recursive render with the shared Lucide folder icons, select-first/
toggle-expand, `?id=<uuid>` deep link, every row a real `<a href>`, kebab via
`box-shadow` dots, drag-drop with cycle guard, `/cron` chrome order (All
profiles → hr → buttons → hr → tree → root-drop strip), and the recursive
folder detail table.

- **Files:** `webapp/profile_views.py` (markup + CSS), `static/profile.js`
  (tree + form JS), `webapp/profile_api.py`, `db/profile.py`. Nav entry
  "Profile" next to Settings.
- **Leaf name = a standalone label** ("Simon", "Demo — no PII", "European"),
  renamed via the click-to-rename modal
  ([`ui-modal-rename.md`](../ui-modal-rename.md)). It is *not* derived from
  first/last name — a demo profile's label ("European") and its example
  person's name ("Lena Fischer") serve different masters.
- **Folder detail table columns:** Name / Person / Language / Units / Time /
  Country — enough to tell demo profiles apart at a glance.
- **Kebab on a profile:** Rename, **Duplicate**, Delete (type-to-confirm).
  Duplicate copies the whole `data` blob into a new row named "<name> copy",
  placed right after the source — the one-action way to mint a friend's
  account from an archetype. (Same shape as `/prompt`'s clone, minus the
  version lineage: no `parent_uuid`, duplication is a convenience, not
  ancestry.)

## Data model

Two tables, mirroring `PromptFolder`/`Prompt` structurally:

- `profile_folder`: `id`, `uuid`, `name`, `description`, `parent_uuid`
  (nullable, no FK), `position`, timestamps.
- `profile`: `id`, `uuid`, `name`, `folder_uuid` (nullable, no FK),
  `position`, timestamps, **`data JSONB NOT NULL DEFAULT '{}'`**.

All person fields live in the single `data` JSONB column rather than one
column per field. Reasons: the field set is explicitly expected to grow
(dynamic info below), every field is optional, nothing needs SQL-side
indexing or joining, and a JSONB blob makes "duplicate profile" and future
import/export trivial. The schema lives in the application as a **field
registry** that is the single source of truth for validation, form
rendering, and (later) prompt rendering:

```python
# profile_fields.py — one row per field; drives the validator AND the form.
# kind: "text" | "enum" | "date" | "email" — the complete set for v1.
PROFILE_FIELDS = [
    # group "Identity"
    Field("first_name",     "Identity", kind="text",  label="First name"),
    Field("last_name",      "Identity", kind="text",  label="Last name"),
    Field("nickname",       "Identity", kind="text",  label="Nickname"),
    Field("gender",         "Identity", kind="enum",  label="Gender",
          choices=["", "male", "female", "other"]),
    Field("preferred_name", "Identity", kind="text",  label="Address them as",
          hint="How the assistant addresses this person, e.g. “Simon” or “you”."),
    Field("birthday",       "Identity", kind="date",  label="Birthday"),
    # group "Locale & formats"
    Field("units",          "Locale & formats", kind="enum", label="Units",
          choices=["", "metric", "imperial"]),
    Field("timezone",       "Locale & formats", kind="text", label="Timezone",
          datalist="tz", hint="IANA name, e.g. Europe/Copenhagen"),
    Field("date_format",    "Locale & formats", kind="enum", label="Date format",
          choices=["", "YYYY-MM-DD", "DD/MM/YYYY", "MM/DD/YYYY", "DD.MM.YYYY"]),
    Field("time_format",    "Locale & formats", kind="enum", label="Time format",
          choices=["", "24h", "12h"]),
    Field("language",       "Locale & formats", kind="text", label="Language (primary)",
          datalist="lang", hint="BCP-47, e.g. da, en-US, zh-Hans"),
    Field("language_2",     "Locale & formats", kind="text", label="Language (secondary)",
          datalist="lang"),
    Field("currency",       "Locale & formats", kind="text", label="Currency (primary)",
          datalist="currency", hint="ISO 4217, e.g. DKK, USD"),
    Field("currency_2",     "Locale & formats", kind="text", label="Currency (secondary)",
          datalist="currency"),
    # group "Contact & location"
    Field("country",        "Contact & location", kind="text", label="Country",
          datalist="country"),
    Field("city",           "Contact & location", kind="text", label="City"),
    Field("address",        "Contact & location", kind="text", label="Address",
          multiline=True),
    Field("email",          "Contact & location", kind="email", label="Email"),
]
```

Design decisions baked in above:

- **Datetime formatting is two enums, not a strftime string.** Free-form
  format strings are a footgun (nobody remembers `%-d`), and four date
  shapes + two clock shapes cover every locale the page targets. The form
  shows a **live preview line** ("Preview: 14.07.2026 · 21:30") rendered
  client-side from the profile's timezone + both formats, updating as the
  selects change — the preview is the documentation.
- **Timezone / language / currency / country are text inputs backed by
  `<datalist>`s**, not hard `<select>`s: the common values are one keystroke
  away, but an uncommon-yet-valid value (a niche IANA zone, a regional
  language tag) is never blocked. Validation is correspondingly soft —
  server-side the validator checks *types* strictly (enum membership, int
  range, string kinds; unknown keys rejected) but does not gatekeep IANA/
  BCP-47/4217 membership. The datalists ship as static JS arrays in
  `profile.js` (timezones via `Intl.supportedValuesOf('timeZone')` at
  runtime — no list to maintain).
- **Birthday is a full date.** This is a personal assistant's record of a
  person: it must support birthday greetings and reminders, not just age
  arithmetic, so the field is a complete date. The form uses a native
  `<input type="date">`; storage is ISO `YYYY-MM-DD` in the JSONB regardless
  of the profile's `date_format` (that enum governs how the *assistant
  presents* dates, not how they are stored).
- **Every field optional.** An empty profile is valid; the form renders
  blanks, the JSONB stays sparse (absent key, not `""`), and later prompt
  rendering skips absent fields.

### Dynamic info (future, shape reserved now)

Current location, screen size, connection type (Telegram / Discord /
rainbox UI) are **observations written by connectors, not fields edited by
humans**. Reserving the shape now costs one rule: connector-written data
goes under `data["dynamic"][...]` with a `seen_at` timestamp per entry, the
validator ignores the `dynamic` subtree, and the form renders it (when
present) as a read-only "Last seen" group under the editable groups. No
connector writes it in this proposal; the rule just prevents a future
migration of editable-vs-observed fields.

## Detail pane and saving

The pane renders the registry's groups as three `<fieldset>`s in registry
order, one label + input per field, the name display (click-to-rename) as
the heading. **Field edits autosave** — debounced 400 ms per profile, one
in-flight PUT with a queued re-send, exactly the tree's debounce discipline —
with a quiet "Saved ✓" status in the pane corner (no toast per keystroke).
Rationale: a Save button on a 17-field form is the inline-rename failure
mode multiplied — type into five fields, wander off, lose five edits. With
autosave there is no dangling state to lose, so no dirty guard and no
`beforeunload` handler are needed. Last write wins per profile, same as
`/prompt` content.

Like `/prompt`'s content, **`data` stays out of the tree payload and the
version hash** (structural fields only: uuid, name, description, parent,
folder, position), saved via its own per-profile PUT — so autosaving a form
field never 409s an open tree, and vice versa.

## API

Mirrors `webapp/prompt_api.py`:

- `GET /profile/api/tree` → `{folders, profiles, version}` (no `data`).
- `PUT /profile/api/tree` — full replace, `version` + `deletes` guarded,
  400 on `ProfileTreeError`, 409 on `ProfileTreeConflict`.
- `GET /profile/api/profiles/<uuid>` → `{ok, uuid, name, data}`.
- `PUT /profile/api/profiles/<uuid>` `{data}` — validates against the
  registry (unknown keys / wrong types / out-of-enum → 400 naming the field),
  writes the whole blob.
- `POST /profile/api/profiles/<uuid>/duplicate` → new row, copied `data`,
  name "<name> copy", positioned after the source.

`db/profile.py` supplies `profile_load_tree` / `profile_save_tree` /
`profile_tree_version` / `validate_profile_tree` (ported from `db/prompt.py`,
including the uuid-collision check that keeps `?id=` unambiguous) plus
`profile_get` / `profile_update_data` / `profile_duplicate` and
`validate_profile_data(data)` built from the registry.

## Seeded examples

On init, when **both tables are empty**, seed one `Example/` folder with five
profiles (fictional people — no real PII, per standing policy). They are
ordinary rows: rename, edit, or delete freely; deleting them does not
re-seed (the emptiness check makes seeding once-only in practice).

| Label | Person | units | time | date | lang | currency | country/city | timezone |
|---|---|---|---|---|---|---|---|---|
| European | Lena Fischer | metric | 24h | DD.MM.YYYY | de / en | EUR | Germany, Berlin | Europe/Berlin |
| US | Mike Johnson | imperial | 12h | MM/DD/YYYY | en-US | USD | USA, Denver | America/Denver |
| Canadian | Claire Tremblay | metric | 12h | YYYY-MM-DD | en-CA / fr-CA | CAD | Canada, Montreal | America/Toronto |
| Chinese | Wei Zhang | metric | 24h | YYYY-MM-DD | zh-Hans / en | CNY | China, Shanghai | Asia/Shanghai |
| Australian | Olivia Baker | metric | 12h | DD/MM/YYYY | en-AU | AUD | Australia, Sydney | Australia/Sydney |

Each also carries a plausible nickname, gender, birthday, and a
`preferred_name`; `email`/`address` stay blank (nothing to demo there, and
blanks show the sparse-JSONB behaviour). The five rows are the living
answer to "what does a filled-in profile look like" — the demo script is:
open `Example/`, duplicate the closest archetype, rename it to the friend,
adjust.

## Phasing

1. **The page.** Tables, `db/profile.py`, API, views + JS (tree ported from
   `/prompt`, pane replaced by the registry-driven form), rename/duplicate/
   delete, autosave, datetime preview, seeds, nav entry.
   *Acceptance:* tree behaviours verified in a real browser per the tree
   doc's §8 process rule (drag to root strip, kebab on selected row,
   type-to-confirm delete — not by code-diffing); form round-trips every
   field; invalid enum/int rejected with the field named; duplicate copies
   all data; seeds appear exactly once on a fresh DB.
2. **Assistant integration (separate proposal when wanted).** `?` Active-
   profile setting, working-context line 1 quoting it, units/timezone/format
   preferences steering assistant output, lens `profile_uuid` linkage.
3. **Accounts (deferred, unchanged).** When the security work's Phase 2
   lands real request identity, an account maps to a person profile
   (identity) + an operator lens (visibility). This table is ready to be
   pointed at; nothing else here presumes auth exists.

## Tests

Ported from the `/prompt` suites plus registry-specific ones — all
deterministic, no live LLM, no browser:

1. Tree: load/save round-trip, version 409 on stale save, `deletes` guard,
   dangling/cyclic folder rejection, folder-vs-profile uuid collision
   rejection.
2. `data` excluded from tree payload and version hash — saving `data` does
   not change `profile_tree_version()`.
3. `validate_profile_data`: unknown key → error naming it; enum out of set →
   error; `birthday` must be a valid ISO calendar date (rejects `2026-02-30`
   and non-ISO shapes); multiline `address` accepted; absent keys
   (sparse blob) valid; `dynamic` subtree ignored.
4. Duplicate: copies `data` deep, new uuid, "<name> copy", positioned after
   source.
5. Seeds: fresh DB → `Example/` + 5 profiles; second init → still 5; a DB
   with any existing row → no seeding.
6. Views: marker-string tests for the pane fieldsets and datalists —
   remembering these prove presence, not behaviour (§8), and that the inline
   JS is a **non-raw** Python string (no bare `\n`-style escapes).

## Alternatives considered

- **One column per field** — rejected: 17 nullable columns today, a
  migration for every future field, and the dynamic subtree wouldn't fit at
  all. The registry gives the same type safety at the validation layer.
- **Reusing the lens JSON files** (`operators/<profile>.json`) as the store —
  rejected: those are hand-edited visibility presets in the customize dir;
  this is CRUD-heavy structured data belonging in Postgres with the other
  trees. The pointer between them (future) is one uuid field.
- **Deriving the leaf name from first/last name** — rejected: demo
  archetypes ("European") and template profiles have labels that are not
  names, and the rename-modal convention wants one explicit rename path.
- **A Save button instead of autosave** — rejected: the dangling-edit
  failure mode the rename-modal doc exists to prevent, multiplied across 17
  fields. Autosave removes the state that can be lost.
- **Locale presets as a first-class mechanism** (pick "Chinese" → fields
  fill in) — rejected as machinery; duplicating a seeded archetype achieves
  the same in one kebab action with zero new concepts.

## See also

- [`../ui-left-panel-tree.md`](../ui-left-panel-tree.md) — the tree pattern;
  `/prompt` is the reference port (editor-pane variant, content out of tree).
- [`../ui-modal-rename.md`](../ui-modal-rename.md),
  [`../ui-modals.md`](../ui-modals.md) — rename + dialog mechanics.
- [`2026-07-07-operator-profiles-and-working-context.md`](2026-07-07-operator-profiles-and-working-context.md)
  — the visibility lens this record will link to; its "not multi-user"
  caveat applies here identically.
- [`2026-06-20-phase3-user-profile.md`](2026-06-20-phase3-user-profile.md)
  — the memory-derived `user_profile` prompt block (distinct namespace;
  this page introduces no Python package named `profile`, which would
  shadow the stdlib profiler).
