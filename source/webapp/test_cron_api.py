"""Tests for the cron persistence backend (db.py models/helpers + cron_api).

Uses the live local Postgres. The `cron_tree_snapshot` fixture saves the cron
tables before each test and restores them after, so tests are non-destructive.
"""

from uuid import UUID, uuid4

import pytest

import db
from db import CronFolder, CronJob, CronRun  # noqa: F401  (CronRun import smoke-checks the model)


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    try:
        yield app
    finally:
        ctx.pop()


def test_message_target_must_be_a_chatroom_uuid(app_ctx):
    # A non-uuid (legacy name) message target is rejected; empty is allowed.
    with pytest.raises(db.CronTreeError):
        db.validate_cron_tree([], [{
            "uuid": str(uuid4()), "name": "M", "type": "message",
            "cron": "* * * * *", "target": "#dev", "message": "hi",
        }])
    db.validate_cron_tree([], [{  # uuid target + empty target both fine
        "uuid": str(uuid4()), "name": "Ok", "type": "message",
        "cron": "* * * * *", "target": str(uuid4()), "message": "hi",
    }, {
        "uuid": str(uuid4()), "name": "Empty", "type": "message",
        "cron": "* * * * *", "target": "", "message": "hi",
    }])


def test_one_shot_message_job_allows_empty_cron(app_ctx):
    # Reminders create one-shot 'message' jobs with an empty cron_expr — they fire
    # once at next_run_at and retire. Re-saving the tree (e.g. toggling Active on
    # such a job) must not reject "" as "cron expression must have 5 fields".
    db.validate_cron_tree([], [{
        "uuid": str(uuid4()), "name": "Reminder", "type": "message",
        "cron": "", "target": str(uuid4()), "message": "hi",
    }])


def test_cron_load_tree_includes_chatrooms(app_ctx):
    out = db.cron_load_tree()
    assert isinstance(out.get("chatrooms"), list)
    # The cron room is seeded, so there's at least one {uuid, name} entry.
    assert out["chatrooms"] and all(
        "uuid" in c and "name" in c for c in out["chatrooms"])


def test_migrate_message_target_name_to_uuid(app_ctx):
    s = db.db.session
    room_uuid, ju_known, ju_ghost = uuid4(), uuid4(), uuid4()
    s.add(db.Chatroom(uuid=room_uuid, name="MigRoom", created_by=uuid4()))
    s.add(db.CronJob(uuid=ju_known, name="known", action_type="message", target="MigRoom"))
    s.add(db.CronJob(uuid=ju_ghost, name="ghost", action_type="message", target="nope"))
    s.commit()
    try:
        db._migrate_cron_message_targets()
        assert s.query(db.CronJob).filter_by(uuid=ju_known).one().target == str(room_uuid)
        assert s.query(db.CronJob).filter_by(uuid=ju_ghost).one().target == ""  # unknown -> cleared
    finally:
        s.query(db.CronJob).filter(db.CronJob.uuid.in_([ju_known, ju_ghost])).delete(
            synchronize_session=False)
        s.query(db.Chatroom).filter_by(uuid=room_uuid).delete()
        s.commit()


@pytest.fixture
def cron_tree_snapshot(app_ctx):
    """Snapshot the cron tables via the ORM, yield, then restore them, so a
    test that wipes/replaces the cron tree leaves the DB as it found it. Uses
    the ORM directly (not cron_load_tree/save) so it works from Task 1 on."""
    import sqlalchemy as sa

    def grab(model):
        rows = db.db.session.execute(sa.select(model)).scalars().all()
        return [
            {c.name: getattr(r, c.name) for c in model.__table__.columns if c.name != "id"}
            for r in rows
        ]

    fsnap, jsnap = grab(CronFolder), grab(CronJob)
    try:
        yield
    finally:
        db.db.session.execute(sa.delete(CronJob))
        db.db.session.execute(sa.delete(CronFolder))
        for row in fsnap:
            db.db.session.add(CronFolder(**row))
        for row in jsnap:
            db.db.session.add(CronJob(**row))
        db.db.session.commit()


