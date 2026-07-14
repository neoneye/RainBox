# User profile page (`/profile`)

**Status: proposal.** A new left-panel-tree page where each leaf is a **person
profile** — the structured record of a human (name, locale, formats, contact),
editable through a form pane. The immediate use is demoing rainbox to friends:
the operator creates a profile per friend in seconds, and a built-in
`Templates/` folder ships twenty read-only locale archetypes covering the
major tech countries (US, Germany, Japan, South Korea, India, China, …)
that double as documentation of what a filled-in profile looks like and
update with every rainbox release. The longer arc is multi-user preparation: this table is the person
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
- **Leaf name = a standalone label** ("Simon", "Demo — no PII", "Germany"),
  renamed via the click-to-rename modal
  ([`ui-modal-rename.md`](../ui-modal-rename.md)). It is *not* derived from
  first/last name — a template's label ("Germany") and the name of the
  person it describes ("Karl Weierstraß") serve different masters.
- **Folder detail table columns:** Name / Person / Language / Units / Time /
  Country — enough to tell demo profiles apart at a glance.
- **Kebab on a profile:** Rename, **Duplicate**, Delete (type-to-confirm).
  Duplicate copies the whole `data` blob into a new row named "<name> copy",
  placed right after the source — the one-action way to mint a friend's
  account from an archetype. (Same shape as `/prompt`'s clone, minus the
  version lineage: no `parent_uuid`, duplication is a convenience, not
  ancestry.) On a built-in template the kebab offers **Duplicate only** —
  no rename, no delete (see below).

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
    Field("about",          "Identity", kind="text",  label="About",
          multiline=True,
          hint="Self-description in their own words, e.g. “programmer, "
               "modern day alchemist doing code”."),
    Field("birthday",       "Identity", kind="date",  label="Birthday"),
    # group "Locale & formats"
    Field("units",          "Locale & formats", kind="enum", label="Units",
          choices=["", "metric", "imperial"]),
    Field("timezone",       "Locale & formats", kind="text", label="Timezone",
          datalist="tz", hint="IANA name, e.g. Europe/Copenhagen"),
    Field("date_format",    "Locale & formats", kind="enum", label="Date format",
          choices=["", "YYYY-MM-DD", "DD/MM/YYYY", "MM/DD/YYYY",
                   "DD.MM.YYYY", "DD-MM-YYYY"]),
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
  format strings are a footgun (nobody remembers `%-d`), and five date
  shapes + two clock shapes cover every locale the page targets. The form
  shows a **live preview line** ("Preview: 14.07.2026 · 21:30") rendered
  client-side from the profile's timezone + both formats, updating as the
  selects change — the preview is the documentation.
- **Locale fields never constrain each other.** Country, units, language,
  and the format selects are independent preferences: a European profile
  with `YYYY-MM-DD` (the operator's own choice — ISO 8601 is common in
  tech and in parts of Europe) is a first-class configuration, not an
  inconsistency. No field's value filters another field's options, and
  nothing warns about "unusual" combinations.
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
  Built-in entries are merged in with `builtin: true` so the client renders
  them without a second fetch; they are excluded from `version`.
- `PUT /profile/api/tree` — full replace of the **user-owned** tree,
  `version` + `deletes` guarded, 400 on `ProfileTreeError` (including any
  payload carrying a built-in uuid), 409 on `ProfileTreeConflict`.
- `GET /profile/api/profiles/<uuid>` → `{ok, uuid, name, data, builtin}` —
  serves built-ins from the shipped file, user rows from the DB.
- `PUT /profile/api/profiles/<uuid>` `{data}` — validates against the
  registry (unknown keys / wrong types / out-of-enum → 400 naming the field),
  writes the whole blob. 400 "read-only built-in" for a built-in uuid.
- `POST /profile/api/profiles/<uuid>/duplicate` → new row, copied `data`,
  name "<name> copy", positioned after the source (for a built-in source:
  a top-level row named after the template).

`db/profile.py` supplies `profile_load_tree` / `profile_save_tree` /
`profile_tree_version` / `validate_profile_tree` (ported from `db/prompt.py`,
including the uuid-collision check that keeps `?id=` unambiguous) plus
`profile_get` / `profile_update_data` / `profile_duplicate` and
`validate_profile_data(data)` built from the registry.

## Built-in templates (read-only, shipped with the app)

The `Templates/` folder and its twenty profiles are **not DB rows**. They
ship as a data file, `data/profile_templates.json` (the same shipped-content
pattern as `data/operators/demo.json` and the base Q&A registry): one entry
per profile with a **fixed, hardcoded uuid** (so `?id=` deep links survive
restarts and releases), a name, and a `data` blob. `db/profile.py` loads
and caches the file and merges the entries into the `GET /profile/api/tree`
response under a virtual `Templates` folder (also fixed-uuid), rendered after
the operator's own root content.

Read-only and always-current then fall out by construction, with no guard
code to get wrong:

- **Undeletable/unrenamable/uneditable** — there is no row to delete. The
  tree PUT never includes them (the client excludes them; the validator
  additionally rejects any payload carrying a built-in uuid, and the
  uuid-collision check keeps user rows off those uuids). `PUT
  /profile/api/profiles/<builtin-uuid>` returns 400 "read-only built-in".
- **Updated with rainbox** — the file is part of the release; a new version
  of rainbox serves the new content on the next page load. No migration, no
  re-seed logic, no drift between installs.
- **UI affordances:** built-in rows render with a subtle "built-in" tag;
  they are not draggable and the `Templates` folder accepts no drops; the
  form pane shows their fields disabled with a hint line "Built-in template —
  Duplicate to make an editable copy"; the kebab offers only Duplicate.
  Duplicating a built-in creates a **real** top-level row (the virtual
  folder can't hold user rows) named after the template.

The twenty profiles show each locale's *typical* conventions — they are
starting points for duplication, not rules; any profile, including the
operator's own, sets whatever formats it prefers (a European choosing
`YYYY-MM-DD` just picks it in the selector). Each is **named in homage to a
deceased, groundbreaking scientist from that country**, deliberately
skipping the household names (no Einstein, no Newton, no von Neumann) in
favour of the discoverers one layer down. **Only the dead qualify** — a
living namesake is an open-ended reputational risk (they may later do
something that makes the name an unfortunate default), a risk a dead
person's completed life cannot carry. The names are the only historical
element: every other detail stays fictional and modern (per the no-real-PII
policy, and so a demo never shows a 19th-century birth year):

| Label | Person | About | units | time | date | lang | currency | city | timezone |
|---|---|---|---|---|---|---|---|---|---|
| US | Raymond Davis Jr. | detected solar neutrinos | imperial | 12h | MM/DD/YYYY | en-US | USD | Denver | America/Denver |
| Canada | Marie-Victorin | wrote the Flore laurentienne, Québec's definitive botany | metric | 12h | YYYY-MM-DD | fr-CA / en-CA | CAD | Montreal | America/Toronto |
| Mexico | Ynés Mexía | discovered some 500 new plant species | metric | 12h | DD/MM/YYYY | es-MX / en | MXN | Mexico City | America/Mexico_City |
| Brazil | Maurício Rocha e Silva | discovered bradykinin, the blood-pressure peptide | metric | 24h | DD/MM/YYYY | pt-BR / en | BRL | São Paulo | America/Sao_Paulo |
| UK | D'Arcy Wentworth Thompson | founded mathematical biology (On Growth and Form) | metric | 12h | DD/MM/YYYY | en-GB | GBP | London | Europe/London |
| France | Émilie du Châtelet | showed kinetic energy scales with velocity squared | metric | 24h | DD/MM/YYYY | fr / en | EUR | Paris | Europe/Paris |
| Germany | Karl Weierstraß | made calculus rigorous (ε–δ) | metric | 24h | DD.MM.YYYY | de / en | EUR | Berlin | Europe/Berlin |
| Netherlands | Jacobus van 't Hoff | founded stereochemistry | metric | 24h | DD-MM-YYYY | nl / en | EUR | Amsterdam | Europe/Amsterdam |
| Spain | Santiago Ramón y Cajal | showed the brain is made of neurons | metric | 24h | DD/MM/YYYY | es / en | EUR | Madrid | Europe/Madrid |
| Italy | Rita Levi-Montalcini | discovered nerve growth factor | metric | 24h | DD/MM/YYYY | it / en | EUR | Milan | Europe/Rome |
| Denmark | Øjvind Winge | founded the genetics of yeast | metric | 24h | DD.MM.YYYY | da / en | DKK | Copenhagen | Europe/Copenhagen |
| Sweden | Anders Jonas Ångström | pioneered spectroscopy | metric | 24h | YYYY-MM-DD | sv / en | SEK | Stockholm | Europe/Stockholm |
| Poland | Zofia Kielan-Jaworowska | led the Gobi expeditions that rewrote early-mammal evolution | metric | 24h | DD.MM.YYYY | pl / en | PLN | Warsaw | Europe/Warsaw |
| Israel | Yuval Ne’eman | ordered the particle zoo (SU(3)) | metric | 24h | DD/MM/YYYY | he / en | ILS | Tel Aviv | Asia/Jerusalem |
| India | Satyendra Nath Bose | Bose–Einstein statistics | metric | 12h | DD/MM/YYYY | en-IN / hi | INR | Bengaluru | Asia/Kolkata |
| China | Wu Chien-Shiung | overthrew parity conservation | metric | 24h | YYYY-MM-DD | zh-Hans / en | CNY | Shanghai | Asia/Shanghai |
| Japan | Hideki Yukawa | predicted the meson | metric | 24h | YYYY-MM-DD | ja / en | JPY | Tokyo | Asia/Tokyo |
| South Korea | Woo Jang-choon | the triangle of U | metric | 12h | YYYY-MM-DD | ko / en | KRW | Seoul | Asia/Seoul |
| Singapore | Wu Lien-teh | pioneered modern epidemic control | metric | 12h | DD/MM/YYYY | en-SG / zh | SGD | Singapore | Asia/Singapore |
| Australia | Howard Florey | turned penicillin into a medicine | metric | 12h | DD/MM/YYYY | en-AU | AUD | Sydney | Australia/Sydney |

