"""Tests for backup_db.

The path-layout tests are pure (no DB). test_backup_database_roundtrip hits the
live local Postgres (db.psycopg_dsn()) but writes only into pytest's tmp_path,
so it leaves no artifacts behind. Backups are encrypt-only (age), so the
round-trip generates a throwaway keypair, encrypts to its public key, then
decrypts with the private identity to verify.
"""
import re
import subprocess
from datetime import datetime, timezone

import pytest

import backup_db


@pytest.fixture
def age_keypair(tmp_path):
    """A throwaway age keypair. Returns (recipient_pubkey, identity_path)."""
    identity = tmp_path / "identity.txt"
    out = subprocess.run(
        ["age-keygen", "-o", str(identity)], capture_output=True, text=True, check=True
    )
    # age-keygen prints "Public key: age1..." to stderr.
    m = re.search(r"(age1[0-9a-z]+)", out.stderr + out.stdout)
    assert m, f"could not parse age public key from: {out.stderr!r}"
    return m.group(1), identity


def test_backup_relative_path_example():
    # The example from the spec: Jan 1 2026 22:39:06 UTC.
    now = datetime(2026, 1, 1, 22, 39, 6)
    rel = backup_db.backup_relative_path(now)
    assert rel.as_posix() == "rainbox_database/2026-01-xx/2026-01-01T22-39-06Z.zstd.age"


def test_backup_relative_path_zero_pads_single_digit_month_and_day():
    rel = backup_db.backup_relative_path(datetime(2026, 3, 7, 4, 5, 6))
    assert rel.as_posix() == "rainbox_database/2026-03-xx/2026-03-07T04-05-06Z.zstd.age"


def test_backup_relative_path_two_digit_month():
    rel = backup_db.backup_relative_path(datetime(2026, 12, 31, 23, 59, 59))
    assert rel.as_posix() == "rainbox_database/2026-12-xx/2026-12-31T23-59-59Z.zstd.age"


def test_backup_relative_path_no_colon_in_name():
    rel = backup_db.backup_relative_path(datetime(2026, 1, 1, 22, 39, 6))
    assert ":" not in rel.as_posix()


def test_backup_relative_path_converts_aware_to_utc():
    from datetime import timedelta

    tz = timezone(timedelta(hours=2))  # 00:30 local == 22:30 UTC previous day
    aware = datetime(2026, 1, 2, 0, 30, 0, tzinfo=tz)
    rel = backup_db.backup_relative_path(aware)
    assert rel.as_posix() == "rainbox_database/2026-01-xx/2026-01-01T22-30-00Z.zstd.age"


def test_resolve_recipients_inline_and_env(monkeypatch):
    # Explicit argument wins.
    recips, rfile = backup_db.resolve_recipients(["age1aaa", "age1bbb"])
    assert recips == ["age1aaa", "age1bbb"] and rfile is None
    # Env var, whitespace/comma separated.
    monkeypatch.setenv(backup_db.ENV_RECIPIENT, "age1aaa, age1bbb\nage1ccc")
    recips, _ = backup_db.resolve_recipients()
    assert recips == ["age1aaa", "age1bbb", "age1ccc"]


def test_resolve_recipients_missing_raises(monkeypatch):
    monkeypatch.delenv(backup_db.ENV_RECIPIENT, raising=False)
    monkeypatch.delenv(backup_db.ENV_RECIPIENTS_FILE, raising=False)
    with pytest.raises(backup_db.NoRecipientError):
        backup_db.resolve_recipients()


def test_backup_database_roundtrip(tmp_path, age_keypair):
    """Take a real encrypted backup, then decrypt+decompress and verify it is a
    valid pg_dump (standard header)."""
    recipient, identity = age_keypair
    now = datetime(2026, 1, 1, 22, 39, 6, tzinfo=timezone.utc)
    dest = backup_db.backup_database(
        tmp_path, now=now, zstd_level=1, recipients=[recipient]
    )

    assert dest == tmp_path / "rainbox_database/2026-01-xx/2026-01-01T22-39-06Z.zstd.age"
    assert dest.is_file() and dest.stat().st_size > 0
    # No leftover temp/part files in the bucket dir.
    assert sorted(p.name for p in dest.parent.iterdir()) == [dest.name]

    # Ciphertext is an age file, not raw zstd.
    assert dest.read_bytes().startswith(b"age-encryption.org/")

    # Decrypt with the private identity, then decompress, and check the header.
    decrypted = subprocess.run(
        ["age", "-d", "-i", str(identity), str(dest)],
        capture_output=True, check=True,
    ).stdout
    zstd = backup_db._resolve_tool("ZSTD", "zstd")
    plain = subprocess.run(
        [zstd, "-dc"], input=decrypted, capture_output=True, check=True
    ).stdout
    assert b"PostgreSQL database dump" in plain


def test_backup_database_without_recipient_raises(tmp_path, monkeypatch):
    """Fail-closed: no recipient configured -> refuse to back up (no plaintext)."""
    monkeypatch.delenv(backup_db.ENV_RECIPIENT, raising=False)
    monkeypatch.delenv(backup_db.ENV_RECIPIENTS_FILE, raising=False)
    now = datetime(2026, 1, 1, 22, 39, 6, tzinfo=timezone.utc)
    with pytest.raises(backup_db.NoRecipientError):
        backup_db.backup_database(tmp_path, now=now)
    # Nothing was written.
    assert not (tmp_path / "rainbox_database").exists()


def test_backup_database_missing_tool_raises(tmp_path, monkeypatch, age_keypair):
    recipient, _ = age_keypair
    monkeypatch.setenv("PG_DUMP", "/nonexistent/definitely-not-pg_dump-binary")
    now = datetime(2026, 1, 1, 22, 39, 6, tzinfo=timezone.utc)
    with pytest.raises(Exception):
        backup_db.backup_database(tmp_path, now=now, recipients=[recipient])
    # A failed backup leaves nothing behind.
    bucket = tmp_path / "rainbox_database/2026-01-xx"
    assert not bucket.exists() or list(bucket.iterdir()) == []
