# Database Backup

## Purpose

Back up the rainbox Postgres database to a compressed, **public-key-encrypted**,
timestamped file — on demand from the CLI or on a schedule via the cron system.
This is the "System → Backup" use case (`docs/usecases.md`).

A backup is a `pg_dump` plain-SQL dump, compressed with **zstd**, then encrypted
to a recipient's **public key** with [`age`](https://age-encryption.org):

```text
pg_dump <dsn> | zstd | age -r <recipient>   ->   FILE.zstd.age
```

### Threat model — why encrypt to a public key

rainbox holds only the **public** key, so it can *write* backups but cannot
*read* them. The matching **private** key (the age "identity") never touches the
machine — you keep it offline and only bring it out to restore. So if the host
(or the backup directory it writes to) is exposed on the public web, an attacker
gets ciphertext they cannot decrypt, and a compromised rainbox cannot leak a
decryption key it never had.

Encryption is **mandatory and fail-closed**: if no recipient public key is
configured, a backup refuses to run rather than write plaintext, and if
encryption errors it fails loudly — a backup is never silently written
unencrypted.

Code: `backup/dump.py` (the dump + encryption) and `db/cron.py` (the `backup` cron
action + the seeded daily job).

## One-time key setup (do this offline)

Generate a keypair on a trusted machine — ideally **not** the one running
rainbox:

```bash
age-keygen -o backup-identity.txt
# Public key: age1ql3z7h9...        <- give THIS to rainbox
```

- `backup-identity.txt` contains the **private** key (`AGE-SECRET-KEY-1…`). Keep
  it offline and safe (password manager, hardware token, printed paper). **If
  you lose it, your backups are unrecoverable.**
- The `age1…` string is the **public** key (the "recipient"). It is the only
  thing rainbox needs.

You can configure more than one recipient (e.g. a primary key plus a break-glass
key) so any one of their private keys can restore — see Configuration.

## Quickstart

Example setup: backups go to the git repo
`/Users/Username/git/rainbox-backup`, which pushes to
`git@github.com:Username/rainbox-backup.git`.

```bash
# Prereqs (once): age + a keypair (see "One-time key setup"), and the backup
# repo already cloned with its GitHub remote. `brew install age` if needed.

export RAINBOX_BACKUP_REPO=/Users/Username/git/rainbox-backup
export RAINBOX_BACKUP_AGE_RECIPIENT=age1...      # your age public key
export RAINBOX_BACKUP_GIT_PUSH=1                 # commit + push to GitHub

# one-off backup (writes the encrypted file, commits it, pushes):
venv/bin/python -m backup.dump

# or scheduled: set those three vars in the app's environment, run
# `python main.py`, then enable /cron -> System -> "Database backup".
```

Restore (on a machine that has the offline age private key):

```bash
git clone git@github.com:Username/rainbox-backup.git
age -d -i backup-identity.txt \
  rainbox-backup/rainbox_database/<yyyy>-<mm>-xx/<file>.zstd.age \
  | zstd -dc | psql postgresql://localhost/rainbox_restore
```

The sections below explain each piece in detail.

## File layout

Backups are written under a **backup-repo** directory you choose:

```text
<repo>/rainbox_database/<yyyy>-<mm>-xx/<yyyy>-<mm>-<dd>T<hh>-<mm>-<ss>Z.zstd.age
```

Example — a backup taken 2026-01-01 22:39:06 UTC:

```text
<repo>/rainbox_database/2026-01-xx/2026-01-01T22-39-06Z.zstd.age
```

- The `<yyyy>-<mm>-xx` directory buckets a whole month's backups together.
- The timestamp is **UTC** (the trailing `Z`).
- Month/day/time are zero-padded (January is `01`).
- `:` is replaced with `-` because macOS paths can't contain a colon.
- `.zstd.age` = zstd-compressed, then age-encrypted.

## Configuration

