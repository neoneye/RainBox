# Database Backup

## Purpose

Back up the rainbox Postgres database to a compressed, **public-key-encrypted**,
timestamped file — on demand from the CLI or on a schedule via the cron system.

A backup is a `pg_dump` **directory-format** dump, stored **deduplicated per
file**: each file of the dump (one per table's data, plus `toc.dat` with the
schema) is compressed with **zstd** and encrypted to a recipient's **public
key** with [`age`](https://age-encryption.org) — but only when no earlier
backup already stored that exact content. A small encrypted **manifest** per
backup names, for every dump file, the part that holds it:

```text
pg_dump --format=directory <dsn>              ->  toc.dat, 4872.dat, 4874.dat, …  (plaintext, temp dir)
each file whose SHA-256 isn't stored yet:
    zstd | age -r <recipient>                 ->  <ts>Z/<file>.zstd.age             (a "part")
{file -> part, sha256, bytes} as JSON | age   ->  <ts>Z.manifest.age                (one per backup)
```

Most of the database changes rarely (the diary is updated about once a month),
so a typical night stores only the few tables that changed — chat, the
assistant and model-call logs, cron runs — plus `toc.dat` when the schema
changed. A month's backups cost a few full copies of the busy tables instead of
thirty copies of everything. The dump of an unchanged table is byte-identical
from one night to the next (`pg_dump` writes rows in physical order and the
directory format has no timestamps), which is what the deduplication relies on;
a table that merely *looks* changed costs one extra part, never a wrong restore.

**Derived tables are left out.** The dump keeps the schema of the tables in
`DERIVED_TABLES` (`backup/dump.py`) but not their rows: `diary_embedding`,
`memory_embedding` and `diary_word`. The two embedding tables are vector caches
computed from rows the dump does keep (diary passages, memory claims), and
vectors barely compress — the diary embeddings alone were about 90% of a full
dump and pushed every file past GitHub's 100 MiB limit. `diary_word` is the
diary's word index, which Diary sync deletes and rebuilds on every run, so it
would also look changed every night. After a restore all three are rebuilt; see
[Restore from a backup](#restore-from-a-backup).

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

A backup contains the **entire database** — chat history, memory claims, and
the model API keys stored in `model_config` — so the recipient keys guard all
of it. This is also why secret-flagged settings are **env-only** and can never
be stored in `app_setting` (`db/settings.py:set_setting` enforces it): a secret
in the DB would land in cleartext inside every dump. Chat-bridge bot tokens
(`bridge_credential`) are the exception that keeps the rule's point: they
are stored sealed (AES-GCM under a key derived from `RAINBOX_CREDENTIAL_KEY`
in `.env`), so a dump carries only ciphertext — and a restore needs that
`.env` line, or the tokens must be pasted again on `/bridges`.

**What deduplication reveals.** Contents stay encrypted, but anyone who can
read the backup repo sees which dump files changed on which night and how big
each part is — table-level activity and size, not data. The SHA-256 of each
plaintext file is kept only in two places: inside the encrypted manifest, and
in the **local index** on the host (`.git/rainbox-backup-index.json`, never
committed — see File layout). A plaintext hash in the repo would let a reader
confirm a guess about a small table's exact contents. While a backup runs,
`pg_dump` writes the plaintext dump into a private (`0700`) temporary directory
on the host, which is deleted when the run ends; the host already holds the
database, so this widens nothing beyond that window.

### Limits of the threat model — the control plane

The encrypt-only design holds only as long as the *recipient configuration* is
trustworthy, and today it is not protected: the web app has no authentication,
so any process that can reach the localhost HTTP port can change
`backup.age_recipient` and `backup.repo` via `POST /config/api/set` (the DB
value **overrides** the env var), then fire the backup job via the cron API —
producing a dump of the whole database encrypted to a key *they* control,
optionally pushed off-machine if `backup.git_push` is on. The destination has
no approved-roots check. This is Finding 3 of
`notes/proposals/2026-06-25-security-review-mitigations.md` (status: open); the
planned fixes are an operator-auth boundary, elevated confirmation + audit for
backup-setting changes, and approved destination roots.

Until that lands: keep the app bound to `127.0.0.1` and treat any
local-machine compromise as full control-plane compromise. The "Verify a
backup" check below doubles as tamper detection — a backup that no longer
decrypts with **your** identity means the recipient was changed under you.

Code: `backup/dump.py` (the dump + CLI), `backup/store.py` (deduplicated parts,
manifest, local index), `backup/restore.py` (reassembling a dump from a
manifest), `backup/pipeline.py` (the shell-less `zstd`/`age` pipelines),
`backup/remote.py` (git upload), and `db/cron.py` (the `backup` cron action +
the seeded daily job).

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

# one-off backup (writes the new parts + manifest, commits them, pushes):
venv/bin/python -m backup.dump

# or scheduled: set those three vars in the app's environment, run
# `python main.py`, then enable /cron -> System -> "Database backup".
```

Restore (on a machine that has the offline age private key):

```bash
git clone git@github.com:Username/rainbox-backup.git
venv/bin/python -m backup.restore \
  rainbox-backup/rainbox_database/<yyyy>-<mm>-xx/<ts>Z.manifest.age \
  -i backup-identity.txt -o restore-dump
createdb rainbox_restore && pg_restore -d rainbox_restore restore-dump
rm -rf restore-dump          # plaintext
```

The sections below explain each piece in detail.

## File layout

Backups are written under a **backup-repo** directory you choose. Each backup
writes one manifest, plus a directory holding the parts it was first to store:

```text
<repo>/rainbox_database/<yyyy>-<mm>-xx/<yyyy>-<mm>-<dd>T<hh>-<mm>-<ss>Z.manifest.age
<repo>/rainbox_database/<yyyy>-<mm>-xx/<yyyy>-<mm>-<dd>T<hh>-<mm>-<ss>Z/<dump file>.zstd.age
```

Example — a backup taken 1999-12-31 03:30:00 UTC that found two dump files
changed:

```text
<repo>/rainbox_database/1999-12-xx/1999-12-31T03-30-00Z.manifest.age
<repo>/rainbox_database/1999-12-xx/1999-12-31T03-30-00Z/toc.dat.zstd.age
<repo>/rainbox_database/1999-12-xx/1999-12-31T03-30-00Z/4893.dat.zstd.age
```

- The `<yyyy>-<mm>-xx` directory buckets a whole month's backups together.
- The timestamp is **UTC** (the trailing `Z`).
- Month/day/time are zero-padded (January is `01`).
- `:` is replaced with `-` because macOS paths can't contain a colon.
- `.zstd.age` = zstd-compressed, then age-encrypted; the manifest is small JSON,
  age-encrypted only.
- A part's name is the dump file it holds (`toc.dat`, or `<dump id>.dat` for a
  table's rows). Dump ids can shift when the schema changes; the manifest is
  what maps them, so a part stored under one id is reused under another.
- Parts are **shared**: a manifest usually names parts from many earlier
  backups. Never delete a backup's part directory by hand — later manifests may
  still need it. Prune by deleting manifests first, then only the parts no
  remaining manifest names.

The manifest, decrypted, is:

```json
{
  "format": "rainbox-backup-manifest/1",
  "created_at": "1999-12-31T03:30:00Z",
  "files": [
    {"name": "4872.dat", "part": "rainbox_database/1999-12-xx/1999-12-01T03-30-00Z/4872.dat.zstd.age",
     "sha256": "…", "bytes": 1234},
    {"name": "toc.dat", "part": "rainbox_database/1999-12-xx/1999-12-31T03-30-00Z/toc.dat.zstd.age",
     "sha256": "…", "bytes": 567}
  ]
}
```

`files` lists every file of the dump, sorted by name; `part` is relative to the
backup repo; `sha256` and `bytes` describe the plaintext, so a restore verifies
each file it reassembles.

**The local index** — which plaintext SHA-256 is already stored in which part —
lives at `<repo>/.git/rainbox-backup-index.json` (or
`<repo>/.rainbox-backup-index.json` when the backup repo is not a git repo). It
never leaves the host: git never pushes `.git/`, and the upload stages only
the files a backup wrote. A part is reused only while its file still exists
and — in a git repo — is committed, so resetting or pruning the repo never
yields a manifest naming a part that isn't there. Deleting the index costs one
full-size backup, nothing else.

**Older single-file backups** (`<ts>Z.zstd.age` next to the manifests, from
before deduplication) are complete plain-SQL dumps; they stay where they are and
restore as described under [Restore from a backup](#restore-from-a-backup).

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

The `backup.age_recipient` DB setting validates that every token starts with
`age1…`. SSH recipients (`ssh-ed25519 AAAA…`) contain a space and can't be
expressed inline — use the env-only recipients file for those.

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
unset (falls through). Edit them on the **`/config`** page (typed inputs,
shows whether each value comes from DB/env/default, with a Clear button to drop
the DB value and fall back); they're also visible read-only in Flask-Admin under
**Config → App settings**. `RAINBOX_BACKUP_AGE_RECIPIENTS_FILE` has no setting —
it's a host-path escape hatch, env/CLI only. See
`notes/proposals/2026-06-07-user-configuration-in-postgres.md`.

**Database** to dump comes from `DATABASE_URL` (default
`postgresql+psycopg://localhost/rainbox_production`), via `db.psycopg_dsn()`.

**External tools** (`pg_dump`, `zstd`, `age`) are found on `PATH`. If any lives
somewhere non-standard (e.g. the cron environment's `PATH` doesn't include
Homebrew), point at it explicitly:

```bash
export PG_DUMP=/opt/homebrew/opt/postgresql@17/bin/pg_dump
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

On success it prints the manifest's path. Tune compression with
`--zstd-level N` (default 19; lower is faster and larger); it applies to the
parts written by that run.

The CLI reads its config from **flags and env vars only** — it does not consult
the `app_setting` DB settings (it builds no app context). Settings edited in the
UI drive the scheduled cron backup, not a manual `python -m backup.dump` run; pass the
flags/env explicitly here.

Every part and the manifest stream into a temporary `.part` file next to their
destination and are **atomically renamed** into place when their pipeline
succeeds. The manifest is written last, so a manifest exists only once every
part it names is in place; a run that fails removes the parts it wrote and
leaves no manifest, so an interrupted run never looks like a finished backup.
The local index is updated only after the manifest is in place.

## Scheduled backup (cron)

Backup is a first-class cron action type (`backup`), alongside `message` and
`command`. It runs **in-process** in the supervisor — unlike a `command` job,
which goes through the workspace-shell agent whose allowlist can't run
`pg_dump`/`zstd`/`age` or write files.

A disabled **"Database backup"** job is seeded under a **System** folder
(`seed_cron_defaults()` in `db/cron.py`), scheduled daily at **03:30 local
time** (`30 3 * * *`). (The same folder also seeds a "Memory embedding sync"
job at 03:15 — an unrelated in-process maintenance sibling.) To turn the
backup on:

1. Configure the recipient: set the `backup.age_recipient` app setting (your
   `age1…` public key) or the `RAINBOX_BACKUP_AGE_RECIPIENT` env var.
2. Run the app: `python main.py` (the cron scheduler lives in the supervisor
   loop, so backups only fire while the app is running).
3. Open `/cron` → **System** → **Database backup**.
4. Set the destination: put a directory path in the job's **command** field, or
   set the `backup.repo` setting / `RAINBOX_BACKUP_REPO`.
5. Click **Run debug** first if you like — a dry-run that resolves and reports
   the destination and recipient count without dumping anything, so you can
   check the wiring risk-free.
6. Toggle the job **Active**, and optionally click **Run now** to confirm it
   works end-to-end.

Every fire — scheduled, "Run now", or "Run debug" — records a `cron_run` row
(status `ok`/`error`, shown in the `/cron` health column) and posts a line to
the **`cron`** chatroom:

```text
▶ backed up "Database backup" (scheduled) → /path/.../1999-12-31T03-30-00Z.manifest.age (2 new parts, 60 reused; 3912345 bytes written)
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
#   …writes the parts + manifest, then: git add <them> && git commit && git push
```

It stages **only the files this backup wrote** — its new parts and its
manifest (unrelated working-tree changes are left alone) — commits them as one
`backup <manifest relpath>`, and pushes the current branch to `origin`. Code:
`backup/remote.py`.

- The push uses whatever credentials the running process has. For an
  `git@github.com:` remote that means an SSH key/agent reachable by the app (and
  by the cron supervisor, if you schedule backups).
- A push failure does **not** discard the local backup: the file is already
  written. A cron fire posts the push result to the `cron` room as its own line
  (`↑ pushed …` on success, `✖ upload failed: …` on failure) without failing
  the fire; the CLI logs the error and exits non-zero so a caller notices.
- A file over GitHub's per-file limit (`MAX_FILE_BYTES`, 100 MiB) is **never
  committed**, and neither is the rest of that backup: the upload fails with
  `✖ upload failed: … over the remote's … file limit; not committed`, and the
  files stay on disk as a local-only backup. Committing it would block this push
  and every later one, because an unpushed commit with an oversize file can
  never reach GitHub. Each part holds one table, so only a single table over
  100 MiB compressed hits this.
- Because a part is reused only once it is committed, a backup that couldn't be
  committed is not built upon: the next run stores those tables again.
- If the backup repo is a git repo but pushing is off, nothing gets
  committed, so nothing is ever reused and every backup is full-size.
- Restoring — pull/clone the backup repo, then reassemble as below.

> Caveat: git keeps every pushed backup forever, so the repo still grows — by
> the parts each backup adds. Prune old backups (rewrite history, keeping every
> part a remaining manifest names) or use a retention policy if size becomes a
> problem.

## Restore from a backup

You need the **private identity** file (`backup-identity.txt`) you generated
and a checkout of the backup repo (`git clone`/`git pull`). Do this on a trusted
machine where the identity lives, from `source/` of a rainbox checkout.

A restore has two steps: `backup.restore` decrypts the manifest, decrypts and
decompresses every part it names back into a `pg_dump` directory, and checks
each file's SHA-256 and size against the manifest (it stops on any missing part
or mismatch); then `pg_restore` loads that directory, putting the rows in before
the constraints just like a single-file dump.

**Into a fresh, empty database** (recommended — avoids "already exists"
errors):

```bash
# 1. Reassemble the dump (the output directory must not exist yet):
venv/bin/python -m backup.restore \
  /path/to/backup-repo/rainbox_database/1999-12-xx/1999-12-31T03-30-00Z.manifest.age \
  -i backup-identity.txt -o restore-dump

# 2. Create an empty target database and load it:
createdb rainbox_restore
pg_restore -d rainbox_restore restore-dump
rm -rf restore-dump          # it is the plaintext database

# 3. Point the app at it to verify, then promote when satisfied:
DATABASE_URL=postgresql+psycopg://localhost/rainbox_restore python main.py
```

`backup.restore` finds the parts relative to the backup repo that holds the
manifest (`<repo>/rainbox_database/<bucket>/<manifest>`); pass `--repo DIR` if
the manifest was copied elsewhere.

**Overwrite the live `rainbox_production` database** (destructive — drops and
recreates it; stop the app first):

```bash
dropdb rainbox_production && createdb rainbox_production
pg_restore -d rainbox_production restore-dump
```

> Restoring on top of an existing populated database emits "relation already
> exists" / duplicate-key errors. Restore into an empty database.

**An older single-file backup** (`<ts>Z.zstd.age`) is one plain-SQL dump:

```bash
age -d -i backup-identity.txt FILE.zstd.age | zstd -dc | psql postgresql://localhost/rainbox_restore
```

After restoring, the app's `init_db()` runs its idempotent migrations on the
next start, so an older dump is brought up to the current schema automatically.

The derived tables come back empty and refill on their own: the nightly
"Diary sync" job rebuilds the word index and resumes every missing diary vector,
and "Memory embedding sync" backfills memory-claim vectors (fire either from
`/cron` to do it now).
Until then diary search falls back to its lexical routes. If the embedding
model changed since the backup, Diary sync reports `embedder model changed`;
run `embed` by hand then:

```bash
python -m tools.diary --database-url postgresql+psycopg://localhost/rainbox_restore --production embed --source <name>
```

## Verify a backup

Confirm a backup reassembles and is a real dump (needs the identity; no
database restore). Step 1 of the restore already verifies every file against
the manifest; `pg_restore --list` then reads the reassembled table of contents:

```bash
venv/bin/python -m backup.restore MANIFEST.manifest.age -i backup-identity.txt -o check-dump
pg_restore --list check-dump | head     # -> ";" header lines, then one line per object
rm -rf check-dump
```

Confirm a file is an age file at all (no key needed):

```bash
head -c 21 FILE.manifest.age   # -> "age-encryption.org/v1"
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
- Check the global pause: while the `cron.paused` setting is on, nothing fires
  at all (the job's "next run" column shows `paused`).
- "Run debug" on `/cron` dry-runs the job — it resolves and reports the
  destination and recipients without dumping, isolating configuration problems.
- "Run now" fires immediately and surfaces any error in the `cron` chatroom —
  use it to isolate scheduling vs. backup problems.

### Backup is slow / blocks other cron jobs

The dump runs synchronously on the supervisor thread (fine for a local
single-user database). A multi-minute dump would delay other cron ticks and
agent routing while it runs. If your database grows large, move the dump to a
worker; see the note in `fire_cron_job` (`db/cron.py`).

## Limitations

- **No control-plane authentication.** The settings and cron HTTP APIs that
  configure and fire backups are unauthenticated, so the recipient and
  destination can be redirected by any local caller — see "Limits of the
  threat model" above and the security-review mitigation plan.
- **No prune tool.** Parts are shared between backups, so pruning by hand
  must keep every part a remaining manifest names (see File layout).
- **Whole tables, not rows.** A table that gains one row is stored again in
  full that night; deduplication is per dump file, so the append-only logs
  (`assistant_step`, `llm_call`) are the bulk of a quiet night.
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

- `notes/cron-design.md` — the cron scheduler/firing this hooks into.
- `notes/operator-guide.md` — running the app, general troubleshooting.
- `notes/proposals/2026-06-25-security-review-mitigations.md` — the open
  control-plane findings and the plan to close them (auth, audited backup
  settings, approved destination roots).
- `notes/proposals/2026-06-07-user-configuration-in-postgres.md` — the
  `app_setting` design the backup settings live in.
- [age-encryption.org](https://age-encryption.org) — the encryption tool/format.
- Tests: `backup/test_store.py` (deduplication, manifest, local index),
  `backup/test_dump.py` (the dump end to end, through restore),
  `backup/test_remote.py` (the git upload), `db/test_cron_backup.py` (the cron
  action).
