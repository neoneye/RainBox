# Settings — design (registry + /settings page)

A code-side registry (`db/settings.py`) declares every configurable key;
values persist in the `app_setting` Postgres table; the `/settings` page
renders one editable card per key with provenance badges and Q&A action
buttons.

## The idea

Operator configuration is **typed, code-owned, and DB-backed**. The registry in code is the single source of truth for *which keys exist* and their type/default/validation; the database only holds the *value*. Every key resolves in a fixed priority order — **DB value → env var → default** — so the app works out of the box on env vars alone, and anything set on the page cleanly overrides the environment. The `/settings` page shows exactly where each live value comes from, so "why is this on?" is answered by a badge, not by grepping.

## Where things live

| Piece | File |
|-------|------|
| Registry (`SETTINGS`), accessors, reconcile | `db/settings.py` (re-exported from the `db` facade) |
| Table (`AppSetting`) | `db/models.py` |
| Page + JSON API (set / repopulate / rebuild) | `webapp/settings_views.py` |
| Q&A sync/rebuild backends | `memory/seed_memory.py` (`sync_kb`, `rebuild_kb`, `available_qa_shields`) |
| Tests | `db/test_settings.py`, `webapp/test_settings_views.py` |

## The registry

One frozen `Setting` dataclass per key in the `SETTINGS` dict:

```
Setting(key, env, type, default,
        secret=False, validate=None, description="",
        dynamic_default=None)
```

- **`type`** ∈ `string | bool | int | json`. Values are stored as text in the DB and coerced on read (`_coerce`): booleans accept `1/true/yes/on` and `0/false/no/off`, json goes through `json.loads`.
- **`env`** — the legacy environment variable this key shadows (the fallback layer), or `None` for DB-only keys.
- **`validate`** — an optional callable run on write (e.g. age-recipient shape, model-uuid existence). A failing validator rejects the save.
- **`dynamic_default`** — a computed fallback used *instead of* `default` when both DB and env are unset (e.g. `chat.default_model` derives from the `model_config` table). Runs on every unset `get_setting()`, so it must be cheap and app-context safe.
- **`secret`** — a `secret=True` setting is **env-only**: `set_setting` refuses to store a value for it (it would land in cleartext in Postgres and in every backup — threat model in `docs/backup.md`), `all_settings()` redacts its value, and the page renders it read-only ("environment-managed"). No currently registered key is secret; the machinery is in place for when one is.
- **`internal`** — machine-owned bookkeeping keys (event stamps) that `all_settings()` omits by default (`include_internal=True` lists them), so the `/settings` page never shows them; `get_setting`/`set_setting` treat them like any other key. Distinct from `secret`: secrets are redacted but still listed, internal keys are ordinary values that are simply not operator-facing.

Unknown keys raise `UnknownSetting` — there is no ad-hoc key creation.

## Registered keys

| Key | Type | Default | Env fallback | What it controls |
|-----|------|---------|--------------|------------------|
| `backup.repo` | string | unset | `RAINBOX_BACKUP_REPO` | Directory backups are written under. |
| `backup.age_recipient` | string | unset | `RAINBOX_BACKUP_AGE_RECIPIENT` | age public key(s) backups are encrypted to (whitespace/comma separated; each token validated as `age1…` — SSH recipients go in the env-only recipients file). |
| `backup.git_push` | bool | `false` | `RAINBOX_BACKUP_GIT_PUSH` | Commit+push each backup into the backup-repo git repo. |
| `cron.paused` | bool | `false` | — | Global cron pause: the scheduler fires nothing while on; per-job/folder flags are untouched, so resume restores the prior state. |
| `assistant.disabled_capabilities` | json | `[]` | — | Assistant capability names the operator has turned off; a disabled capability is removed from the assistant's prompt catalog *and* its dispatch path. |
| `chat.default_model` | string | dynamic: alphabetically earliest model-config override | — | Model a direct chat room talks to while the room has no model selected (a ModelConfig / ModelConfigOverride uuid; validated to exist). |
| `customize.dir` | string | unset | `RAINBOX_CUSTOMIZE_DIR` | Directory with the operator's private customizations (PII / persona); its `question_answer.jsonl` overlays the base Q&A registry by id. |
| `qa.unlocked_shields` | json | `[]` | — | Names of unlocked Q&A shields. A shielded Q&A entry reaches the LLM only when its shield is listed; empty keeps every shielded entry hidden. |
| `qa.facts_invalidated_at` | string | unset | — | ISO timestamp of the last change that can stale prior facts (shield toggle, Q&A repopulate, or profile switch); the assistant posts a one-time "re-check facts" notice per room after it changes. |
| `profile.current` | string | unset | — | The `/profile` profile that IS the operator (validated to exist, built-ins allowed). Drives the assistant's identity, formatting-guide, and knowledge-calibration blocks; unset = none of them. The page edits it with a profile picker showing names, not uuids. |
| `profile.current_changed_at` | string | unset | — | **Internal.** Event stamp of the last actual `profile.current` change, written by `set_current_profile` in the same transaction as the facts stamp; acknowledged per room by the assistant's context marker. Never listed on `/settings`. |

