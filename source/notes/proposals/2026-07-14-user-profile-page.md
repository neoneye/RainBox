# User profile page (`/profile`)

**Status: implemented.** A left-panel-tree page where each leaf is a **person
profile** — the structured record of a human (name, locale, formats, contact),
editable through a form pane. The immediate use is demoing rainbox to friends:
the operator creates a profile per friend in seconds, and a built-in
`Templates/` folder ships twenty-one read-only locale archetypes covering the
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

`ChatUser` is an actor record, not a fourth profile concept. It identifies who
sent a chat message and is referenced by room creation/membership; a person
profile is the richer human record and can also describe a friend who has no
account and has never entered a chat. The two stay independent in v1. When
accounts land, the account row is the explicit one-to-one binding between a
human `chat_user_uuid` (message actor) and a **user-owned** `profile_uuid`
(person record), with both pointers unique; a built-in template can never be
bound to an account. `ChatUser.name` remains the short chat display label,
while `profile.data.full_name` is the person's full record name. This makes the
future identity join explicit without adding a premature nullable pointer now.

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
  (tree + form JS), `webapp/profile_api.py`, `db/profile.py`,
  `profile_fields.py` (registry), and `data/profile_templates.json` (shipped
  built-ins). The two models live in `db/models.py`; `db/__init__.py` exports
  the persistence functions; `webapp/__init__.py` imports the view/API modules
  so their routes register; and `webapp/core.py` adds the Admin views and the
  nav entry "Profile" next to Settings. Tests live beside the existing prompt
  suites in `db/test_profile_tree.py`, `webapp/test_profile_api.py`, and
  `webapp/test_profile_views.py`. New tables are additive and are created by
  the existing `db.create_all()` startup path.
- **Leaf name = a standalone label** ("Simon", "Demo — no PII", "Germany"),
  renamed via the click-to-rename modal
  ([`ui-modal-rename.md`](../ui-modal-rename.md)). It is *not* derived from
  `full_name` — a template's label ("Germany") and the name of the person it
  describes ("Karl Weierstraß") serve different masters.
- **Folder detail table columns:** Name / Person / Language / Time /
  Country — enough to tell demo profiles apart at a glance. (Units is in the
  summary payload but not the table — it is nearly always metric, so it earns
  no column.)
- **Kebab on a profile:** Rename, **Duplicate**, Delete (type-to-confirm).
  Duplicate copies the whole `data` blob into a new row named "<name> copy",
  placed in the same folder right after a user-owned source — the one-action
  way to mint a friend's account from an archetype. (Same shape as `/prompt`'s
  clone, minus the version lineage: no `parent_uuid`, duplication is a
  convenience, not ancestry.) A built-in source instead produces a real row at
  the end of the user-owned top level, immediately before the virtual
  `Templates/` folder in root rendering. On a built-in template the kebab
  offers **Duplicate only** — no rename, no delete (see below).

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
    Field("full_name",      "Identity", kind="text",  label="Full name",
          hint="However they write it — any script, order, or particles; "
               "one field, never split into first/last."),
    Field("native_name",    "Identity", kind="text",  label="Native name",
          hint="The name in its native script when that differs from the "
               "Latin form — e.g. 湯川秀樹, יובל נאמן, యల్లాప్రగడ సుబ్బారావు."),
    Field("preferred_name", "Identity", kind="text",  label="Address them as",
          hint="What the assistant calls them, e.g. “Simon” or “you”."),
    Field("handle",         "Identity", kind="text",  label="Internet nickname",
          hint="Online handle / username, e.g. “neoneye”."),
    Field("gender",         "Identity", kind="enum",  label="Gender",
          choices=["male", "female", "other"]),
    Field("about",          "Identity", kind="text",  label="About",
          multiline=True,
          hint="Self-description in their own words, e.g. “programmer, "
               "modern day alchemist doing code”."),
    Field("birthday",       "Identity", kind="date",  label="Birthday"),
    # group "Locale & formats"
    Field("units",          "Locale & formats", kind="enum", label="Units",
          choices=["metric", "imperial"]),
    Field("timezone",       "Locale & formats", kind="text", label="Timezone",
          datalist="tz", hint="IANA name, e.g. Europe/Copenhagen"),
    Field("date_format",    "Locale & formats", kind="enum", label="Date format",
          choices=["YYYY-MM-DD", "DD/MM/YYYY", "MM/DD/YYYY",
                   "DD.MM.YYYY", "DD-MM-YYYY"]),
    Field("time_format",    "Locale & formats", kind="enum", label="Time format",
          choices=["24h", "12h"]),
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

