"""Back up the rainbox Postgres database to a zstd-compressed, public-key-encrypted dump.

Implements the "System -> Backup" use case (docs/usecases.md): dump postgres to
zstd, encrypted to a recipient's *public* key with `age`. The pipeline is:

    pg_dump <dsn> | zstd | age -r <recipient>   ->   FILE.zstd.age

Encryption is **mandatory and fail-closed**: a backup is only ever written
encrypted. rainbox holds only the recipient's public key, so a compromised host
can produce backups but cannot read them — the matching age *identity* (private
key) stays offline and is needed to restore:

    age -d -i identity.txt FILE.zstd.age | zstd -dc | psql <dsn>

Generate a keypair once, offline, with `age-keygen -o identity.txt`; give
rainbox only the printed `age1…` public key (via RAINBOX_BACKUP_AGE_RECIPIENT or
--recipient) and keep identity.txt off the machine.

Backups are laid out under a backup-repo directory as:

    <repo>/rainbox_database/<yyyy>-<mm>-xx/<yyyy>-<mm>-<dd>T<hh>-<mm>-<ss>Z.zstd.age

e.g. a backup taken 2026-01-01 22:39:06 UTC lands at:

    <repo>/rainbox_database/2026-01-xx/2026-01-01T22-39-06Z.zstd.age

The month-bucket directory keeps a month's worth of backups together. The
timestamp is UTC (the trailing `Z`); `:` is replaced with `-` because it is not
allowed in macOS paths.

Usage:
    python backup_db.py <backup-repo> -r age1...   # or set the env vars below
"""
import argparse
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from db.models import psycopg_dsn

logger = logging.getLogger(__name__)

DB_SUBDIR = "rainbox_database"
BACKUP_SUFFIX = ".zstd.age"
DEFAULT_ZSTD_LEVEL = 19

# Env vars for the recipient public key(s). RECIPIENT holds one or more inline
# `age1…`/`ssh-…` recipients (whitespace/comma separated); RECIPIENTS_FILE points
# at an age recipients file (one per line). Either or both may be set.
ENV_RECIPIENT = "RAINBOX_BACKUP_AGE_RECIPIENT"
ENV_RECIPIENTS_FILE = "RAINBOX_BACKUP_AGE_RECIPIENTS_FILE"


class NoRecipientError(RuntimeError):
    """No age recipient (public key) was configured. Backups are fail-closed:
    rather than write a plaintext dump, we refuse to back up at all."""


def backup_relative_path(now: datetime) -> Path:
    """Path of one backup relative to the backup repo, for a UTC `now`.

    Naive datetimes are assumed to already be UTC; aware ones are converted.
    Month/day/time components are zero-padded by strftime (so January is `01`).
    """
    if now.tzinfo is not None:
        now = now.astimezone(timezone.utc)
    bucket = now.strftime("%Y-%m-xx")
    filename = now.strftime(f"%Y-%m-%dT%H-%M-%SZ{BACKUP_SUFFIX}")
    return Path(DB_SUBDIR) / bucket / filename


def _resolve_tool(env_var: str, name: str) -> str:
    """Locate an external tool, preferring an explicit env override."""
    override = os.environ.get(env_var)
    if override:
        return override
    found = shutil.which(name)
    if not found:
        raise FileNotFoundError(
            f"{name!r} not found on PATH; set {env_var} to its full path"
        )
    return found


def split_recipients(value: str | None) -> list[str]:
    """Split an inline recipient string ("age1aaa, age1bbb") into a list,
    dropping empties. Whitespace- or comma-separated."""
    return [r for r in re.split(r"[\s,]+", (value or "").strip()) if r]


def resolve_recipients(
    recipients: list[str] | None = None,
    recipients_file: str | os.PathLike[str] | None = None,
) -> tuple[list[str], str | None]:
    """Resolve the age recipient public key(s) for this backup.

    Explicit arguments win; otherwise read RAINBOX_BACKUP_AGE_RECIPIENT (inline,
    whitespace/comma separated) and RAINBOX_BACKUP_AGE_RECIPIENTS_FILE. Returns
    (inline_recipients, recipients_file_or_None). Raises NoRecipientError if
    neither source yields anything — backups never silently fall back to
    plaintext."""
    if recipients is None:
        recipients = split_recipients(os.environ.get(ENV_RECIPIENT, ""))
    if recipients_file is None:
        recipients_file = os.environ.get(ENV_RECIPIENTS_FILE) or None
    if not recipients and not recipients_file:
        raise NoRecipientError(
            "no age recipient configured; set "
            f"{ENV_RECIPIENT} (an age1… public key) or {ENV_RECIPIENTS_FILE}, "
            "or pass --recipient. Backups are encrypt-only and will not run "
            "without a recipient public key."
        )
    return recipients, (str(recipients_file) if recipients_file else None)