def test_cron_models_round_trip(app_ctx, cron_tree_snapshot):
    import sqlalchemy as sa

    fu, ju = uuid4(), uuid4()
    db.db.session.execute(sa.delete(CronJob))
    db.db.session.execute(sa.delete(CronFolder))
    db.db.session.add(CronFolder(uuid=fu, name="T-folder", parent_uuid=None, enabled=True, position=0))
    db.db.session.add(CronJob(
        uuid=ju, name="T-job", enabled=False, folder_uuid=fu,
        cron_expr="*/5 * * * *", action_type="command", target="", message="",
        command="echo hi", description="d", position=0,
    ))
    db.db.session.commit()

    f = db.db.session.get(CronFolder, db.db.session.execute(
        sa.select(CronFolder.id).where(CronFolder.uuid == fu)).scalar_one())
    j = db.db.session.execute(sa.select(CronJob).where(CronJob.uuid == ju)).scalar_one()
    assert f.name == "T-folder" and f.enabled is True
    assert j.action_type == "command" and j.enabled is False and j.folder_uuid == fu


def test_cron_save_and_load_tree(app_ctx, cron_tree_snapshot):
    f_root, f_child, j1 = str(uuid4()), str(uuid4()), str(uuid4())
    room_uuid = str(uuid4())  # message target is a chatroom uuid (rename-proof)
    folders = [
        {"id": f_root, "name": "Root", "description": "top-level notes",
         "parentId": None, "enabled": True},
        {"id": f_child, "name": "Child", "parentId": f_root, "enabled": False},
    ]
    jobs = [
        {"uuid": j1, "name": "J1", "enabled": True, "folderId": f_child,
         "cron": "0 9 * * 1", "timezone": "UTC", "type": "message", "target": room_uuid,
         "message": "hi", "command": "", "description": "note"},
    ]
    db.cron_save_tree(folders, jobs)
    out = db.cron_load_tree()

    assert [f["name"] for f in out["folders"]] == ["Root", "Child"]   # order preserved
    assert out["folders"][0]["description"] == "top-level notes"      # folder notes round-trip
    assert out["folders"][0]["created_at"] and out["folders"][0]["updated_at"]
    assert out["folders"][1]["parentId"] == f_root
    assert out["folders"][1]["enabled"] is False
    assert len(out["jobs"]) == 1
    job = out["jobs"][0]
    assert job["uuid"] == j1 and job["folderId"] == f_child
    assert job["cron"] == "0 9 * * 1" and job["type"] == "message"
    assert job["target"] == room_uuid and job["description"] == "note"
    assert job["timezone"] == "UTC"   # timezone choice round-trips

    # An unspecified timezone defaults to localtime.
    db.cron_save_tree([], [
        {"uuid": str(uuid4()), "name": "noTz", "folderId": None,
         "cron": "* * * * *", "type": "command", "command": "ls"}])
    assert db.cron_load_tree()["jobs"][0]["timezone"] == "localtime"


def test_toggling_active_on_one_shot_reminder_persists(app_ctx, cron_tree_snapshot):
    # The user's flow: a reminder is a one-shot 'message' job with an empty cron.
    # Deselect Active, save, reload — it must stay inactive (not snap back to
    # Active), and its pre-set fire time must survive the save.
    from datetime import UTC, datetime, timedelta

    job = db.cron_create_one_shot_message(
        message="⏰ Reminder: brush teeth",
        fire_at=datetime.now(UTC) + timedelta(hours=1),
    )
    tree = db.cron_load_tree()
    row = next(j for j in tree["jobs"] if j["uuid"] == str(job.uuid))
    assert row["enabled"] is True and row["cron"] == ""
    row["enabled"] = False
    db.cron_save_tree(tree["folders"], tree["jobs"])  # must not raise on empty cron

    saved = next(j for j in db.cron_load_tree()["jobs"] if j["uuid"] == str(job.uuid))
    assert saved["enabled"] is False         # the toggle persisted
    assert saved["cron"] == ""               # still a one-shot
    assert saved["next_run_at"] is not None  # fire time survived the save


def test_cron_save_tree_replaces(app_ctx, cron_tree_snapshot):
    db.cron_save_tree([{"id": str(uuid4()), "name": "A", "parentId": None, "enabled": True}], [])
    db.cron_save_tree([{"id": str(uuid4()), "name": "B", "parentId": None, "enabled": True}], [])
    out = db.cron_load_tree()
    assert [f["name"] for f in out["folders"]] == ["B"]  # replace, not append


