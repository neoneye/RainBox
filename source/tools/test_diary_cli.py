"""tools.diary: the database guard runs before any connection, and the
commands work end to end on the sandbox database."""

import json
import os
import shutil
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa

from tools import diary as cli

FIXTURE = Path(__file__).resolve().parent.parent / "data" / "diary_fixture"
SANDBOX_URL = os.environ["DATABASE_URL"]


@pytest.mark.parametrize("url, production, pilot, ok", [
    ("postgresql+psycopg://localhost/rainbox_claude", False, False, True),
    ("postgresql+psycopg://localhost/rainbox_diary_test_a", False, True, True),
    ("postgresql+psycopg://localhost/rainbox_production", False, False, False),
    ("postgresql+psycopg://localhost/rainbox_production", True, False, True),
    ("postgresql+psycopg://localhost/rainbox_production", True, True, False),
    ("postgresql+psycopg://localhost/", True, False, False),
])
def test_database_guard(url, production, pilot, ok):
    if ok:
        cli.check_database(url, production=production, pilot=pilot)
    else:
        with pytest.raises(SystemExit):
            cli.check_database(url, production=production, pilot=pilot)


def test_forbidden_url_fails_before_importing_db(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("connected")
    monkeypatch.setattr("sqlalchemy.create_engine", boom)
    with pytest.raises(SystemExit, match="--production"):
        cli.main(["--database-url", "postgresql+psycopg://localhost/rainbox_production",
                  "list"])


@pytest.fixture
def workspace(tmp_path):
    import db

    root = tmp_path / "input"
    shutil.copytree(FIXTURE, root, ignore=shutil.ignore_patterns("README.md"))
    app = db.make_app()
    db.init_db(app)
    room = uuid.uuid4()
    name = f"cli-test-{uuid.uuid4().hex[:12]}"
    with app.app_context():
        db.session.add(db.Chatroom(uuid=room, name="diary-cli", created_by=uuid.uuid4()))
        db.session.commit()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "schema_version": 1, "name": name, "root": str(root.resolve()),
        "room_uuid": str(room), "timezone": "Europe/Copenhagen",
        "dialect_rules": [{"prefix": "current/", "dialect": "timed"},
                          {"prefix": "daily/", "dialect": "daily"},
                          {"prefix": "archive/", "dialect": "changelog"}],
        "month_languages": ["en", "da", "de"],
    }))
    try:
        yield manifest, name
    finally:
        with app.app_context():
            src = db.session.query(db.DiarySource).filter_by(name=name).one_or_none()
            if src is not None:
                db.session.execute(sa.update(db.DiaryFile).where(db.DiaryFile.source_uuid == src.uuid)
                                   .values(current_generation_uuid=None))
                db.session.execute(sa.delete(db.DiarySource).where(db.DiarySource.uuid == src.uuid))
            db.session.execute(sa.delete(db.Chatroom).where(db.Chatroom.uuid == room))
            db.session.commit()


def call(capsys, *argv):
    assert cli.main(["--database-url", SANDBOX_URL, *argv]) == 0
    return json.loads(capsys.readouterr().out)


def test_register_parse_sync_show(workspace, capsys):
    manifest, name = workspace
    reg = call(capsys, "register", "--manifest", str(manifest))
    assert reg["created"] and reg["enabled"] is False
    dry = call(capsys, "parse", "--source", name, "--dry-run")
    assert dry["totals"]["quarantined"] == 0 and len(dry["files"]) == 3
    assert all("text" not in f for f in dry["files"])
    synced = call(capsys, "sync", "--source", name)
    assert synced["published"] == 3
    import db
    app = db.make_app()
    with app.app_context():
        p = db.session.query(db.DiaryPassage).filter(db.DiaryPassage.text.like("%Physiotherapy%")).first()
        locator = f"diary:{p.cite_revision_uuid}:{p.byte_start}-{p.byte_end}"
    shown = call(capsys, "show", "--citation", locator)
    assert "Physiotherapy" in shown["text"] and shown["current_revision"] is True
    listed = call(capsys, "list")
    assert any(s["name"] == name and s["files"] == 3 for s in listed)


def test_reset_pilot_requires_marker(workspace, capsys):
    manifest, name = workspace
    call(capsys, "register", "--manifest", str(manifest))
    with pytest.raises(SystemExit, match="pilot marker"):
        cli.main(["--database-url", SANDBOX_URL, "reset-pilot", "--source", name])


def test_query_prints_the_observation(workspace, capsys):
    manifest, name = workspace
    call(capsys, "register", "--manifest", str(manifest))
    call(capsys, "sync", "--source", name)
    assert cli.main(["--database-url", SANDBOX_URL, "query", "--source", name,
                     "literal", "Edit::VSpace#move_left"]) == 0
    out = capsys.readouterr().out
    assert "Edit::VSpace#move_left" in out and "<diary_passages" in out
    assert cli.main(["--database-url", SANDBOX_URL, "query", "--source", name,
                     "timeline", "--from", "2027-07-26"]) == 0
    assert "re-enabled Buffer" in capsys.readouterr().out
