# Proposal: store user configuration in Postgres

**Status:** Draft / proposal (rev. 4 — rev. 2 incorporated the code-review
findings: app-context boundary for the CLI, Flask-Admin validation,
recipients-file scoping, provider-URL caveat, schema `created_at`, unset
semantics. rev. 3 adds two build notes: pass `recipients=None` (not `[]`) when
unset to keep fail-closed; registry-owned metadata reconciled on startup to
prevent drift. rev. 4 folds in Gemini review: DB-wins precedence, env-only
secrets for now, no NOTIFY/scope column, defensive reads, atomic reconciliation,
strict bool parsing, read-only admin first.)
**Date:** 2026-06-07
**Scope:** A general mechanism for operator-set configuration, with the backup
settings (`RAINBOX_BACKUP_REPO`, `RAINBOX_BACKUP_AGE_RECIPIENT`,
`RAINBOX_BACKUP_GIT_PUSH`) as the first concrete consumer.

## Motivation

Configuration today is set through **environment variables** read at call time,
e.g. in `backup_db.py` / `db_cron.py`:

- `RAINBOX_BACKUP_REPO` — where backups are written
- `RAINBOX_BACKUP_AGE_RECIPIENT` — the age public key backups are encrypted to
- `RAINBOX_BACKUP_GIT_PUSH` — whether to commit+push each backup
- plus `LM_STUDIO_BASE_URL`, `JAN_BASE_URL`, `OLLAMA_BASE_URL`, `KOKORO_TTS_URL`,
  `SECRET_KEY`, `GIT`, `DATABASE_URL`.

Env vars have real downsides for a long-running, UI-driven, single-user app:

- **Not editable from the running app.** Changing the backup destination or
  recipient means editing a shell profile / launch environment and restarting.
  There's already a settings UI culture here (`/cron`, `/agent_models`,
  `/modelgroups`) — config should live in the same place.
- **Invisible.** You can't see the current value, when it changed, or what's
  even configurable. `os.environ` is an undocumented, unvalidated surface.
- **Inconsistent with where config already lives.** Model/provider config is
  *already* in Postgres (`model_config`, `model_config_override`, `model_group`,
  bindings — see `db_model_config.py`). The backup destination is *already*
  partly in Postgres too: the seeded "Database backup" cron job stores its
  destination in `CronJob.command`. Env vars are the odd one out.

Postgres is the natural home: it's the app's single source of truth, it's
inspectable in Flask-Admin, it survives restarts, and — relevant here — it's the
thing we already back up.

## Goals / non-goals

**Goals**
- One place to store operator-set settings, editable while the app runs.
- Typed access with defaults and validation.
- Backward compatible: existing env-var deployments keep working.
- A clear rule for secrets (what may and may not be stored).

**Non-goals**
- Per-end-user preferences / multi-tenant config. This is a **single-operator**
  app; settings are global. Do **not** add a `scope`/`owner` column now; if the
  app ever becomes multi-tenant, user context/RBAC will be a larger design and a
  scope column will be an easy migration then.
- Replacing bootstrap config that must exist *before* the DB is reachable.

## What stays an environment variable

A setting needed to *open the database* obviously can't be stored *in* the
database:

- **`DATABASE_URL`** — bootstrap; env only, always.
- **`SECRET_KEY`** — needed at app construction (`make_app`); keep in env.
- Tool path overrides (`GIT`, `PG_DUMP`, `ZSTD`, `AGE`) are deployment/runtime
  facts, not user config — leave them in env.
- **`RAINBOX_BACKUP_AGE_RECIPIENTS_FILE`** — a path to an age recipients file
  (`backup_db.resolve_recipients`). This is a deployment fact (a filesystem
  path), and the inline `backup.age_recipient` setting already accepts *multiple*
  whitespace/comma-separated public keys, which covers the multi-recipient need
  for DB-configured installs. Keep the file path env-only as an escape hatch; do
  **not** add a `backup.age_recipients_file` setting unless a concrete need
  appears. (If we ever do, store recipient *contents*, not a host path.)

Everything else (the other backup settings, the Kokoro URL) is a fair candidate
to move. **Provider base URLs are a special case — see the note in the rollout
section**, they are not a simple lift-and-shift.

## Proposed schema

A single key–value table, in the same `Mapped`/`mapped_column` +
`created_at`/`updated_at` style as `ModelConfig`. It deliberately **omits the
`uuid`** the other tables carry: rows are addressed by their unique `key`, and an
`AppSetting` is never FK-referenced or deep-linked (the reason `uuid` exists on
`ModelConfig`/`CronJob`), so a second identifier would be dead surface.