def _run_pipeline(stages: list[list[str]], out) -> None:
    """Run `stages` as a shell-less pipeline (stage N's stdout → stage N+1's
    stdin), the last writing to the open file `out`. Raises RuntimeError naming
    the first failing stage and its stderr."""
    procs: list[subprocess.Popen] = []
    for i, argv in enumerate(stages):
        is_last = i == len(stages) - 1
        p = subprocess.Popen(
            argv,
            stdin=procs[-1].stdout if procs else None,
            stdout=out if is_last else subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if procs:
            # Close the parent's copy of the upstream pipe so the upstream stage
            # gets SIGPIPE if a downstream stage dies early.
            assert procs[-1].stdout is not None
            procs[-1].stdout.close()
        procs.append(p)

    # communicate() downstream-first; each call collects that stage's stderr and
    # waits for it to exit.
    errs = {id(p): p.communicate()[1] for p in reversed(procs)}
    for argv, p in zip(stages, procs):
        if p.returncode != 0:
            err = errs[id(p)].decode(errors="replace").strip()
            raise RuntimeError(f"{argv[0]} failed (exit {p.returncode}): {err}")


def backup_database(
    backup_repo: str | os.PathLike[str],
    *,
    dsn: str | None = None,
    now: datetime | None = None,
    zstd_level: int = DEFAULT_ZSTD_LEVEL,
    recipients: list[str] | None = None,
    recipients_file: str | os.PathLike[str] | None = None,
) -> Path:
    """Dump the database to an encrypted file under `backup_repo`, return its path.

    Streams `pg_dump | zstd | age -r <recipient>…` into a temp file in the
    destination directory, then atomically renames it into place, so an
    interrupted backup never leaves a truncated file looking complete. Encryption
    is mandatory: raises NoRecipientError before touching pg_dump if no recipient
    public key is configured.
    """
    recips, recips_file = resolve_recipients(recipients, recipients_file)

    dsn = psycopg_dsn() if dsn is None else dsn
    if now is None:
        now = datetime.now(timezone.utc)

    pg_dump = _resolve_tool("PG_DUMP", "pg_dump")
    zstd = _resolve_tool("ZSTD", "zstd")
    age = _resolve_tool("AGE", "age")

    age_argv = [age]
    for r in recips:
        age_argv += ["-r", r]
    if recips_file:
        age_argv += ["-R", recips_file]

    stages = [
        [pg_dump, dsn],
        [zstd, f"-{zstd_level}", "-c"],
        age_argv,  # reads stdin, writes ciphertext to stdout
    ]

    dest = Path(backup_repo) / backup_relative_path(now)
    dest.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        dir=dest.parent, prefix=dest.name + ".", suffix=".part"
    )
    tmp = Path(tmp_name)
    logger.info("backing up %s -> %s (encrypted to %d recipient(s))",
                dsn, dest, len(recips) + (1 if recips_file else 0))
    try:
        with os.fdopen(fd, "wb") as out:
            _run_pipeline(stages, out)
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

    logger.info("backup complete: %s (%d bytes)", dest, dest.stat().st_size)
    return dest


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    parser = argparse.ArgumentParser(
        description="Back up the rainbox database to a zstd+age-encrypted file."
    )
    parser.add_argument(
        "backup_repo",
        nargs="?",
        default=os.environ.get("RAINBOX_BACKUP_REPO"),
        help="Backup-repo directory (or set RAINBOX_BACKUP_REPO). "
        "Backups land under <repo>/rainbox_database/.",
    )
    parser.add_argument(
        "-r", "--recipient",
        action="append",
        dest="recipients",
        metavar="age1...",
        help=f"age recipient public key (repeatable). Or set {ENV_RECIPIENT}.",
    )
    parser.add_argument(
        "--recipients-file",
        metavar="PATH",
        help=f"age recipients file (one per line). Or set {ENV_RECIPIENTS_FILE}.",
    )
    parser.add_argument(
        "--zstd-level",
        type=int,
        default=DEFAULT_ZSTD_LEVEL,
        help=f"zstd compression level (default {DEFAULT_ZSTD_LEVEL}).",
    )
    parser.add_argument(
        "--git-push",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="After writing the backup, commit it into the backup-repo git repo "
        "and push (default: RAINBOX_BACKUP_GIT_PUSH).",
    )
    args = parser.parse_args()

    if not args.backup_repo:
        parser.error("backup_repo is required (pass it or set RAINBOX_BACKUP_REPO)")

    try:
        dest = backup_database(
            args.backup_repo,
            zstd_level=args.zstd_level,
            recipients=args.recipients,
            recipients_file=args.recipients_file,
        )
    except NoRecipientError as exc:
        parser.error(str(exc))
    print(dest)

    import backup_remote

    push = backup_remote.git_push_enabled() if args.git_push is None else args.git_push
    if push:
        # The local backup already succeeded; an upload failure shouldn't discard
        # it, but it should still be a non-zero exit so a caller notices.
        try:
            logger.info("%s", backup_remote.git_push_backup(args.backup_repo, dest))
        except Exception as exc:  # noqa: BLE001
            logger.error("upload failed: %s", exc)
            sys.exit(1)


if __name__ == "__main__":
    sys.exit(main())