> **CLI vs. scheduled — where settings come from.** The standalone
> `python -m backup.dump` CLI is intentionally **flags/env only**: it builds no app
> context, so it does **not** read DB-backed app settings. The **scheduled cron**
> backup runs inside the app and resolves the backup settings from Postgres
> (`app_setting`) first, falling back to env, then the default — see
> "Operator settings (app_setting)" below. So a value you edit in the UI affects
> nightly backups, **not** a manual `python -m backup.dump` run.

**Destination** (the backup-repo directory):

| Context  | Where the destination comes from                                               |
| -------- | ------------------------------------------------------------------------------ |
| CLI      | the positional argument, else `RAINBOX_BACKUP_REPO`                             |
| Cron job | the job's **command** field, else the `backup.repo` setting (DB → env → none)   |

**Recipient public key(s)** — required, resolved at run time:

| Source                             | Meaning                                              |
| ---------------------------------- | ---------------------------------------------------- |
| `--recipient age1…` (CLI, repeats) | inline recipient(s)                                  |
| `backup.age_recipient` setting (cron) | DB value, else `RAINBOX_BACKUP_AGE_RECIPIENT`     |
| `RAINBOX_BACKUP_AGE_RECIPIENT`     | one or more `age1…` keys, whitespace/comma separated |
| `RAINBOX_BACKUP_AGE_RECIPIENTS_FILE` / `--recipients-file` | an age recipients file (one per line) — **env/CLI only** |

```bash
export RAINBOX_BACKUP_AGE_RECIPIENT=age1ql3z7h9...
```

### Operator settings (app_setting)

For the **scheduled** backup you can set these in Postgres instead of the
environment, and edit them while the app runs (the nightly job reads them at fire
time — no restart):

| Setting               | Type   | Shadows env var                 |
| --------------------- | ------ | ------------------------------- |
| `backup.repo`         | string | `RAINBOX_BACKUP_REPO`           |
| `backup.age_recipient`| string | `RAINBOX_BACKUP_AGE_RECIPIENT`  |
| `backup.git_push`     | bool   | `RAINBOX_BACKUP_GIT_PUSH`       |

Resolution is **DB value → env var → default**; an empty string is treated as
unset (falls through). Edit them on the **`/settings`** page (typed inputs,
shows whether each value comes from DB/env/default, with a Clear button to drop
the DB value and fall back); they're also visible read-only in Flask-Admin under
**Config → App settings**. `RAINBOX_BACKUP_AGE_RECIPIENTS_FILE` has no setting —
it's a host-path escape hatch, env/CLI only. See
`docs/proposals/2026-06-07-user-configuration-in-postgres.md`.

**Database** to dump comes from `DATABASE_URL` (default
`postgresql+psycopg://localhost/rainbox_production`), via `db.psycopg_dsn()`.

**External tools** (`pg_dump`, `zstd`, `age`) are found on `PATH`. If any lives
somewhere non-standard (e.g. the cron environment's `PATH` doesn't include
Homebrew), point at it explicitly:

```bash
export PG_DUMP=/opt/homebrew/opt/postgresql@16/bin/pg_dump
export ZSTD=/opt/homebrew/bin/zstd
export AGE=/opt/homebrew/bin/age
```

`age` is required — install it with `brew install age` if missing.

## Manual backup (CLI)

```bash
# Destination + recipient as arguments:
venv/bin/python -m backup.dump /path/to/backup-repo -r age1ql3z7h9...

# …or via env vars:
export RAINBOX_BACKUP_REPO=/path/to/backup-repo
export RAINBOX_BACKUP_AGE_RECIPIENT=age1ql3z7h9...
venv/bin/python -m backup.dump
```

On success it prints the path of the file it wrote. Tune compression with
`--zstd-level N` (default 19; lower is faster and larger).

The CLI reads its config from **flags and env vars only** — it does not consult
the `app_setting` DB settings (it builds no app context). Settings edited in the
UI drive the scheduled cron backup, not a manual `python -m backup.dump` run; pass the
flags/env explicitly here.

The dump streams into a temporary `.part` file in the destination directory and
is **atomically renamed** into place only when the whole pipeline succeeds, so an
interrupted run never leaves a truncated file that looks like a finished backup.

