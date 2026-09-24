"""Diary persistence and sync (proposal §4, §6): registration, publication,
linear storage under appends, stable citations, exclusions, the rename hold,
prune/purge, locking and policy races. Synthetic files in tmp_path only."""

import shutil
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa

import db
from diary.config import load_manifest
from diary.ingest import sync_source
from diary.parsing import parse_file, sha256_hex

FIXTURE = Path(__file__).resolve().parent.parent / "data" / "diary_fixture"


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    try:
        yield app
    finally:
        db.session.rollback()
        ctx.pop()


def manifest_for(root: Path, room_uuid, name: str, **changes):
    raw = {
        "schema_version": 1, "name": name, "root": str(root),
        "room_uuid": str(room_uuid), "agent_uuid": None,
        "timezone": "Europe/Copenhagen", "sensitivity": "private",
        "include_suffixes": [".txt"],
        "dialect_rules": [{"prefix": "current/", "dialect": "timed"},
                          {"prefix": "daily/", "dialect": "daily"},
                          {"prefix": "archive/", "dialect": "changelog"}],
        "month_languages": ["en", "da", "de"], "command_tokens": ["/goal"],
        "author_tokens": {"kreese": "operator"}, "file_overrides": {},
    }
    raw.update(changes)
    return load_manifest(raw)


@pytest.fixture
def source(app_ctx, tmp_path):
    """A registered source over a copy of the fixture tree, in its own room."""
    root = tmp_path / "input"
    shutil.copytree(FIXTURE, root, ignore=shutil.ignore_patterns("README.md"))
    room_uuid = uuid.uuid4()
    db.session.add(db.Chatroom(uuid=room_uuid, name="diary-test", created_by=uuid.uuid4()))
    db.session.commit()
    name = f"diary-test-{uuid.uuid4().hex[:12]}"
    src, created = db.diary_register_source(manifest_for(root.resolve(), room_uuid, name))
    assert created
    try:
        yield src, root.resolve(), room_uuid
    finally:
        db.session.rollback()
        db.session.execute(sa.update(db.DiaryFile)
                           .where(db.DiaryFile.source_uuid == src.uuid)
                           .values(current_generation_uuid=None))
        db.session.execute(sa.delete(db.DiarySource).where(db.DiarySource.uuid == src.uuid))
        db.session.execute(sa.delete(db.Chatroom).where(db.Chatroom.uuid == room_uuid))
        db.session.commit()


def files(src):
    return {f.relative_path: f for f in
            db.session.query(db.DiaryFile).filter_by(source_uuid=src.uuid)}


def refresh(src):
    db.session.expire_all()
    return db.session.get(db.DiarySource, src.uuid)


def current_passages(f):
    return (db.session.query(db.DiaryPassage)
            .join(db.DiaryEntry, db.DiaryEntry.uuid == db.DiaryPassage.entry_uuid)
            .filter(db.DiaryEntry.generation_uuid == f.current_generation_uuid)
            .order_by(db.DiaryPassage.byte_start).all())


def revisions(f):
    return db.session.query(db.DiaryRevision).filter_by(file_uuid=f.uuid).all()


# --- bootstrap and registration -----------------------------------------------------


def test_bootstrap_is_repeatable(app_ctx):
    db.init_db(app_ctx)
    db.init_db(app_ctx)
    for name in ("fk_diary_file_current_generation", "ck_retrieval_event_target_type",
                 "eval_case_case_type_check"):
        assert db._constraint_def(name) is not None
    assert "diary_citation" in db._constraint_def("ck_retrieval_event_target_type")
    assert "diary_recall" in db._constraint_def("eval_case_case_type_check")


def test_register_starts_disabled_and_reregister_keeps_identity(source):
    src, root, room = source
    assert src.enabled is False and src.policy_version == 1
    db.diary_exclude(src.uuid, "archive/ChangeLog.txt")
    db.diary_set_enabled(src.uuid, True)
    again, created = db.diary_register_source(manifest_for(root, room, src.name, timezone="UTC"))
    assert not created and again.uuid == src.uuid
    again = refresh(again)
    assert again.enabled is False and again.timezone == "UTC"
    assert db.diary_exclusions(src.uuid) == {"archive/ChangeLog.txt"}


def test_register_refuses_root_change_and_unknown_room(source, tmp_path):
    src, root, room = source
    other = tmp_path / "elsewhere"
    other.mkdir()
    with pytest.raises(db.DiaryError, match="root change"):
        db.diary_register_source(manifest_for(other.resolve(), room, src.name))
    with pytest.raises(db.DiaryError, match="does not exist"):
        db.diary_register_source(manifest_for(other.resolve(), uuid.uuid4(), "no-room-src"))


def test_secret_source_cannot_be_enabled(source):
    src, root, room = source
    db.diary_register_source(manifest_for(root, room, src.name, sensitivity="secret"))
    with pytest.raises(db.DiaryError, match="secret"):
        db.diary_set_enabled(src.uuid, True)


# --- sync and publication ---------------------------------------------------------------