def test_cron_save_tree_preserves_created_at(app_ctx, cron_tree_snapshot):
    """The save upserts by uuid: an unchanged-uuid job keeps its created_at
    across saves (the timestamps shown on the Job details page stay meaningful)
    and updated_at moves forward when the job actually changes."""
    import sqlalchemy as sa

    ju = str(uuid4())
    job = {"uuid": ju, "name": "Orig", "enabled": True, "folderId": None,
           "cron": "* * * * *", "type": "command", "target": "", "message": "",
           "command": "ls", "description": ""}
    db.cron_save_tree([], [job])
    out1 = db.cron_load_tree()["jobs"][0]
    assert out1["created_at"] and out1["updated_at"]

    # Pin updated_at to a known past value, then save again with a changed name
    # (same uuid) and confirm onupdate moved it forward.
    from datetime import UTC, datetime

    old = datetime(2000, 1, 1, tzinfo=UTC)
    db.db.session.execute(
        sa.update(CronJob).where(CronJob.uuid == UUID(ju)).values(updated_at=old)
    )
    db.db.session.commit()
    job["name"] = "Renamed"
    db.cron_save_tree([], [job])
    out2 = db.cron_load_tree()["jobs"][0]

    assert out2["name"] == "Renamed"
    assert out2["created_at"] == out1["created_at"]      # creation date preserved
    assert out2["updated_at"] != old.isoformat()         # onupdate refreshed it


def test_validate_cron_tree_accepts_valid(app_ctx):
    fu, ju = str(uuid4()), str(uuid4())
    db.validate_cron_tree(
        [{"id": fu, "name": "F", "parentId": None, "enabled": True}],
        [{"uuid": ju, "name": "J", "folderId": fu, "cron": "*/5 9 * * 1",
          "type": "command", "command": "ls"}],
    )  # does not raise


@pytest.mark.parametrize("folders, jobs, needle", [
    # payload shape — must be lists of objects, not a string/scalar/etc.
    ("bad", [], "'folders' must be a list"),
    ([], "bad", "'jobs' must be a list"),
    (["not-an-object"], [], "folder entry must be an object"),
    ([], ["not-an-object"], "job entry must be an object"),
    ([{"id": "not-a-uuid", "name": "F", "parentId": None}], [], "not a uuid"),
    ([{"id": str(uuid4()), "name": "F", "parentId": "nope"}], [], "is not a uuid"),
    # dangling parent reference
    ([{"id": str(uuid4()), "name": "F", "parentId": str(uuid4())}], [], "missing parent"),
    # job points at an unknown folder
    ([], [{"uuid": str(uuid4()), "name": "J", "folderId": str(uuid4()),
           "cron": "* * * * *", "type": "command"}], "missing folder"),
    # bad action type
    ([], [{"uuid": str(uuid4()), "name": "J", "folderId": None,
           "cron": "* * * * *", "type": "carrier-pigeon"}], "unknown type"),
    # malformed cron (4 fields)
    ([], [{"uuid": str(uuid4()), "name": "J", "folderId": None,
           "cron": "* * * *", "type": "message"}], "5 fields"),
    # unknown timezone
    ([], [{"uuid": str(uuid4()), "name": "J", "folderId": None,
           "cron": "* * * * *", "type": "message", "timezone": "PST"}], "unknown timezone"),
])
def test_validate_cron_tree_rejects(app_ctx, folders, jobs, needle):
    with pytest.raises(db.CronTreeError) as exc:
        db.validate_cron_tree(folders, jobs)
    assert needle in str(exc.value)


def test_validate_cron_tree_rejects_cross_kind_uuid_collision(app_ctx):
    """A folder and a job may not share a uuid — a node is identified globally
    (e.g. /cron?id=<uuid>), so a collision would make that deep link ambiguous."""
    shared = str(uuid4())
    with pytest.raises(db.CronTreeError) as exc:
        db.validate_cron_tree(
            [{"id": shared, "name": "F", "parentId": None}],
            [{"uuid": shared, "name": "J", "folderId": None,
              "cron": "* * * * *", "type": "message"}])
    assert "collides with a folder id" in str(exc.value)


def test_validate_cron_tree_rejects_case_variant_duplicate(app_ctx):
    """The same uuid in upper- vs lower-case is one identity — caught here as a
    duplicate, not later by the DB unique constraint (which would 500)."""
    base = str(uuid4())
    with pytest.raises(db.CronTreeError) as exc:
        db.validate_cron_tree(
            [{"id": base.lower(), "name": "A", "parentId": None},
             {"id": base.upper(), "name": "B", "parentId": None}], [])
    assert "duplicate folder id" in str(exc.value)


