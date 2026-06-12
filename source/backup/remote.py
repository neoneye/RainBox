"""Upload a finished backup to a remote by committing it into a git repo.

The backup-repo directory (where `backup.dump` writes `…Z.zstd.age` files) can
itself be a git repo with a remote — e.g.
`git@github.com:Username/rainbox-backup.git`. "Upload to remote" then
means: stage just the new backup file, commit it, and `git push`. Because the
files are already public-key-encrypted ciphertext (see `backup.dump`), pushing
them to untrusted storage like GitHub is safe.

This is opt-in: enabled by `RAINBOX_BACKUP_GIT_PUSH` (truthy) or the
`--git-push` CLI flag. Pushing needs the running process to have credentials for
the remote (e.g. an SSH agent/key for an `git@github.com:` remote).
"""
import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# How long to allow each git invocation (the push talks to the network).
GIT_TIMEOUT = 180

_TRUTHY = {"1", "true", "yes", "on"}


def git_push_enabled() -> bool:
    """Whether backups should be pushed, from RAINBOX_BACKUP_GIT_PUSH."""
    return os.environ.get("RAINBOX_BACKUP_GIT_PUSH", "").strip().lower() in _TRUTHY


def is_git_repo(path: str | os.PathLike[str]) -> bool:
    git = shutil.which(os.environ.get("GIT", "git"))
    if not git:
        return False
    return subprocess.run(
        [git, "-C", str(path), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
    ).returncode == 0


def _git(repo: Path, args: list[str]) -> subprocess.CompletedProcess:
    git = shutil.which(os.environ.get("GIT", "git"))
    if not git:
        raise FileNotFoundError("'git' not found on PATH; set GIT to its full path")
    return subprocess.run(
        [git, "-C", str(repo), *args],
        capture_output=True, text=True, timeout=GIT_TIMEOUT,
    )


def _check(cp: subprocess.CompletedProcess, what: str) -> None:
    if cp.returncode != 0:
        raise RuntimeError(f"{what} failed (exit {cp.returncode}): {cp.stderr.strip()}")


def git_push_backup(
    repo: str | os.PathLike[str],
    file: str | os.PathLike[str],
    *,
    remote: str = "origin",
) -> str:
    """Commit `file` (which must live under `repo`) and push to `remote`.

    Stages only that file (so unrelated working-tree changes are left alone),
    commits, and pushes the current branch. Returns a short human summary.
    Raises RuntimeError on any git failure (no remote, auth, non-fast-forward,
    network/timeout)."""
    repo = Path(repo).resolve()
    file = Path(file).resolve()
    try:
        rel = file.relative_to(repo).as_posix()
    except ValueError as e:
        raise RuntimeError(
            f"backup file {file} is not inside the git repo {repo}"
        ) from e

    if not is_git_repo(repo):
        raise RuntimeError(f"{repo} is not a git repository")

    _check(_git(repo, ["add", "--", rel]), "git add")

    # Commit only if staging produced a change (a re-pushed identical path is a
    # no-op rather than an error). `diff --cached --quiet` exits 1 when staged.
    if _git(repo, ["diff", "--cached", "--quiet", "--", rel]).returncode == 0:
        return f"{rel}: already committed, nothing to push"

    _check(_git(repo, ["commit", "-m", f"backup {rel}", "--", rel]), "git commit")
    _check(_git(repo, ["push", remote, "HEAD"]), "git push")
    return f"pushed {rel} to {remote}"