def test_sync_publishes_and_a_second_sync_is_a_no_op(source):
    src, root, _ = source
    report = sync_source(src.uuid)
    assert (report.seen, report.published, report.quarantined) == (3, 3, 0)
    catalog = refresh(src).catalog_version
    fs = files(src)
    assert {f.availability for f in fs.values()} == {"ready"}
    for path, f in fs.items():
        raw = (root / path).read_bytes()
        (rev,) = revisions(f)
        assert rev.sha256 == sha256_hex(raw) and bytes(rev.raw_bytes) == raw
        for p in current_passages(f):
            assert raw[p.byte_start:p.byte_end].decode() == p.text
            assert p.cite_revision_uuid == rev.uuid
    again = sync_source(src.uuid)
    assert (again.published, again.unchanged) == (0, 3)
    assert refresh(src).catalog_version == catalog


def test_search_vector_is_generated(source):
    src, _, _ = source
    sync_source(src.uuid)
    hit = db.session.execute(sa.text(
        "SELECT count(*) FROM diary_passage WHERE search_vector @@ to_tsquery('simple', 'physiotherapy')"
    )).scalar()
    assert hit >= 2


def test_appends_store_bytes_once_and_keep_citations(source):
    """The replay the pilot gate runs, in miniature: thirty daily appends."""
    src, root, _ = source
    path = root / "current" / "2027.txt"
    base = path.read_bytes()
    path.write_bytes(base)
    sync_source(src.uuid)
    f = files(src)["current/2027.txt"]
    first_cites = {(p.byte_start, p.byte_end): p.cite_revision_uuid for p in current_passages(f)}
    content = base
    for day in range(1, 31):
        content += f"\n2027040{day % 10}\n09h00\nappended day {day}\n".encode()
        path.write_bytes(content)
        report = sync_source(src.uuid)
        assert report.published == 1
    db.session.expire_all()
    f = files(src)["current/2027.txt"]
    revs = revisions(f)
    assert len(revs) == 31
    stored = sum(len(r.raw_bytes) for r in revs if r.raw_bytes is not None)
    assert stored == len(content)                           # bytes once
    hosts = {r.bytes_in_revision_uuid for r in revs if r.raw_bytes is None}
    assert len(hosts) == 1                                  # one hop, to the newest
    gens = db.session.query(db.DiaryGeneration).filter_by(file_uuid=f.uuid).count()
    assert gens == 1                                        # one generation
    for rev in revs:
        assert db.diary_revision_bytes(rev) == content[: rev.byte_length]
    now_cites = {(p.byte_start, p.byte_end): p.cite_revision_uuid for p in current_passages(f)}
    for key, cite in first_cites.items():
        if key in now_cites:
            assert now_cites[key] == cite                   # untouched text keeps its citation


def test_edit_moves_citations_and_revert_reuses_revision(source):
    src, root, _ = source
    path = root / "current" / "2027.txt"
    original = path.read_bytes()
    sync_source(src.uuid)
    edited = original.replace(b"Slept badly", b"Slept well")
    path.write_bytes(edited)
    sync_source(src.uuid)
    f = files(src)["current/2027.txt"]
    revs = revisions(f)
    assert len(revs) == 2 and all(r.raw_bytes is not None for r in revs)
    new = next(r for r in revs if r.sha256 == sha256_hex(edited))
    assert {p.cite_revision_uuid for p in current_passages(f)} == {new.uuid}
    path.write_bytes(original)
    sync_source(src.uuid)
    db.session.expire_all()
    f = files(src)["current/2027.txt"]
    assert len(revisions(f)) == 2                          # reused, not re-inserted
    gen = db.session.get(db.DiaryGeneration, f.current_generation_uuid)
    assert db.session.get(db.DiaryRevision, gen.revision_uuid).sha256 == sha256_hex(original)


def test_quarantine_hides_file_until_fixed(source):
    src, root, _ = source
    path = root / "current" / "2027.txt"
    good = path.read_bytes()
    sync_source(src.uuid)
    path.write_bytes(good + b"\xff\n")
    report = sync_source(src.uuid)
    assert report.quarantined == 1
    f = files(src)["current/2027.txt"]
    assert f.availability == "quarantined"
    assert f.diagnostics[0]["code"] == "invalid_utf8"
    path.write_bytes(good + b"\nfine\n")
    sync_source(src.uuid)
    db.session.expire_all()
    assert files(src)["current/2027.txt"].availability == "ready"


def test_deleted_file_is_missing_and_scan_failure_marks_nothing(source):
    src, root, _ = source
    sync_source(src.uuid)
    (root / "archive" / "ChangeLog.txt").unlink()
    report = sync_source(src.uuid)
    assert report.missing == 1
    assert files(src)["archive/ChangeLog.txt"].availability == "missing"
    shutil.rmtree(root)
    failed = sync_source(src.uuid)
    assert failed.scan_failed and failed.missing == 0
    db.session.expire_all()
    assert files(src)["current/2027.txt"].availability == "ready"