def test_validate_cron_tree_rejects_self_parent_cycle(app_ctx):
    fu = str(uuid4())
    with pytest.raises(db.CronTreeError) as exc:
        db.validate_cron_tree([{"id": fu, "name": "F", "parentId": fu}], [])
    assert "cycle" in str(exc.value)


def test_validate_cron_tree_rejects_two_node_cycle(app_ctx):
    a, b = str(uuid4()), str(uuid4())
    with pytest.raises(db.CronTreeError) as exc:
        db.validate_cron_tree(
            [{"id": a, "name": "A", "parentId": b},
             {"id": b, "name": "B", "parentId": a}], [])
    assert "cycle" in str(exc.value)


def test_cron_api_put_rejects_invalid_tree(app_ctx, cron_tree_snapshot):
    """A malformed PUT is rejected with 400 and leaves the tree untouched."""
    from webapp.core import app as flask_app

    client = flask_app.test_client()
    got = client.get("/cron/api/tree").get_json()
    a, b = str(uuid4()), str(uuid4())
    body = {  # mutual parent cycle
        "folders": [{"id": a, "name": "A", "parentId": b, "enabled": True},
                    {"id": b, "name": "B", "parentId": a, "enabled": True}],
        "jobs": [],
        "version": got["version"],
    }
    resp = client.put("/cron/api/tree", json=body)
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False and "cycle" in resp.get_json()["error"]
    # Nothing from the rejected payload persisted.
    ids = {f["id"] for f in db.cron_load_tree()["folders"]}
    assert a not in ids and b not in ids


@pytest.mark.parametrize("body, code", [
    ({"folders": "bad", "jobs": []}, 400),        # folders not a list
    ({"folders": [], "jobs": "bad"}, 400),        # jobs not a list
    ([1, 2, 3], 400),                             # root is not an object
    ({}, 400),                                    # missing version token
    ({"version": 5}, 400),                        # version not a string
    ({"version": "x", "deletes": -1}, 400),       # deletes negative
    ({"version": "x", "deletes": "1"}, 400),      # deletes not an int
])
def test_cron_api_put_malformed_payload(app_ctx, cron_tree_snapshot, body, code):
    """Malformed bodies are 400s, never 500s. A bare {} is no longer a 'valid
    wipe': every PUT must carry the version token it hydrated with, and
    deletions must be declared via a non-negative int 'deletes'."""
    from webapp.core import app as flask_app

    client = flask_app.test_client()
    if isinstance(body, dict) and "folders" in body:
        # Shape-validation cases need a real version to get past the token check.
        body = {**body, "version": client.get("/cron/api/tree").get_json()["version"]}
    resp = client.put("/cron/api/tree", json=body)
    assert resp.status_code == code
    if code == 400:
        assert resp.get_json()["ok"] is False


def test_cron_api_tree_round_trip(app_ctx, cron_tree_snapshot):
    from webapp.core import app as flask_app

    client = flask_app.test_client()
    before = client.get("/cron/api/tree").get_json()
    fu, ju = str(uuid4()), str(uuid4())
    body = {
        "folders": [{"id": fu, "name": "ApiFolder", "parentId": None, "enabled": True}],
        "jobs": [{"uuid": ju, "name": "ApiJob", "enabled": True, "folderId": fu,
                  "cron": "* * * * *", "type": "command", "target": "", "message": "",
                  "command": "ls", "description": ""}],
        "version": before["version"],
        # Replacing the whole tree deletes every pre-existing row — declare it.
        "deletes": len(before["folders"]) + len(before["jobs"]),
    }
    put = client.put("/cron/api/tree", json=body)
    assert put.status_code == 200 and put.get_json()["ok"] is True
    # A successful save returns the new version token for the next PUT.
    assert isinstance(put.get_json()["version"], str) and put.get_json()["version"]

    got = client.get("/cron/api/tree").get_json()
    assert [f["name"] for f in got["folders"]] == ["ApiFolder"]
    assert got["jobs"][0]["uuid"] == ju and got["jobs"][0]["command"] == "ls"
    assert got["version"] == put.get_json()["version"]