Each entry's `country` field carries the country name (the label doubles as
it, so the column is omitted above). Rough grouping in the file — Americas,
Europe, Middle East, Asia, Oceania — is also the fixed tree order. Some
rows are career or legacy placements rather than birthplaces — Wu Lien-teh
(Straits Settlements), Wu Chien-Shiung (Chinese-born, career in the US), Ynés
Mexía
(US-born, of Mexican heritage, collected across Mexico) — all earn their
rows on the strength of the discovery. The About
column above is each entry's `about` value — the self-description field
every profile has (the operator's own might read "programmer, modern day
alchemist doing code"); on the templates it holds the discovery, so opening
any template teaches something.

Each also carries gender, a modern plausible birthday, and a
`preferred_name` (the scientist's given name); `email`/`address` stay blank
(nothing to demo there, and blanks show the sparse-JSONB behaviour). For
the entries whose scientist wrote their name in a non-Latin script, the
`nickname` field holds the native spelling: 吳健雄 (Wu Chien-Shiung),
湯川秀樹 (Yukawa), 우장춘 (Woo), 伍連德 (Wu Lien-teh), יובל נאמן
(Ne’eman), সত্যেন্দ্রনাথ বসু (Bose).
Canada's shows the field's other purpose — the name a person actually goes
by: `first_name`/`last_name` are Conrad/Kirouac, `nickname` is "Frère
Marie-Victorin", the religious name all of Québec knew him by.

**The templates are also the name-handling test fixture.** Between the
names, nicknames, abouts, and cities they deliberately cover Latin
diacritics (É é í â è ó ã), the Danish Ø ("Øjvind Winge"), the Swedish Å and
ö ("Ångström" — a special letter at the very start of the string), the
German ß (Weierstraß), Greek (ε–δ), CJK,
Hangul, Bengali, right-to-left Hebrew, a generational suffix
(`last_name` "Davis Jr."), an apostrophe-particle surname (`last_name`
"van 't Hoff" — apostrophe, internal space, lowercase particles), an
apostrophe in the given name (`first_name` "D'Arcy Wentworth"), a
typographic apostrophe standing for a Hebrew ayin (`last_name` "Ne’eman" —
U+2019, not the ASCII `'` of the two above), a
Portuguese compound surname joined by a conjunction (`last_name` "Rocha e
Silva"), a hyphenated double surname (Kielan-Jaworowska), and a person whose everyday
name lives in `nickname` rather than first/last (Frère
Marie-Victorin) — standing tests that nothing assumes a last name is one
capitalized dot-free word — so an encoding, rendering, or name-splitting
bug anywhere on the page (tree row, form field, folder detail table, JSON
round-trip) shows up on shipped data before it can corrupt an operator's
own. The validate-the-shipped-file test doubles as the encoding round-trip
test.

The twenty profiles are the living
answer to "what does a filled-in profile look like" — the demo script is:
open `Templates/`, duplicate the closest archetype, rename it to the friend,
adjust.

## Phasing

1. **The page.** Tables, `db/profile.py`, API, views + JS (tree ported from
   `/prompt`, pane replaced by the registry-driven form), rename/duplicate/
   delete, autosave, datetime preview, built-in templates, nav entry.
   *Acceptance:* tree behaviours verified in a real browser per the tree
   doc's §8 process rule (drag to root strip, kebab on selected row,
   type-to-confirm delete — not by code-diffing); form round-trips every
   field; invalid enum/date rejected with the field named; duplicate copies
   all data; built-ins render read-only, survive a tree save untouched, and
   cannot be edited, renamed, deleted, or dragged.
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
5. Built-ins: present in `GET tree` with `builtin: true` on a fresh DB;
   excluded from `profile_tree_version()`; a tree PUT containing a built-in
   uuid → 400; data PUT on a built-in uuid → 400; a user row reusing a
   built-in uuid rejected by the validator; duplicate of a built-in creates
   a real editable top-level row; `data/profile_templates.json` entries all
   pass `validate_profile_data` (so a release can't ship a broken template).
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
  fill in) — rejected as machinery; duplicating a built-in archetype
  achieves the same in one kebab action with zero new concepts.
- **Templates as seeded or flagged DB rows** (seed-once-when-empty, or rows
  with a `builtin` column protected by guards) — rejected. Seed-once can't
  deliver updates (edited/deleted rows must not be resurrected, so new
  releases can't touch them), and a protected-row design needs delete/rename/
  edit/drag guards in four places that all have to be right. Virtual entries
  from a shipped file make read-only and always-current structural: there is
  no row to delete and the file *is* the release.

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
