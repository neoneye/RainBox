"""`sync_source`: reconcile one source's directory with the database.

CLI-only (proposal §6). One sync per source at a time, enforced by a
session-level advisory lock on a dedicated connection. Each file publishes in
its own transaction; a policy change between files stops the sync. No model
or network call happens here: embedding is a separate command.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import sqlalchemy as sa

import db
from diary.config import file_included
from diary.parsing import parse_file, sha256_hex

STABLE_READ_ATTEMPTS = 3   # the first read plus two retries


@dataclass
class SyncReport:
    source_uuid: UUID
    busy: bool = False
    scan_failed: bool = False
    aborted: str | None = None     # policy_changed when the policy moved mid-sync
    seen: int = 0
    published: int = 0
    unchanged: int = 0
    quarantined: int = 0
    missing: int = 0
    held: int = 0
    carried: int = 0
    excluded: int = 0
    failures: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        out = {k: v for k, v in self.__dict__.items() if k != "source_uuid"}
        out["source_uuid"] = str(self.source_uuid)
        return out


def enumerate_files(root: str, suffixes: list[str]) -> list[str]:
    """Included regular files below `root`, as relative POSIX paths, sorted.
    Symlinks (files and directories) are skipped. Raises OSError when the
    root cannot be walked, so a scan failure never marks files missing."""
    if not os.path.isdir(root):
        raise FileNotFoundError(root)
    errors: list[OSError] = []
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, onerror=errors.append, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not os.path.islink(os.path.join(dirpath, d)))
        for name in filenames:
            full = os.path.join(dirpath, name)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            rel = Path(os.path.relpath(full, root)).as_posix()
            if file_included(rel, suffixes):
                out.append(rel)
    if errors:
        raise errors[0]
    return sorted(out)


def stable_read(path: str) -> bytes | None:
    """The file's bytes if identity, size and mtime are unchanged across the
    read; None if the file kept changing through every attempt."""
    for _ in range(STABLE_READ_ATTEMPTS):
        before = os.stat(path, follow_symlinks=False)
        with open(path, "rb") as fh:
            data = fh.read()
        after = os.stat(path, follow_symlinks=False)
        key = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)  # noqa: E731
        if key(before) == key(after) and len(data) == after.st_size:
            return data
    return None


def _codes(diagnostics: list[dict] | None) -> set[str]:
    return {d.get("code") for d in (diagnostics or [])}


def sync_source(source_uuid: UUID) -> SyncReport:
    report = SyncReport(source_uuid)
    key = db.diary_advisory_key(source_uuid)
    conn = db.db.engine.connect()
    try:
        got = conn.execute(sa.text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).scalar()
        conn.commit()
        if not got:
            report.busy = True
            return report
        try:
            _sync(source_uuid, report)
        except db.DiaryPolicyChanged:
            db.session.rollback()
            report.aborted = "policy_changed"
        finally:
            conn.execute(sa.text("SELECT pg_advisory_unlock(:k)"), {"k": key})
            conn.commit()
    finally:
        conn.close()
    return report


def _sync(source_uuid: UUID, report: SyncReport) -> None:
    source = db.diary_get_source(source_uuid)
    policy = source.policy_version
    parser_config = source.parser_config
    fingerprint = source.parser_fingerprint
    root = source.root_path
    suffixes = source.config.get("include_suffixes", [".txt"])
    db.session.commit()   # release the read snapshot; each publish is its own transaction

    if os.path.realpath(root) != root:
        report.scan_failed = True
        report.failures.append({"code": "root_mismatch"})
        return
    try:
        listing = enumerate_files(root, suffixes)
    except OSError as exc:
        report.scan_failed = True
        report.failures.append({"code": "scan_failed", "errno": exc.errno or 0})
        return
    report.seen = len(listing)
    seen = set(listing)
    exclusions = db.diary_exclusions(source_uuid)
    known = {f.relative_path: f for f in
             db.session.query(db.DiaryFile).filter_by(source_uuid=source_uuid)}
    db.session.commit()

    vanished_excluded = [f for path, f in known.items()
                         if path in exclusions and path not in seen and f.availability != "missing"]
    new_paths = {p for p in listing if p not in known and p not in exclusions}
    carry: dict[str, str] = {}
    if vanished_excluded and new_paths:
        for f in vanished_excluded:
            for (sha,) in db.session.query(db.DiaryRevision.sha256).filter_by(file_uuid=f.uuid):
                carry[sha] = f.relative_path
        db.session.commit()

    for path in listing:
        if path in exclusions:
            report.excluded += 1
            f, _ = db.diary_get_or_create_file(source_uuid, path)
            db.diary_mark_file(f.uuid, seen=True)
            continue
        f, _ = db.diary_get_or_create_file(source_uuid, path)
        if "held_for_reconcile" in _codes(f.diagnostics):
            report.held += 1
            continue
        raw = stable_read(os.path.join(root, path))
        if raw is None:
            report.quarantined += 1
            db.diary_mark_file(f.uuid, availability="quarantined", policy_version=policy,
                               diagnostics=[{"code": "unstable_read", "byte_offset": 0}])
            continue
        sha = sha256_hex(raw)

        if path in new_paths and vanished_excluded:
            if sha in carry:
                db.diary_exclude(source_uuid, path)
                exclusions.add(path)
                policy = db.diary_get_source(source_uuid).policy_version
                db.diary_mark_file(f.uuid, seen=True, diagnostics=[{
                    "code": "exclusion_carried", "byte_offset": 0}])
                db.session.commit()
                report.carried += 1
            else:
                db.diary_mark_file(f.uuid, availability="pending", policy_version=policy,
                                   diagnostics=[{"code": "held_for_reconcile", "byte_offset": 0}])
                report.held += 1
            continue

        if _unchanged(f, sha, fingerprint):
            report.unchanged += 1
            db.diary_mark_file(f.uuid, seen=True)
            continue

        parsed = parse_file(raw, path, parser_config)
        if parsed.status != "ok":
            report.quarantined += 1
            db.diary_mark_file(f.uuid, availability="quarantined", policy_version=policy,
                               diagnostics=parsed.diagnostics_json(), seen=True)
            continue
        problem = validate_parsed(raw, parsed)
        if problem is not None:
            report.quarantined += 1
            report.failures.append({"code": "publication_invalid", "path_index": listing.index(path)})
            db.diary_mark_file(f.uuid, availability="quarantined", policy_version=policy,
                               diagnostics=[{"code": "publication_invalid", "byte_offset": problem}],
                               seen=True)
            continue
        db.diary_publish(f.uuid, raw, sha, parsed, parser_config=parser_config,
                         fingerprint=fingerprint, policy_version=policy)
        report.published += 1

    # Missing only after a complete, successful enumeration.
    for path, f in known.items():
        if path not in seen and f.availability != "missing":
            db.diary_mark_file(f.uuid, availability="missing")
            report.missing += 1


def _unchanged(f: Any, sha: str, fingerprint: str) -> bool:
    if f.availability != "ready" or f.current_generation_uuid is None:
        return False
    row = db.session.execute(sa.text(
        "SELECT r.sha256, g.parser_fingerprint FROM diary_generation g "
        "JOIN diary_revision r ON r.uuid = g.revision_uuid WHERE g.uuid = :g"),
        {"g": f.current_generation_uuid}).first()
    db.session.commit()
    return row is not None and row[0] == sha and row[1] == fingerprint


def validate_parsed(raw: bytes, parsed: Any) -> int | None:
    """The publisher's own check before any switch: coverage tiles
    [0, len(raw)), every entry/passage range slices to its exact text on
    UTF-8 boundaries. Returns the first offending byte offset, or None."""
    pos = 0
    for start, end, _tag in parsed.coverage:
        if start != pos or end <= start:
            return start
        pos = end
    if pos != len(raw):
        return pos
    for e in parsed.entries:
        try:
            if raw[e.byte_start:e.byte_end].decode("utf-8") != e.text:
                return e.byte_start
            for p in e.passages:
                if raw[p.byte_start:p.byte_end].decode("utf-8") != p.text:
                    return p.byte_start
        except UnicodeDecodeError:
            return e.byte_start
    return None