- **Names are one `full_name`, not first/last.** A single free-text
  `full_name` holds the whole name however the person writes it — any script,
  any order (family-name-first), particles ("von", "van 't"), multiple given
  names, mononyms, suffixes. Splitting into first/last is a Western
  assumption that mis-handles most of the world; the built-in templates alone
  break it a dozen ways (Wu Chien-Shiung, Ferdinand Jakob Heinrich von
  Mueller, Yuval Ne’eman). Three companion fields carry the *other* jobs a
  name does: **`native_name`** (the same name in its native script — 湯川秀樹,
  יובל נאמן — so the romanized `full_name` stays sortable and pronounceable
  while the authentic spelling is preserved), **`preferred_name`** (what the
  assistant calls them — a given name, a chosen name, or "you"), and
  **`handle`** (their internet nickname / username). The assistant addresses
  them by `preferred_name`, records them by `full_name`, shows the native
  spelling from `native_name`, and can @-mention them by `handle`.
- **Datetime formatting is two enums, not a strftime string.** Free-form
  format strings are a footgun (nobody remembers `%-d`), and five date
  shapes + two clock shapes cover every locale the page targets. The form
  shows a **preview line** ("Preview: 31.12.2026 · 23:59") rendered
  client-side from both format selects, updating as they change — the
  preview is the documentation. The sample values are fixed and chosen to be
  unambiguous: 31 December (31 can only be a day, so DD/MM vs MM/DD is
  readable at a glance) at 23:59 (which can only be a 24h clock; 12h shows
  11:59 pm). Because timezone validation is deliberately soft,
  preview construction is wrapped in `try/catch`: an invalid or half-typed
  zone shows "Preview unavailable — timezone not recognized" and never breaks
  the rest of the form. If `Intl.supportedValuesOf` is unavailable, the
  timezone input still works as free text with an empty datalist.
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
  runtime — no list to maintain). The form adds a client-side **advisory**
  layer on top: an inline warning under timezone/language/currency when the
  typed value is provably invalid (Intl timezone lookup, BCP-47
  canonicalization, 4217 three-letter shape), the field hint as a visible
  placeholder, and a "Use my timezone" one-click fill — guidance for
  non-developers that never blocks a save.
- **Birthday is a full date.** This is a personal assistant's record of a
  person: it must support birthday greetings and reminders, not just age
  arithmetic, so the field is a complete date. The form uses a native
  `<input type="date">`; storage is ISO `YYYY-MM-DD` in the JSONB regardless
  of the profile's `date_format` (that enum governs how the *assistant
  presents* dates, not how they are stored).
- **Every field optional.** An empty profile is valid; the form renders
  blanks, the JSONB stays sparse (absent key, not `""`), and later prompt
  rendering skips absent fields. The blank option rendered for each enum is a
  form affordance, not a registry choice. Before validation, the API
  canonicalizes every known editable key whose value is `""` by removing it;
  it then validates the remaining values and stores only that canonical sparse
  object. Unknown keys are rejected even when their value is empty.

### Dynamic info (future, shape reserved now)

Current location, screen size, connection type (Telegram / Discord /
rainbox UI) are **observations written by connectors, not fields edited by
humans**. Reserving the shape now costs one rule: connector-written data
goes under `data["dynamic"][...]` with a `seen_at` timestamp per entry, the
form renders it (when present) as a read-only "Last seen" group under the
editable groups, and the human-facing PUT rejects a submitted `dynamic` key as
read-only. The server treats that PUT as a complete snapshot of the **editable
registry fields only**: it canonicalizes and validates them, then merges them
into the current row while preserving the row's existing `dynamic` subtree.
Future connectors get a separate narrow update operation for individual
dynamic entries; they never call the human PUT. No connector writes dynamic
data in this proposal, but defining ownership and merge behaviour now prevents
both a migration and a stale form autosave overwriting a newer observation.

## Detail pane and saving

The pane renders the registry's groups as three `<fieldset>`s in registry
order, one label + input per field, the name display (click-to-rename) as
the heading. **Field edits autosave** — debounced 400 ms per profile, with
timers/state keyed by profile uuid, one in-flight PUT per profile, and a queued
re-send carrying the newest snapshot. A late detail GET is discarded unless
its uuid is still selected. The pane status moves through "Saving…", "Saved
✓", and "Save failed — retrying"; failures retain the dirty snapshot and
retry with exponential backoff whose delay is capped while retries continue
for as long as the page is open (and immediately on the next edit or `online`
event), rather than silently waiting for an unrelated change. On
acknowledgement the client also refreshes that row's local `summary` from the
canonical editable snapshot, so a subsequently opened folder table reflects
the saved values without reloading the whole tree.

