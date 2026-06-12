"""Tests for backup_remote (git commit + push of a backup file).

No DB and no network: a local bare repo stands in for the remote, and a working
clone is the backup-repo. Everything lives under pytest's tmp_path.
"""
import subprocess

import pytest

import backup_remote


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True)


@pytest.fixture
def backup_repo(tmp_path):
    """A working git repo (the backup-repo) wired to a local bare 'remote'."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "remote", "add", "origin", str(remote))
    (repo / "README.md").write_text("backups\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    _git(repo, "push", "-u", "origin", "HEAD")
    return repo, remote


def test_git_push_enabled(monkeypatch):
    for v in ("1", "true", "YES", "On"):
        monkeypatch.setenv("RAINBOX_BACKUP_GIT_PUSH", v)
        assert backup_remote.git_push_enabled() is True
    for v in ("", "0", "no", "off"):
        monkeypatch.setenv("RAINBOX_BACKUP_GIT_PUSH", v)
        assert backup_remote.git_push_enabled() is False
    monkeypatch.delenv("RAINBOX_BACKUP_GIT_PUSH", raising=False)
    assert backup_remote.git_push_enabled() is False


def test_is_git_repo(backup_repo, tmp_path):
    repo, _ = backup_repo
    assert backup_remote.is_git_repo(repo) is True
    plain = tmp_path / "plain"
    plain.mkdir()
    assert backup_remote.is_git_repo(plain) is False


def test_push_commits_and_uploads(backup_repo):
    repo, remote = backup_repo
    f = repo / "rainbox_database" / "2026-01-xx" / "2026-01-01T03-30-00Z.zstd.age"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"age-encryption.org/v1\nciphertext")

    note = backup_remote.git_push_backup(repo, f)
    assert "pushed" in note and "2026-01-01T03-30-00Z.zstd.age" in note

    # The file is committed locally…
    log = _git(repo, "log", "--name-only", "--format=", "-1").stdout
    assert "2026-01-01T03-30-00Z.zstd.age" in log
    # …and present in the remote (bare repo) tree.
    tree = _git(remote, "ls-tree", "-r", "--name-only", "HEAD").stdout
    assert "rainbox_database/2026-01-xx/2026-01-01T03-30-00Z.zstd.age" in tree


def test_push_same_file_twice_is_noop(backup_repo):
    repo, _ = backup_repo
    f = repo / "rainbox_database" / "2026-01-xx" / "x.zstd.age"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"data")
    backup_remote.git_push_backup(repo, f)
    note = backup_remote.git_push_backup(repo, f)
    assert "nothing to push" in note


def test_push_file_outside_repo_raises(backup_repo, tmp_path):
    repo, _ = backup_repo
    outside = tmp_path / "elsewhere.zstd.age"
    outside.write_bytes(b"data")
    with pytest.raises(RuntimeError, match="not inside the git repo"):
        backup_remote.git_push_backup(repo, outside)


def test_push_no_remote_raises(tmp_path):
    repo = tmp_path / "norem"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.email", "t@e.com")
    _git(repo, "config", "user.name", "T")
    f = repo / "b.zstd.age"
    f.write_bytes(b"data")
    with pytest.raises(RuntimeError, match="git push failed"):
        backup_remote.git_push_backup(repo, f)