Main consumers: `db/cron.py` (pause, backup destination/recipients/push), `agents/assistant.py` (disabled capabilities, facts stamp), `webapp/chat_api.py` + `agents/direct_chat.py` (default model), `memory/seed_memory.py` + `agents/mcp_config.py` + `skills/loader.py` (customize dir, shields).

## Resolution order and "unset"

`get_setting(key)` returns the first layer that holds a real value:

1. **DB** — the `app_setting` row's `value`, if set.
2. **Env** — the registry's `env` var, if set.
3. **Default** — `dynamic_default()` if declared, else `default`.

"Set" is type-aware (`_is_unset`): for **string/json**, `NULL` *or* empty/whitespace text counts as unset and falls through; for **bool/int**, only `NULL` is unset — `false` and `0` are explicit stored values. `_source()` re-runs the same walk to label each key `db` / `env` / `default` for the UI.

`get_setting` reads `db.session`, so it **requires a Flask app context** (the web app and the cron scheduler both have one). Standalone CLI tools do not call it — they take explicit arguments instead.

## Read/write API

- **`get_setting(key) -> object`** — resolved, type-coerced value (above).
- **`set_setting(key, value)`** — persists `value` as text (`_to_text`: json via `json.dumps`); `None` clears the row's value to `NULL`, dropping the DB layer so env/default apply again. Runs the validator (skipped for empty text), enforces the env-only rule for secrets, and re-stamps the row's cached metadata. Commits immediately.
- **`all_settings() -> list[dict]`** — every registry key with effective value, `value_type`, `secret`, `description`, and `source`; secrets redacted (`REDACTED = "••••••"`). Metadata always comes from the registry, never the row, so it cannot drift.
- **`mark_facts_invalidated() -> str`** — stamps `qa.facts_invalidated_at` with now (UTC ISO) and returns it.
- **`set_current_profile(value) -> str | None`** — the runtime write path for `profile.current`: validates the target, no-ops on the same effective uuid (returns `None`), and on an actual change writes the pointer, advances `qa.facts_invalidated_at`, and stores that same stamp in `profile.current_changed_at` as **one transaction** (via the no-commit `_upsert_setting_row` helper, rolled back wholly on failure) — a concurrent assistant turn sees either the complete old state or the complete new state. A plain `set_setting("profile.current", ...)` still works but stamps nothing; it is the low-level seam for tests and scripts.
- **`reconcile_app_settings()`** — idempotent, called from `init_db`: ensures a row exists per registry key (value left `NULL`) and re-stamps `value_type`/`secret`/`description` from the registry. The `AppSetting` columns beyond `key`/`value` are a **seeded cache** of the registry, never an independent source of truth.

## The /settings page

`GET /settings` (`webapp/settings_views.py`) renders `all_settings()` as JSON into an inline script (escaped so a value containing `</script>` cannot break out) alongside the discovered Q&A shields and the chat-model choices. All rendering is client-side.

**Form generation is type-driven.** Each key gets a card: monospace key, type chip, description, the effective value, and a provenance badge (`from db` / `from env` / `from default`, with hover help and a legend at the top). The **Edit** button opens a modal whose control depends on the setting:

- `bool` → a `<select>` with `(unset)`, `true`, `false`.
- `int` → `<input type=number>`; everything else → `<input type=text>`.
- `chat.default_model` → a `<select>` of model configs, showing human labels (uuid in the tooltip) instead of raw uuids, with unavailable models marked.
- `qa.unlocked_shields` → no modal; the card itself is a checkbox checklist of every discovered shield, grouped by dotted-path prefix (checked = unlocked), with its own **Save shields** button.
- secret settings → read-only card, no Edit.

The modal shows the current *effective* value and its source, so an empty DB field is not mistaken for "no value". **Save is disabled until the control differs from the DB-layer baseline** (the stored value if provenance is `db`, else empty). An empty control saves `null` — i.e. "clear the DB value, fall back to env/default".

**Save flow.** `POST /settings/api/set` with `{key, value}` (value already typed: bool/number/string/list, or `null`). The endpoint calls `db.set_setting`; `UnknownSetting`, validation failures, and secret-store attempts come back as a 400 with the error message, rendered inline in the modal (with a session rollback so the failed write doesn't poison later requests). On success the response carries the key's fresh `all_settings()` row; the page swaps it into its local state and re-renders — no reload.

## Action buttons (on the `customize.dir` card)

- **Repopulate Q&A memory** → `POST /settings/api/repopulate_memory` → `seed_memory.sync_kb()`: reconciles the Q&A vector table with the merged JSONL (base + `customize.dir` overlay). Only changed rows re-embed; the result line reports unchanged/updated/embedded/deleted counts. The facts-invalidated stamp happens *inside* `sync_kb`, and only when something actually changed. Failure → 502 with the error (also logged with file:line detail for JSONL parse errors / the Ollama error for embedding failures); already-synced rows stay intact and the stale ones retry on the next press.
- **Rebuild (full)** → `POST /settings/api/rebuild_memory` → `seed_memory.rebuild_kb()`: TRUNCATE + re-embed everything — the escape hatch for genuine table corruption. A full rebuild always re-embeds, so the endpoint always calls `mark_facts_invalidated()` afterwards. Failure → 502; the table may be empty/partial, and pressing again after fixing the cause heals it.

## Settings with side effects

- **`qa.unlocked_shields`** — a shield change can stale facts already answered in a conversation. The `set` endpoint captures the prior value, and if the write actually changed it, calls `mark_facts_invalidated()`; the assistant then posts a one-time re-check-facts notice per room (comparing the stamp against markers already in the room).
- **`profile.current`** — the `set` endpoint special-cases this key to `db.set_current_profile` (mirroring the page's special-cased dropdown), so an actual switch fires both event stamps atomically and the assistant's per-room context marker announces the new profile. Switching changes identity, formatting, and calibration; it preserves room history and is not an audience boundary.
- **`customize.dir`** — changing it (or editing the overlay files) does nothing by itself; the operator must press **Repopulate Q&A memory** to reconcile, which is why the buttons live on this card and the description says so.
- **`cron.paused`** — read at the top of every scheduler tick; the `/cron` page's Pause/Resume buttons write it via `POST /cron/api/pause|resume`, so the same state is visible and editable on both pages.

## Adding a new setting

1. Add a `Setting(...)` entry to `SETTINGS` in `db/settings.py` — key, env fallback (or `None`), type, default, description, optional validator. That is the whole registration: `init_db`'s `reconcile_app_settings()` seeds the row, and the page picks it up from `all_settings()` with a type-appropriate editor automatically.
2. Read it with `db.get_setting("your.key")` wherever it applies (inside an app context).
3. Only bespoke UI (a custom control like the shield checklist or model picker, or an action button) needs a change in `webapp/settings_views.py`.

## Deliberate tradeoffs

- **Registry in code, not a DB-managed schema.** Keys, types, and validation ship with the code that consumes them; the DB row is just a value slot. No migration is needed to add a key, and metadata cannot drift (it is re-stamped on startup and on every write).
- **Whole-value writes, no history.** `app_setting` keeps only the current value (`updated_at` aside); git and backups are the history mechanism.
- **Secrets never in the DB** — env-only by construction, enforced in `set_setting`, so a backup or a DB dump can never leak them.
- **No `uuid` on `AppSetting`** — rows are addressed by `key` and never FK-referenced or deep-linked.