## Scheduled backup (cron)

Backup is a first-class cron action type (`backup`), alongside `message` and
`command`. It runs **in-process** in the supervisor — unlike a `command` job,
which goes through the workspace-shell agent whose allowlist can't run
`pg_dump`/`zstd`/`age` or write files.

A disabled **"Database backup"** job is seeded under a **System** folder
(`seed_cron_defaults()` in `db/cron.py`), scheduled daily at **03:30 local
time** (`30 3 * * *`). To turn it on:

1. Configure the recipient: set the `backup.age_recipient` app setting (your
   `age1…` public key) or the `RAINBOX_BACKUP_AGE_RECIPIENT` env var.
2. Run the app: `python main.py` (the cron scheduler lives in the supervisor
   loop, so backups only fire while the app is running).
3. Open `/cron` → **System** → **Database backup**.
4. Set the destination: put a directory path in the job's **command** field, or
   set the `backup.repo` setting / `RAINBOX_BACKUP_REPO`.
5. Toggle the job **Active**, and optionally click **Run now** to confirm it
   works.

Every fire — scheduled or "Run now" — records a `cron_run` row and posts a line
to the **`cron`** chatroom:

```text
▶ backed up "Database backup" (scheduled) → /path/.../2026-01-01T03-30-00Z.zstd.age (822382 bytes)
```

On failure it posts an error line instead (and does not stop the scheduler),
e.g. with no destination or no recipient configured:

```text
✖ "Database backup" failed to fire: no age recipient configured; set RAINBOX_BACKUP_AGE_RECIPIENT …
```

## Remote upload (git push)

The backup-repo directory can itself be a **git repo with a remote**, in which
case each new backup is committed and pushed off-machine. Because the files are
public-key-encrypted ciphertext, pushing them to untrusted storage (e.g. a
private *or* public GitHub repo) is safe.

This is opt-in. Enable it with `RAINBOX_BACKUP_GIT_PUSH=1` (or `--git-push` on
the CLI):

```bash
# one-time: make the backup-repo a git repo with a remote
cd /path/to/backup-repo
git init && git remote add origin git@github.com:you/rainbox-backup.git

# then, per backup:
export RAINBOX_BACKUP_GIT_PUSH=1
venv/bin/python -m backup.dump /path/to/backup-repo -r age1ql3z7h9...
#   …writes the file, then: git add <file> && git commit && git push
```

It stages **only the new backup file** (unrelated working-tree changes are left
alone), commits it as `backup <relpath>`, and pushes the current branch to
`origin`. Code: `backup/remote.py`.

- The push uses whatever credentials the running process has. For an
  `git@github.com:` remote that means an SSH key/agent reachable by the app (and
  by the cron supervisor, if you schedule backups).
- A push failure does **not** discard the local backup: the file is already
  written, and the failure is logged / posted to the `cron` room as a separate
  `✖ upload failed:` line.
- Restoring is unchanged — pull/clone the backup repo, then decrypt as below.

> Caveat: git keeps every pushed backup forever, so the repo grows without
> bound. Prune old backups (rewrite history) or use a retention policy if size
> becomes a problem; git is convenient transport, not a deduplicating store.

## Restore from a backup

You need the **private identity** file (`backup-identity.txt`) you generated.
Decrypt → decompress → load into `psql`. Do this on a trusted machine where the
identity lives.

**Into a fresh, empty database** (recommended — avoids "already exists"
errors):

```bash
# 1. Create an empty target database (example name shown):
createdb rainbox_restore

# 2. Decrypt + decompress + load:
age -d -i backup-identity.txt \
  /path/.../rainbox_database/2026-01-xx/2026-01-01T03-30-00Z.zstd.age \
  | zstd -dc \
  | psql postgresql://localhost/rainbox_restore

# 3. Point the app at it to verify, then promote when satisfied:
DATABASE_URL=postgresql+psycopg://localhost/rainbox_restore python main.py
```

**Overwrite the live `rainbox_production` database** (destructive — drops and
recreates it; stop the app first):