def test_cron_api_put_stale_version_conflicts(app_ctx, cron_tree_snapshot):
    """A PUT carrying a version older than the current tree is a 409 and
    persists nothing — a second tab can't clobber another writer's changes."""
    from webapp.core import app as flask_app

    client = flask_app.test_client()
    got = client.get("/cron/api/tree").get_json()
    # Another writer (second tab / agent) adds a folder after our hydrate.
    other = str(uuid4())
    db.cron_save_tree(
        got["folders"] + [{"id": other, "name": "OtherTab", "parentId": None, "enabled": True}],
        got["jobs"])
    # Our save, based on the stale hydrate (which omits OtherTab), is refused.
    mine = str(uuid4())
    resp = client.put("/cron/api/tree", json={
        "folders": got["folders"] + [{"id": mine, "name": "Mine", "parentId": None, "enabled": True}],
        "jobs": got["jobs"], "version": got["version"], "deletes": 0,
    })
    assert resp.status_code == 409
    assert resp.get_json()["ok"] is False
    # The response carries the current version so the client can re-hydrate.
    assert isinstance(resp.get_json()["version"], str)
    ids = {f["id"] for f in db.cron_load_tree()["folders"]}
    assert other in ids and mine not in ids  # their write survived; ours refused


def test_cron_api_put_undeclared_delete_refused(app_ctx, cron_tree_snapshot):
    """A save that would delete rows beyond the declared 'deletes' count (e.g.
    a truncated payload from a frontend bug) is refused; declaring the
    deletions lets the same payload through."""
    from webapp.core import app as flask_app

    client = flask_app.test_client()
    got = client.get("/cron/api/tree").get_json()
    n = len(got["folders"]) + len(got["jobs"])
    assert n >= 2  # the seeded System folder + backup job at minimum

    # Undeclared (deletes defaults to 0): refused, tree untouched.
    resp = client.put("/cron/api/tree",
                      json={"folders": [], "jobs": [], "version": got["version"]})
    assert resp.status_code == 400 and "delete" in resp.get_json()["error"]
    after = client.get("/cron/api/tree").get_json()
    assert len(after["folders"]) + len(after["jobs"]) == n

    # Declared: the same wipe is allowed.
    resp = client.put("/cron/api/tree", json={
        "folders": [], "jobs": [], "version": after["version"], "deletes": n})
    assert resp.status_code == 200
    final = client.get("/cron/api/tree").get_json()
    assert final["folders"] == [] and final["jobs"] == []


def test_cron_save_tree_populates_next_run_at(app_ctx, cron_tree_snapshot):
    """Saving a job computes its next_run_at so the scheduler can fire it."""
    import sqlalchemy as sa

    ju = str(uuid4())
    db.cron_save_tree([], [{
        "uuid": ju, "name": "J", "folderId": None, "cron": "*/5 * * * *",
        "timezone": "UTC", "type": "command", "command": "ls",
    }])
    job = db.db.session.execute(
        sa.select(db.CronJob).where(db.CronJob.uuid == UUID(ju))).scalar_one()
    assert job.next_run_at is not None


def test_one_shot_message_stores_origin(app_ctx, cron_tree_snapshot):
    from datetime import UTC, datetime, timedelta
    run, step = uuid4(), uuid4()
    job = db.cron_create_one_shot_message(
        message="⏰ Reminder: x", fire_at=datetime.now(UTC) + timedelta(hours=1),
        origin_run_uuid=run, origin_step_uuid=step)
    fetched = db.db.session.get(db.CronJob, job.id)
    assert fetched.origin_run_uuid == run
    assert fetched.origin_step_uuid == step


def test_one_shot_message_origin_defaults_null(app_ctx, cron_tree_snapshot):
    from datetime import UTC, datetime, timedelta
    job = db.cron_create_one_shot_message(
        message="⏰ Reminder: y", fire_at=datetime.now(UTC) + timedelta(hours=1))
    fetched = db.db.session.get(db.CronJob, job.id)
    assert fetched.origin_run_uuid is None and fetched.origin_step_uuid is None


def test_cron_load_tree_exposes_origin_step_link(app_ctx, cron_tree_snapshot):
    from datetime import UTC, datetime, timedelta
    run, step = uuid4(), uuid4()
    job = db.cron_create_one_shot_message(
        message="⏰ Reminder: z", fire_at=datetime.now(UTC) + timedelta(hours=1),
        origin_run_uuid=run, origin_step_uuid=step)
    out = db.cron_load_tree()
    row = next(j for j in out["jobs"] if j["uuid"] == str(job.uuid))
    assert row["origin_step_link"] == db.assistant_step_path(run, step)

    plain = db.cron_create_one_shot_message(
        message="⏰ Reminder: nolink", fire_at=datetime.now(UTC) + timedelta(hours=1))
    out2 = db.cron_load_tree()
    prow = next(j for j in out2["jobs"] if j["uuid"] == str(plain.uuid))
    assert prow["origin_step_link"] is None