```python
class AppSetting(db.Model):
    __tablename__ = "app_setting"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(Text, unique=True, index=True)   # e.g. "backup.age_recipient"
    value: Mapped[str | None] = mapped_column(Text, default=None)     # always stored as text; NULL = unset
    value_type: Mapped[str] = mapped_column(Text, default="string")   # string|bool|int|json
    description: Mapped[str] = mapped_column(Text, default="")
    secret: Mapped[bool] = mapped_column(default=False)               # redact in UI/logs
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
```

`value_type` and `secret` are persistence-layer flags that the *registry* owns
the truth for; the UI/admin must not let them be edited freely (see UI).

Why key–value rather than a typed column per setting:

- Settings are **sparse and evolving**; a KV table means no migration per new
  knob (the app already does idempotent `ADD COLUMN IF NOT EXISTS` dances in
  `init_db` — fewer of those is good).
- It maps directly to how the user thinks about it ("these parameters in
  Postgres").
- Validation/typing/defaults live in a **code-side registry** (below), not in
  the schema, so they're easy to evolve and test.

Use **dotted keys** (`backup.repo`, `backup.age_recipient`, `backup.git_push`)
so related settings group naturally and the env-var spelling stays a presentation
detail.

### A typed registry (source of truth for keys)

Keys, types, defaults, validation, and the env var they shadow are declared in
code so the DB stays dumb and the app stays safe:

```python
@dataclass(frozen=True)
class Setting:
    key: str
    env: str | None        # legacy env var this shadows (for fallback)
    type: str              # string|bool|int|json
    default: object
    secret: bool = False
    validate: Callable[[object], None] | None = None
    description: str = ""

SETTINGS = {
    "backup.repo": Setting(
        "backup.repo", "RAINBOX_BACKUP_REPO", "string", None,
        description="Directory backups are written under."),
    "backup.age_recipient": Setting(
        "backup.age_recipient", "RAINBOX_BACKUP_AGE_RECIPIENT", "string", None,
        validate=validate_age_recipient,
        description="age public key(s) backups are encrypted to."),
    "backup.git_push": Setting(
        "backup.git_push", "RAINBOX_BACKUP_GIT_PUSH", "bool", False,
        description="Commit+push each backup into the backup-repo git repo."),
}
```

The registry is what a settings UI renders and what validation keys off; the
table just persists values.

**Metadata lives in code, not the row.** `value_type`, `secret`, and
`description` exist on `app_setting` only as a convenience for raw Flask-Admin /
ad-hoc SQL — they are a *seeded cache* of the registry, never a second source of
truth. To keep them from drifting when the registry changes:

- `get_setting()` / `all_settings()` derive type, `secret`, and `description`
  from the **registry** at read time; the row contributes only `value` (and
  `updated_at`). A key absent from the registry is ignored (or surfaced as
  "unknown setting"), not trusted.
- `init_db` **reconciles** seeded rows on startup (idempotent, like
  `seed_cron_defaults()`): re-stamp `value_type`/`secret`/`description` from the
  registry without touching `value`. Use an atomic Postgres upsert (`INSERT ...
  ON CONFLICT (key) DO UPDATE`) rather than a check-then-insert pattern, so a
  future multi-worker boot cannot race into a unique-key error. Then even a
  stale or hand-edited metadata column self-heals on next boot.

This makes the "registry is the source of truth" claim actually hold.

## Access API + precedence

A small accessor in a new `db_settings.py` (re-exported from `db`, like the other
`db_*` modules):

```python
def get_setting(key: str) -> object: ...      # typed, with precedence + default
def set_setting(key: str, value) -> None: ... # validates, coerces to text, commits
def all_settings() -> list[dict]: ...         # for the UI (secrets redacted)
```

**Precedence: DB value (if set) → env var → registry default.**

- DB is authoritative once set from the UI; the env var is a **bootstrap /
  fallback** so existing deployments and first-run/headless keep working.
- Rationale: the request is to make these editable in Postgres, so the
  UI-written value should win. The env var still seeds a fresh install and works
  before anyone opens the UI.
- An env-always-overrides model would make the UI feel broken ("I changed it and
  nothing happened"), so do not use it for these operator-editable settings.

Reads should be **uncached by default**. The backup path currently reads
`os.environ` fresh at fire time, so `get_setting()` should likewise reflect a
just-saved change without a restart. Do **not** build a Postgres
NOTIFY-invalidated cache for this slice. If high-frequency settings arrive
later, prefer a request-scoped cache (for example `flask.g`) for web requests
and one explicit read per cron/background job execution.

`get_setting()` must be defensive on read. `set_setting()` validates normal app
writes, but raw SQL or future admin mistakes can still put invalid text in
`app_setting.value`. If DB coercion/validation fails, log a warning and fall
through to env/default rather than crashing cron or the web app. Apply the same
principle to invalid env values: log and fall back to the registry default.

### Requires an app/DB context — important for standalone tools

`get_setting()` touches `db.session`, so it only works **inside a Flask app
context**. The cron scheduler already runs in one (the supervisor pushes
`app.app_context()` around `cron_tick`), so cron consumers can call
`get_setting()` directly. But `backup_db.py`'s **CLI** (`main()`) constructs no
app — it reads env/flags and calls `backup_database()` with explicit arguments.

So the boundary is: **DB settings are resolved by app-context callers (cron, the
web app), which pass explicit values into the plain functions.** `backup_database()`
keeps its current explicit `recipients` / `recipients_file` parameters and does
**no** DB access. Do *not* push `get_setting()` down into `resolve_recipients()`
— that would either break the standalone CLI or force it to bootstrap an app
context it otherwise doesn't need. (If a standalone tool ever truly needs DB
settings, give it an explicit, documented `with make_app().app_context():`
bootstrap rather than making DB access implicit.)

This creates an intentional CLI/web difference: a manual `python backup_db.py`
run ignores values edited in the UI and uses only flags/env. Document that in
`docs/backup.md` and the operator guide so it is not surprising. A future
`--use-db-settings` flag is possible, but not part of this slice; it would need
to bootstrap the app context explicitly and still keep flags as the clearest
per-run override.

### Unset semantics

"DB value if set" needs a precise definition of *set*:

- **string / json**: `NULL` **or** empty string `""` counts as **unset** → fall
  through to env, then default. (So "clear this field" in the UI and "never
  touched" behave the same — important for `backup.repo` / `backup.age_recipient`,
  where empty must mean "use fallback", not "encrypt to no one".)
- **bool / int**: parsed from the stored text, so `false` / `0` are **explicit
  values**, not unset. Only `NULL` is unset. (Otherwise you could never turn
  `backup.git_push` off via the DB once an env var enabled it.)
- **bool parsing must be strict**: accept only known truthy/falsy strings such as
  `1`/`true`/`yes`/`on` and `0`/`false`/`no`/`off` (case-insensitive). Never use
  `bool(text)`, because `bool("false")` is `True`.
- `set_setting(key, None)` (or clearing in the UI) writes `NULL`, not `""`, so
  the table stays clean; the read path treats both as unset for string types
  anyway.

## Secrets — important given the backup design

The database is exactly the asset the backup feature assumes can leak (that's why
backups are encrypted to a public key). So:

- **Never store the age private key / identity.** That would defeat the entire
  point of [docs/backup.md](../backup.md): rainbox must not hold the decryption
  key. The `backup.age_recipient` value is a **public** key — safe to store.
  Add an explicit note (and ideally a guard) so no one "helpfully" adds a
  `backup.age_identity` setting.
- For genuine secrets we may want later (API tokens, `SECRET_KEY`), a plain
  `value` column means the secret sits in cleartext in Postgres **and in every
  backup**. For this project phase, choose the simple rule: keep secrets in
  **env only**. A registry entry with `secret=True` is env-backed, redacted, and
  read-only in the UI; it does not store a DB value. Encrypt-at-rest can be a
  separate design later if there is a concrete need, but do not build key
  management for this slice.
- Do **not** store cleartext secrets in `app_setting`.
- The `secret` flag drives redaction in the UI, logs, and `all_settings()`.

## UI

Two complementary surfaces, reusing existing patterns:

- **Flask-Admin**: register `AppSetting`, but **not as a raw editable table** —
  a raw `ModelView` would let an admin write values that skip the registry's
  coercion/validation, flip `secret`, or change `value_type` into an
  inconsistent state. Either (a) a custom `ModelView` whose `on_model_change`
  routes the write through `set_setting()` (same validation/coercion path) and
  makes `key`/`value_type`/`secret` read-only, or (b) register it **read-only**
  until `/settings` exists. For phase 1, use **read-only**; it is cheaper,
  safer, and avoids fragile admin hook code.
- **A `/settings` page** (later): render the registry grouped by prefix
  (`backup.*`, `providers.*`), with typed inputs, inline validation, secrets
  redacted, and "currently from: DB / env / default" provenance shown per row —
  mirroring the `/cron` and `/modelgroups` split-pane style.

## Worked example: the backup settings

1. `init_db` seeds `app_setting` rows from the registry (idempotent; value left
   `NULL` so env/default still apply) — same shape as `seed_cron_defaults()`.
2. **The cron firing path resolves the settings and passes them as explicit
   arguments** — it already runs in an app context. In `fire_cron_job`'s backup
   branch:
   ```python
   recip = get_setting("backup.age_recipient")     # DB -> env -> default
   backup_db.backup_database(
       repo,
       recipients=_split(recip) or None,           # None (not []) when unset
   )
   if get_setting("backup.git_push"):
       backup_remote.git_push_backup(repo, dest)
   ```
   `backup_database()` / `resolve_recipients()` are **unchanged** and still
   accept explicit args; the DB read lives in the caller, not the library (see
   "Requires an app/DB context").

   **Pass `None`, never `[]`, when the recipient resolves to nothing.**
   `resolve_recipients(None)` runs its own env + `RAINBOX_BACKUP_AGE_RECIPIENTS_FILE`
   resolution and raises `NoRecipientError` if truly nothing is configured —
   that's the fail-closed guarantee. An explicit empty list would *skip* that
   fallback and could fail even when a recipients-file is set. So coerce an
   empty/`None`/whitespace setting to `None` (hence `_split(recip) or None`),
   and let the library stay the single owner of "no recipient ⇒ refuse".
3. **The standalone CLI stays env/flag-only** — it builds no app context, so it
   keeps reading `RAINBOX_BACKUP_*` / `--recipient` exactly as today. Same code,
   same behavior; only the in-app (cron/web) path gains DB-backed settings.
4. `get_setting()` itself keeps each setting's env var as the fallback layer, so
   an install that only ever set env vars sees **no behavior change**.
5. The destination has two homes today: the per-job `CronJob.command` and
   `RAINBOX_BACKUP_REPO`. Keep that order — **per-job command → `backup.repo`
   setting → env** — so a job can still override the global default.
6. The operator sets the recipient/push flag once in the UI; the nightly cron job
   reads them at fire time. No shell-profile edit, no restart.

This is a non-breaking migration: env vars become the *fallback* layer beneath a
DB value for app-context callers, while standalone tools are untouched.

## Migration / rollout

1. Add `AppSetting` + `db_settings.py` + the registry; register in Flask-Admin
   read-only. Reconcile metadata via atomic upsert on startup.
2. Wire the backup settings into the **cron** path via `get_setting` (env
   fallback); leave the CLI env/flag-only. Land with tests (live-DB,
   set/get/precedence, unset-semantics for string vs bool, strict bool parsing,
   invalid DB value falls back with a warning, validation, secret redaction,
   metadata reconciliation; tear down rows per the test-cleanup convention).
3. Update `docs/backup.md` / `docs/operator-guide.md` to call out the
   standalone CLI behavior: env/flags only, no DB settings.
4. (Later) the **Kokoro URL** moves cleanly the same way.
5. (Later) build the `/settings` page; document in `docs/operator-guide.md`.

### Provider base URLs are NOT a simple move

`LM_STUDIO_BASE_URL` / `JAN_BASE_URL` / `OLLAMA_BASE_URL` look like peers of the
above but behave differently: provider defaults are **copied into each
`model_config.arguments`** when a row is created/synced, and `prepare_llm()`
constructs the client straight from those **persisted per-row arguments**
(`llm.py`), not from the env var at call time. So putting a base URL in
`app_setting` would **not** change existing model rows.

A real migration must pick one model and state it:

- **Runtime override**: `prepare_llm` reads `get_setting("providers.<id>.base_url")`
  and overrides the stored `base_url`/`api_base` per call (settings win over
  persisted args). Most "edit once, applies everywhere" — but changes the
  meaning of the stored arguments.
- **Force-sync on change**: writing the setting triggers the existing
  `sync_models_from_providers(force_update_arguments=True)` flow to rewrite rows.
- **Seed-only**: the setting just seeds the default for *new* rows; existing rows
  keep their saved URL (least surprising, least useful).

This deserves its own short proposal; it is explicitly **out of scope** here.

## Decisions

- **Precedence**: DB-wins-over-env for operator-editable app-context settings.
  Env remains the fallback/bootstrap layer.
- **Secrets**: env-only for this phase. `secret=True` means redacted/read-only
  and not persisted in `app_setting.value`.
- **Caching**: no NOTIFY-invalidated cache. Use uncached reads for this slice;
  if needed later, add request-scoped caching for high-frequency web reads.
- **Scope column**: no scope/owner column now. Settings are global until a real
  multi-user architecture exists.

Still deferred: provider base URLs need their own short proposal because
existing model rows persist copied endpoint arguments.

## See also

- `docs/backup.md` — the consumer; the age-recipient secret rule comes from here.
- `db_model_config.py` — existing in-Postgres config (the pattern to extend).
- `db.py` (`make_app`, `init_db`, `seed_*`) — where bootstrap config and seeding
  live.