```bash
dropdb rainbox_production && createdb rainbox_production
age -d -i backup-identity.txt FILE.zstd.age | zstd -dc | psql postgresql://localhost/rainbox_production
```

> A plain-SQL dump restored on top of an existing populated database will emit
> "relation already exists" / duplicate-key errors. Restore into an empty
> database.

After restoring, the app's `init_db()` runs its idempotent migrations on the
next start, so an older dump is brought up to the current schema automatically.

## Verify a backup

Confirm a file decrypts and is a real dump (needs the identity; no full restore):

```bash
age -d -i backup-identity.txt FILE.zstd.age | zstd -dc | head -c 200
# -> should start with: "-- PostgreSQL database dump"
```

Confirm it's an age file at all (no key needed):

```bash
head -c 21 FILE.zstd.age   # -> "age-encryption.org/v1"
```

## Troubleshooting

### "no age recipient configured"

No public key is set. Set `RAINBOX_BACKUP_AGE_RECIPIENT` (or
`RAINBOX_BACKUP_AGE_RECIPIENTS_FILE`, or pass `--recipient`). Backups are
encrypt-only and will not run without one.

### "no backup destination"

The cron job's **command** field is empty and `RAINBOX_BACKUP_REPO` is unset.
Set one of them.

### `'age' not found on PATH` (or `pg_dump`/`zstd`)

The tool isn't on the running process's `PATH`. `brew install age`, and/or set
`AGE` / `PG_DUMP` / `ZSTD` to the full binary path. This is common when the app
is launched from an environment that doesn't inherit your interactive shell's
`PATH`.

### `pg_dump failed (exit N): …`

The error text from `pg_dump` is included in the message and the `cron` event.
Usual causes: the database is unreachable, `DATABASE_URL` is wrong, or the role
lacks read access. Verify the connection the app uses:

```bash
psql "$(python -c 'import db; print(db.psycopg_dsn())')" -c '\dt'
```

### Restore says "no identity matched any of the recipients"

The identity file you passed to `age -d -i` doesn't match any recipient the file
was encrypted to. Use the private key whose public key was configured when the
backup was taken. (This is also the expected failure if you no longer have the
key — the backups are, by design, unrecoverable without it.)

### The scheduled job never fires

- The app must be running (`python main.py`) — the scheduler is part of the
  supervisor loop, not a separate daemon.
- The job and **every ancestor folder** must be Active (folder-disable
  cascades). Check the System folder too.
- "Run now" on `/cron` fires immediately and surfaces any error in the `cron`
  chatroom — use it to isolate scheduling vs. backup problems.

### Backup is slow / blocks other cron jobs

The dump runs synchronously on the supervisor thread (fine for a local
single-user database). A multi-minute dump would delay other cron ticks and
agent routing while it runs. If your database grows large, move the dump to a
worker; see the note in `fire_cron_job` (`db/cron.py`).

## Limitations

- **Remote upload is git-only.** Off-machine upload is done by committing into a
  git repo and pushing (see Remote upload). There's no direct rsync/S3/rclone
  target; point git at whatever remote you like, or add another uploader.
- **Synchronous firing.** See the slowness note above — and note the cron push
  also runs synchronously on the supervisor thread (network I/O, bounded by a
  180s git timeout).
- **`/cron` create/edit-action UI.** The New-job and Edit-action forms only
  offer Message/Command. The seeded backup job is fully manageable from the UI
  (enable/disable, reschedule, rename, set the destination via its command
  field, Run now, delete), but creating a brand-new backup job from scratch via
  the UI isn't wired up yet — add one programmatically (a `CronJob` with
  `action_type="backup"`) if you need a second one.

## See also

- `docs/usecases.md` — the "System → Backup" use case.
- `docs/cron-design.md` — the cron scheduler/firing this hooks into.
- `docs/operator-guide.md` — running the app, general troubleshooting.
- [age-encryption.org](https://age-encryption.org) — the encryption tool/format.
- Tests: `backup/test_dump.py` (the dump + encryption), `db/test_cron_backup.py` (the
  cron action).