A Save button on a 19-field form would leave too much easy-to-lose state, but
autosave still has a short dirty window. A `beforeunload` dirty guard is active
only while a save is pending or failed and is removed as soon as the latest
snapshot is acknowledged; if the operator deliberately confirms leaving, the
UI makes clear that the pending edit may be lost. In-page profile selection
does not cancel another profile's timer. These lifecycle rules make the
remaining risk visible instead of claiming that debounce alone eliminates
unsaved state. Last acknowledged write wins per profile, same as `/prompt`
content.

Like `/prompt`'s content, the **full `data` blob stays out of the tree payload
and the version hash**. The small derived `summary` below may ride on GET rows,
but the hash still covers structural fields only (uuid, name, description,
parent, folder, position). Data saves use their own per-profile PUT, so
autosaving a form field never 409s an open tree, and vice versa.

## API

Mirrors `webapp/prompt_api.py`:

- `GET /profile/api/tree` → `{folders, profiles, version}` (no `data`).
  Each profile carries a read-only `summary` projection containing
  `full_name`, `language`, `units`, `time_format`, and `country`, which is
  sufficient for the recursive folder detail table without an N-request
  detail-fetch fan-out. `summary` is derived from `data`, is not accepted by
  PUT, and is excluded from `version` just like the full data blob. Built-in
  entries are merged in with `builtin: true` and the same summary shape so the
  client renders them without a second fetch; they are excluded from
  `version`.
- `PUT /profile/api/tree` — full replace of the **user-owned** tree,
  `version` + `deletes` guarded. The client projects its mixed GET state back
  to structural folder/profile keys only, omitting every built-in row and every
  `summary`; the validator rejects either if submitted. 400 on
  `ProfileTreeError` (including any payload carrying a built-in uuid), 409 on
  `ProfileTreeConflict`.
- `GET /profile/api/profiles/<uuid>` → `{ok, uuid, name, data, builtin}` —
  serves built-ins from the shipped file, user rows from the DB.
- `PUT /profile/api/profiles/<uuid>` `{data}` — accepts a complete editable
  snapshot, canonicalizes blank strings away, validates against the registry
  (unknown keys / wrong types / out-of-enum → 400 naming the field), rejects
  `dynamic` as read-only, and replaces the editable keys while preserving the
  server's current `dynamic` subtree. 400 "read-only built-in" for a built-in
  uuid.
- `POST /profile/api/profiles/<uuid>/duplicate` → new row with copied `data`
  and name "<name> copy". A user-owned copy is placed in the same folder after
  its source; a built-in copy is placed at the end of the user-owned top level.
  Before every POST, the client first flushes pending structural tree edits so
  the source exists and the duplicate cannot invalidate a queued stale tree
  PUT. For a user-owned source it then cancels the data debounce and awaits the
  newest data PUT. Failure of either flush aborts duplication visibly. After a
  successful POST it reloads the tree, matching `/prompt` clone's version
  discipline.

`db/profile.py` supplies `profile_load_tree` / `profile_save_tree` /
`profile_tree_version` / `validate_profile_tree` (ported from `db/prompt.py`,
including the uuid-collision check that keeps `?id=` unambiguous) plus
`profile_get` / `profile_update_data` / `profile_duplicate` and
`validate_profile_data(data)` built from the registry. Validation returns the
canonical sparse editable object; `profile_update_data` merges it with the
current server-owned dynamic subtree in the same transaction. Concretely, the
stored result is the canonical editable object plus the pre-update `dynamic`
value when one exists; editable keys omitted from the complete snapshot are
deleted, not retained accidentally.

## Built-in templates (read-only, shipped with the app)

The `Templates/` folder and its twenty-one profiles are **not DB rows**. They
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