def test_policy_change_invalidates_publication(source):
    src, root, _ = source
    sync_source(src.uuid)
    f = files(src)["current/2027.txt"]
    raw = (root / "current" / "2027.txt").read_bytes() + b"\nmore\n"
    parsed = parse_file(raw, "current/2027.txt", src.parser_config)
    stale = refresh(src).policy_version
    db.diary_set_enabled(src.uuid, True)                  # bumps the policy
    with pytest.raises(db.DiaryPolicyChanged):
        db.diary_publish(f.uuid, raw, sha256_hex(raw), parsed, parser_config=src.parser_config,
                         fingerprint=src.parser_fingerprint, policy_version=stale)
    db.session.expire_all()
    assert files(src)["current/2027.txt"].current_generation_uuid == f.current_generation_uuid


def test_failed_publication_leaves_no_partial_generation(source, monkeypatch):
    src, root, _ = source
    sync_source(src.uuid)
    before = files(src)["current/2027.txt"].current_generation_uuid
    (root / "current" / "2027.txt").write_bytes(b"20270312\n09h00\nnew text\n")
    real_insert = sa.insert

    def failing_insert(table, *a, **k):
        if table is db.DiaryPassage:
            raise RuntimeError("simulated crash")
        return real_insert(table, *a, **k)

    monkeypatch.setattr(sa, "insert", failing_insert)
    with pytest.raises(RuntimeError):
        sync_source(src.uuid)
    monkeypatch.setattr(sa, "insert", real_insert)
    db.session.rollback()
    db.session.expire_all()
    f = files(src)["current/2027.txt"]
    assert f.current_generation_uuid == before
    assert db.session.query(db.DiaryGeneration).filter_by(file_uuid=f.uuid).count() == 1


def test_concurrent_sync_is_busy(source):
    src, _, _ = source
    conn = db.db.engine.connect()
    try:
        conn.execute(sa.text("SELECT pg_advisory_lock(:k)"), {"k": db.diary_advisory_key(src.uuid)})
        assert sync_source(src.uuid).busy
    finally:
        conn.execute(sa.text("SELECT pg_advisory_unlock_all()"))
        conn.close()


# --- exclusions, rename hold, prune, purge -------------------------------------------------


def test_excluded_path_is_never_read(source):
    src, _, _ = source
    db.diary_exclude(src.uuid, "daily/2027_07_26.txt")
    report = sync_source(src.uuid)
    assert report.excluded == 1 and report.published == 2
    f = files(src)["daily/2027_07_26.txt"]
    assert revisions(f) == [] and f.current_generation_uuid is None


def test_exact_move_carries_exclusion(source):
    src, root, _ = source
    sync_source(src.uuid)
    db.diary_exclude(src.uuid, "daily/2027_07_26.txt")
    (root / "daily" / "2027_07_26.txt").rename(root / "daily" / "2027_07_27.txt")
    report = sync_source(src.uuid)
    assert report.carried == 1
    assert "daily/2027_07_27.txt" in db.diary_exclusions(src.uuid)
    assert files(src)["daily/2027_07_27.txt"].current_generation_uuid is None


def test_edited_rename_is_held_until_reconciled(source):
    src, root, _ = source
    sync_source(src.uuid)
    db.diary_exclude(src.uuid, "daily/2027_07_26.txt")
    old = root / "daily" / "2027_07_26.txt"
    (root / "daily" / "2027_07_28.txt").write_bytes(old.read_bytes() + b"\nedited\n")
    old.unlink()
    report = sync_source(src.uuid)
    assert report.held == 1 and report.published == 0
    held = files(src)["daily/2027_07_28.txt"]
    assert held.availability == "pending" and held.current_generation_uuid is None
    assert sync_source(src.uuid).held == 1                  # still held
    assert db.diary_reconcile(src.uuid, exclude=[], release=["daily/2027_07_28.txt"]) == []
    assert sync_source(src.uuid).published == 1


def test_prune_rematerializes_then_deletes(source):
    src, root, _ = source
    path = root / "current" / "2027.txt"
    sync_source(src.uuid)
    path.write_bytes(path.read_bytes() + b"\n20270401\n09h00\nx\n")
    sync_source(src.uuid)
    f = files(src)["current/2027.txt"]
    assert len(revisions(f)) == 2
    assert db.diary_prune(src.uuid, "current/2027.txt") == 1
    db.session.expire_all()
    (only,) = revisions(files(src)["current/2027.txt"])
    assert only.raw_bytes is not None and bytes(only.raw_bytes) == path.read_bytes()
    assert {p.cite_revision_uuid for p in current_passages(f)} == {only.uuid}


def test_purge_keeps_configuration_and_exclusions(source):
    src, _, _ = source
    sync_source(src.uuid)
    db.diary_exclude(src.uuid, "archive/ChangeLog.txt")
    assert db.diary_purge(src.uuid) == 3
    src = refresh(src)
    assert src.enabled is False and files(src) == {}
    assert db.diary_exclusions(src.uuid) == {"archive/ChangeLog.txt"}
    assert sync_source(src.uuid).published == 2