The twenty-one profiles show each locale's *typical* conventions — they are
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
| Canada | Mieczysław Grzegorz Bekker | founded terramechanics; designed the lunar rover's wheels | metric | 12h | YYYY-MM-DD | fr-CA / en-CA | CAD | Montreal | America/Toronto |
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
| Norway | Fredrik Carl Mülertz Størmer | computed the charged-particle orbits behind the aurora | metric | 24h | DD.MM.YYYY | nb / en | NOK | Oslo | Europe/Oslo |
| Poland | Zofia Kielan-Jaworowska | led the Gobi expeditions that rewrote early-mammal evolution | metric | 24h | DD.MM.YYYY | pl / en | PLN | Warsaw | Europe/Warsaw |
| Israel | Yuval Ne’eman | ordered the particle zoo (SU(3)) | metric | 24h | DD/MM/YYYY | he / en | ILS | Tel Aviv | Asia/Jerusalem |
| India | Yallāpragaḍa Subbārāvu | co-created methotrexate chemotherapy | metric | 12h | DD/MM/YYYY | en-IN / te | INR | Bengaluru | Asia/Kolkata |
| China | Wu Chien-Shiung | overthrew parity conservation | metric | 24h | YYYY-MM-DD | zh-Hans / en | CNY | Shanghai | Asia/Shanghai |
| Japan | Hideki Yukawa | predicted the meson | metric | 24h | YYYY-MM-DD | ja / en | JPY | Tokyo | Asia/Tokyo |
| South Korea | Woo Jang-choon | the triangle of U | metric | 12h | YYYY-MM-DD | ko / en | KRW | Seoul | Asia/Seoul |
| Singapore | Wu Lien-teh | pioneered modern epidemic control | metric | 12h | DD/MM/YYYY | en-SG / zh | SGD | Singapore | Asia/Singapore |
| Australia | Ferdinand Jakob Heinrich von Mueller | documented Australia's flora | metric | 12h | DD/MM/YYYY | en-AU | AUD | Melbourne | Australia/Melbourne |

Each entry's `country` field carries the country name (the label doubles as
it, so the column is omitted above). Rough grouping in the file — Americas,
Europe, Middle East, Asia, Oceania — is also the fixed tree order. Some
rows are career or legacy placements rather than birthplaces — Wu Lien-teh
(Straits Settlements), Wu Chien-Shiung (Chinese-born, career in the US), Ynés
Mexía (US-born, of Mexican heritage, collected across Mexico), Ferdinand von
Mueller (German-born, Victoria's founding Government Botanist), Mieczysław
Bekker (Polish-born, a decade in the Canadian Army's vehicle research) — all
earn their rows on the strength of the discovery. The About
column above is each entry's `about` value — the self-description field
every profile has (the operator's own might read "programmer, modern day
alchemist doing code"); on the templates it holds the discovery, so opening
any template teaches something.

Each also carries gender, a modern plausible birthday, and a
`preferred_name` (the scientist's given name); `handle`/`email`/`address`
stay blank (nothing to demo there, and blanks show the sparse-JSONB
behaviour). Every template's `full_name` is the romanized Latin form (so it
sorts and reads everywhere); the ones whose scientist used a non-Latin script
also fill `native_name`: 吳健雄 (Wu Chien-Shiung), 湯川秀樹 (Hideki Yukawa),
우장춘 (Woo Jang-choon), 伍連德 (Wu Lien-teh), יובל נאמן (Yuval Ne’eman),
యల్లాప్రగడ సుబ్బారావు (Subbārāvu).

**The templates are also the name-handling test fixture.** Across their
`full_name`s, `native_name`s, abouts, and cities they deliberately cover
Latin diacritics (É é í â è ó ã), the Danish Ø ("Øjvind Winge"), the Swedish
Å and ö ("Ångström" — a special letter at the very start of the string), the
German ß (Weierstraß), the Polish ł ("Mieczysław Grzegorz Bekker"), the
macron and retroflex under-dot of Indic
transliteration (ā + ḍ, "Yallāpragaḍa Subbārāvu"), Greek (ε–δ), and — in
`native_name` — CJK, Hangul, Telugu, and right-to-left Hebrew. On top of the
scripts come the awkward *shapes* a name takes, all in the one `full_name`
field, which is the whole point: there is no first/last split to
mis-handle. A generational suffix ("Raymond Davis Jr."), a particle
surname with an apostrophe and internal space ("Jacobus van 't Hoff"), three
given names before a nobiliary particle ("Ferdinand Jakob Heinrich von
Mueller"), an apostrophe inside a given name ("D'Arcy Wentworth Thompson"), a
typographic apostrophe for a Hebrew ayin ("Yuval Ne’eman" — U+2019, not the
ASCII `'` of the two above), a compound surname joined by a conjunction
("Maurício Rocha e Silva"), a hyphenated double surname ("Zofia
Kielan-Jaworowska"), and a family-name-first order (the `native_name`s
above). So an encoding,
rendering, or ordering bug anywhere on the page (tree row, form field, folder
detail table, JSON round-trip) shows up on shipped data before it can corrupt
an operator's own. The validate-the-shipped-file test doubles as the encoding
round-trip test.

The twenty-one profiles are the living
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
   all data; an edit followed immediately by Duplicate copies the edit; an
   invalid/half-typed timezone leaves the form working; pending/failed saves
   visibly guard page exit; built-ins render read-only, survive a tree save
   untouched, and cannot be edited, renamed, deleted, or dragged.
2. **Assistant integration (separate proposal when wanted).** `?` Active-
   profile setting, working-context line 1 quoting it, units/timezone/format
   preferences steering assistant output, lens `profile_uuid` linkage.
3. **Accounts (deferred, unchanged).** When the security work's Phase 2
   lands real request identity, an account binds one human `ChatUser` actor to
   one user-owned person profile (identity) and selects an operator lens
   (visibility). Both account pointers are unique; built-ins are ineligible.
   Nothing in v1 presumes auth exists.

## Tests

The automated suites are ported from `/prompt` plus registry-specific cases;
they are deterministic, use no live LLM, and require no browser. The explicit
real-browser acceptance checks remain in Phase 1 above.

1. Tree: load/save round-trip, version 409 on stale save, `deletes` guard,
   dangling/cyclic folder rejection, folder-vs-profile uuid collision
   rejection; a tree PUT carrying derived `summary` is rejected.
2. `data` excluded from tree payload and version hash — saving `data` does
   not change `profile_tree_version()`. Tree rows expose only the five-field
   derived `summary`; changing data updates that summary without changing the
   structural version.
3. `validate_profile_data`: unknown key → error naming it; enum out of set →
   error; `birthday` must be a valid ISO calendar date (rejects `2026-02-30`
   and non-ISO shapes); multiline `address` accepted; absent keys
   (sparse blob) valid; known `""` values canonicalized away; unknown empty
   keys still rejected; a submitted `dynamic` key rejected as read-only.
4. Duplicate of a user-owned source: copies `data` deep, new uuid, "<name>
   copy", positioned after the source. Browser acceptance additionally covers
   edit → immediate Duplicate, proving the client flushes the pending data
   save before the POST.
5. Built-ins: present in `GET tree` with `builtin: true` on a fresh DB;
   excluded from `profile_tree_version()`; a tree PUT containing a built-in
   uuid → 400; data PUT on a built-in uuid → 400; a user row reusing a
   built-in uuid rejected by the validator; duplicate of a built-in creates
   a real editable top-level row; `data/profile_templates.json` entries all
   pass `validate_profile_data` (so a release can't ship a broken template).
6. Dynamic merge: seed a server-side `dynamic` entry, PUT a stale editable
   snapshot, and prove the observation survives byte-for-byte; duplicate still
   copies the complete stored blob, including dynamic.
7. Views: marker-string tests for the pane fieldsets and datalists —
   remembering these prove presence, not behaviour (§8) — plus the
   mtime-cache-busted external `profile.js` reference. Any small inline script
   left in the non-raw Python HTML template contains no bare `\n`-style
   escapes.

## Alternatives considered

- **One column per field** — rejected: 19 nullable columns today, a
  migration for every future field, and the dynamic subtree wouldn't fit at
  all. The registry gives the same type safety at the validation layer.
- **Reusing the lens JSON files** (`operators/<profile>.json`) as the store —
  rejected: those are hand-edited visibility presets in the customize dir;
  this is CRUD-heavy structured data belonging in Postgres with the other
  trees. The pointer between them (future) is one uuid field.
- **Deriving the leaf name from `full_name`** — rejected: template labels
  ("Germany") are not names, the operator's own label ("Simon") is shorter
  than their full name, and the rename-modal convention wants one explicit
  rename path.
- **A first-name / last-name pair** — rejected: it is a Western assumption
  that mishandles family-name-first order, mononyms, particles, and multiple
  given names (the templates break it a dozen ways). One `full_name`, a
  `native_name` (the native-script spelling), a `preferred_name` (what to
  call them), and a `handle` (their online nickname) cover the jobs a name
  actually does without imposing a structure most of the world's names don't
  have.
- **A Save button instead of autosave** — rejected: the dangling-edit
  failure mode the rename-modal doc exists to prevent, multiplied across 19
  fields. Autosave minimizes routine dirty state; the narrowly active unload
  guard covers its debounce, in-flight, and retry windows honestly.
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
