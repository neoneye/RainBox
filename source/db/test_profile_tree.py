"""Tests for the person-profile tree persistence + data validation (db.profile,
profile_fields registry, the shipped built-in templates)."""
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

import db
import profile_fields
from db.models import Profile, ProfileFolder


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


def test_registry_shape():
    keys = [f.key for f in profile_fields.PROFILE_FIELDS]
    assert len(keys) == len(set(keys)) == 20
    assert keys[0] == "full_name"
    assert profile_fields.FIELD_GROUPS == [
        "Identity", "Locale & formats", "Contact & location"]
    for f in profile_fields.PROFILE_FIELDS:
        assert f.kind in {"text", "enum", "date", "email"}
        if f.kind == "enum":
            assert f.choices
        else:
            assert f.choices == ()
    for k in profile_fields.SUMMARY_KEYS:
        assert k in profile_fields.FIELDS_BY_KEY


def test_profile_models_round_trip(app_ctx):
    fu, pu = uuid4(), uuid4()
    db.db.session.add(ProfileFolder(uuid=fu, name="T-folder", parent_uuid=None, position=0))
    db.db.session.add(Profile(uuid=pu, name="T-profile", folder_uuid=fu, position=0,
                              data={"full_name": "Ada Test", "units": "metric"}))
    db.db.session.commit()
    try:
        f = db.db.session.execute(sa.select(ProfileFolder).where(ProfileFolder.uuid == fu)).scalar_one()
        p = db.db.session.execute(sa.select(Profile).where(Profile.uuid == pu)).scalar_one()
        assert f.name == "T-folder" and f.parent_uuid is None
        assert p.data == {"full_name": "Ada Test", "units": "metric"}
        assert p.folder_uuid == fu
        assert f.created_at and p.updated_at  # timestamp defaults fire
    finally:
        db.db.session.execute(sa.delete(Profile).where(Profile.uuid == pu))
        db.db.session.execute(sa.delete(ProfileFolder).where(ProfileFolder.uuid == fu))
        db.db.session.commit()


def test_validate_data_canonical_and_errors():
    ok = db.validate_profile_data({
        "full_name": "Jacobus van 't Hoff", "units": "metric",
        "birthday": "1987-08-30", "address": "Line one\nLine two",
        "timezone": "", "email": "x@example.com"})
    assert ok == {"full_name": "Jacobus van 't Hoff", "units": "metric",
                  "birthday": "1987-08-30", "address": "Line one\nLine two",
                  "email": "x@example.com"}          # "" canonicalized away
    assert db.validate_profile_data({}) == {}        # sparse blob valid
    with pytest.raises(db.ProfileDataError, match="no_such"):
        db.validate_profile_data({"no_such": "x"})
    with pytest.raises(db.ProfileDataError, match="no_such"):
        db.validate_profile_data({"no_such": ""})    # unknown stays rejected when empty
    with pytest.raises(db.ProfileDataError, match="units"):
        db.validate_profile_data({"units": "furlongs"})
    with pytest.raises(db.ProfileDataError, match="birthday"):
        db.validate_profile_data({"birthday": "2026-02-30"})
    with pytest.raises(db.ProfileDataError, match="birthday"):
        db.validate_profile_data({"birthday": "07/14/2026"})
    with pytest.raises(db.ProfileDataError, match="dynamic"):
        db.validate_profile_data({"dynamic": {}})    # connector-owned, read-only
    with pytest.raises(db.ProfileDataError, match="full_name"):
        db.validate_profile_data({"full_name": 5})
    with pytest.raises(db.ProfileDataError):
        db.validate_profile_data(["not", "a", "dict"])


@pytest.fixture
def profile_tree_snapshot(app_ctx):
    """Snapshot the profile tables, yield, then restore — non-destructive."""
    def grab(model):
        rows = db.db.session.execute(sa.select(model)).scalars().all()
        return [
            {c.name: getattr(r, c.name) for c in model.__table__.columns if c.name != "id"}
            for r in rows
        ]
    fsnap, psnap = grab(ProfileFolder), grab(Profile)
    try:
        yield
    finally:
        db.db.session.execute(sa.delete(Profile))
        db.db.session.execute(sa.delete(ProfileFolder))
        for row in fsnap:
            db.db.session.add(ProfileFolder(**row))
        for row in psnap:
            db.db.session.add(Profile(**row))
        db.db.session.commit()


@pytest.fixture
def empty_tree(profile_tree_snapshot):
    db.db.session.execute(sa.delete(Profile))
    db.db.session.execute(sa.delete(ProfileFolder))
    db.db.session.commit()


def test_save_and_load_roundtrip(app_ctx, empty_tree):
    f_root, f_child, pr = str(uuid4()), str(uuid4()), str(uuid4())
    db.profile_save_tree(
        [{"id": f_root, "name": "Friends", "description": "top", "parentId": None},
         {"id": f_child, "name": "Copenhagen", "parentId": f_root}],
        [{"uuid": pr, "name": "Simon", "folderId": f_child}])
    out = db.profile_load_tree()
    user_folders = [f for f in out["folders"] if not f.get("builtin")]
    user_profiles = [p for p in out["profiles"] if not p.get("builtin")]
    assert [f["name"] for f in user_folders] == ["Friends", "Copenhagen"]
    assert user_folders[1]["parentId"] == f_root
    assert user_profiles[0]["folderId"] == f_child
    assert "data" not in user_profiles[0]           # blob stays out of the tree payload
    assert set(user_profiles[0]["summary"]) == set(profile_fields.SUMMARY_KEYS)
    assert out["version"]


def test_tree_save_preserves_data(app_ctx, empty_tree):
    pr = str(uuid4())
    db.profile_save_tree([], [{"uuid": pr, "name": "P", "folderId": None}])
    row = db.db.session.execute(sa.select(Profile).where(Profile.uuid == UUID(pr))).scalar_one()
    row.data = {"full_name": "Keep Me"}
    db.db.session.commit()
    # A structural save (rename) must not touch data.
    db.profile_save_tree([], [{"uuid": pr, "name": "P renamed", "folderId": None}])
    row = db.db.session.execute(sa.select(Profile).where(Profile.uuid == UUID(pr))).scalar_one()
    assert row.name == "P renamed" and row.data == {"full_name": "Keep Me"}


def test_version_conflict(app_ctx, profile_tree_snapshot):
    with pytest.raises(db.ProfileTreeConflict):
        db.profile_save_tree([], [], base_version="stale-token-xyz")


def test_delete_tripwire(app_ctx, empty_tree):
    f = str(uuid4())
    db.profile_save_tree([{"id": f, "name": "F", "parentId": None}], [])
    # Saving an empty tree would delete the folder; undeclared deletion → refused.
    with pytest.raises(db.ProfileTreeError):
        db.profile_save_tree([], [], expected_deletes=0)


def test_validate_rejects_dangling_cycle_collision_summary(app_ctx):
    with pytest.raises(db.ProfileTreeError):
        db.validate_profile_tree([], [{"uuid": str(uuid4()), "name": "P",
                                       "folderId": str(uuid4())}])
    a, b = str(uuid4()), str(uuid4())
    with pytest.raises(db.ProfileTreeError):
        db.validate_profile_tree([{"id": a, "name": "A", "parentId": b},
                                  {"id": b, "name": "B", "parentId": a}], [])
    shared = str(uuid4())
    with pytest.raises(db.ProfileTreeError):
        db.validate_profile_tree([{"id": shared, "name": "F", "parentId": None}],
                                 [{"uuid": shared, "name": "P", "folderId": None}])
    with pytest.raises(db.ProfileTreeError, match="summary"):
        db.validate_profile_tree([], [{"uuid": str(uuid4()), "name": "P",
                                       "folderId": None, "summary": {}}])


def test_builtins_merged_into_tree(app_ctx, empty_tree):
    out = db.profile_load_tree()
    tf = str(db.profile_templates_folder_uuid())
    builtin_folders = [f for f in out["folders"] if f.get("builtin")]
    assert [f["id"] for f in builtin_folders] == [tf]
    builtins = [p for p in out["profiles"] if p.get("builtin")]
    assert len(builtins) == 21
    assert all(p["folderId"] == tf for p in builtins)
    assert builtins[0]["name"] == "US" and builtins[-1]["name"] == "Australia"
    assert builtins[6]["summary"]["full_name"] == "Karl Weierstraß"
    assert "data" not in builtins[0]


def test_builtins_excluded_from_version(app_ctx, empty_tree):
    out = db.profile_load_tree()
    assert len(out["profiles"]) == 21 and len(out["folders"]) == 1  # virtual rows only
    # The version token covers user rows only, so a builtin-free save of the
    # (empty) user tree against it is a clean no-op — nothing stale, nothing
    # to delete.
    db.profile_save_tree([], [], base_version=out["version"], expected_deletes=0)


def test_tree_put_rejects_builtin_uuids(app_ctx):
    tf = str(db.profile_templates_folder_uuid())
    bp = str(next(iter(db.profile_builtin_uuids() - {db.profile_templates_folder_uuid()})))
    with pytest.raises(db.ProfileTreeError, match="built-in"):
        db.validate_profile_tree([{"id": tf, "name": "Templates", "parentId": None}], [])
    with pytest.raises(db.ProfileTreeError, match="built-in"):
        db.validate_profile_tree([], [{"uuid": bp, "name": "X", "folderId": None}])


def test_all_templates_validate(app_ctx):
    entries = db.profile_templates_entries()
    assert len(entries) == 21
    for e in entries:
        canonical = db.validate_profile_data(e["data"])
        assert canonical == e["data"]        # shipped data is already canonical (no "" values)
        assert e["data"]["country"] == e["name"]
        assert "handle" not in e["data"] and "email" not in e["data"]


def test_all_templates_carry_number_format(app_ctx):
    """Every built-in template declares an explicit number_format, and the
    regional assignments match the shipped conventions (fr-CA Canada groups
    with space, India uses Indian grouping, the apostrophe variant ships as
    an operator-selectable convention with no template)."""
    by_name = {e["name"]: e["data"] for e in db.profile_templates_entries()}
    assert all("number_format" in d for d in by_name.values())
    assigned = {
        "1,234,567.89": {"US", "Mexico", "UK", "Israel", "China", "Japan",
                         "South Korea", "Singapore", "Australia"},
        "1.234.567,89": {"Brazil", "Germany", "Netherlands", "Spain",
                         "Italy", "Denmark"},
        "1 234 567,89": {"Canada", "France", "Sweden", "Norway", "Poland"},
        "12,34,567.89": {"India"},
    }
    for value, names in assigned.items():
        assert {n for n, d in by_name.items() if d["number_format"] == value} == names
    assert not any(d["number_format"] == "1'234'567.89" for d in by_name.values())


def test_update_data_merges_and_deletes(app_ctx, empty_tree):
    pr = str(uuid4())
    db.profile_save_tree([], [{"uuid": pr, "name": "P", "folderId": None}])
    v = db.profile_tree_version()
    dynamic = {"location": {"value": "Copenhagen", "seen_at": "2026-07-14T10:00:00+00:00"}}
    row = db.db.session.execute(sa.select(Profile).where(Profile.uuid == UUID(pr))).scalar_one()
    row.data = {"full_name": "Old Name", "city": "Aarhus", "dynamic": dynamic}
    db.db.session.commit()
    summary = db.profile_update_data(UUID(pr), {"full_name": "New Name", "units": "metric"})
    assert summary["full_name"] == "New Name"
    stored = db.profile_get(UUID(pr))["data"]
    assert stored["dynamic"] == dynamic            # observation survives byte-for-byte
    assert "city" not in stored                    # omitted editable key deleted, not retained
    assert stored["full_name"] == "New Name" and stored["units"] == "metric"
    assert db.profile_tree_version() == v          # data excluded from the structural version
    tree_row = [p for p in db.profile_load_tree()["profiles"] if p["uuid"] == pr][0]
    assert tree_row["summary"]["full_name"] == "New Name"   # summary rides, version stable
    assert db.profile_update_data(uuid4(), {}) is None


def test_duplicate_user_owned(app_ctx, empty_tree):
    f, src, other = str(uuid4()), str(uuid4()), str(uuid4())
    db.profile_save_tree([{"id": f, "name": "F", "parentId": None}],
                         [{"uuid": src, "name": "Simon", "folderId": f},
                          {"uuid": other, "name": "After", "folderId": f}])
    blob = {"full_name": "Simon S", "dynamic": {"screen": {"value": "3440x1440",
                                                           "seen_at": "2026-07-01T00:00:00+00:00"}}}
    row = db.db.session.execute(sa.select(Profile).where(Profile.uuid == UUID(src))).scalar_one()
    row.data = blob
    db.db.session.commit()
    dup = db.profile_duplicate(UUID(src))
    assert dup["name"] == "Simon copy" and dup["folderId"] == f
    got = db.profile_get(UUID(dup["uuid"]))
    assert got["data"] == blob and got["data"] is not blob   # deep copy, dynamic included
    order = [p["uuid"] for p in db.profile_load_tree()["profiles"] if not p.get("builtin")]
    assert order.index(dup["uuid"]) == order.index(src) + 1
    assert db.profile_duplicate(uuid4()) is None


def test_duplicate_builtin(app_ctx, empty_tree):
    pr = str(uuid4())
    db.profile_save_tree([], [{"uuid": pr, "name": "Existing", "folderId": None}])
    germany = next(e for e in db.profile_templates_entries() if e["name"] == "Germany")
    dup = db.profile_duplicate(UUID(germany["uuid"]))
    assert dup["name"] == "Germany" and dup["folderId"] is None
    got = db.profile_get(UUID(dup["uuid"]))
    assert got["builtin"] is False and got["data"] == germany["data"]   # real, editable row
    roots = [p for p in db.profile_load_tree()["profiles"]
             if not p.get("builtin") and p["folderId"] is None]
    assert roots[-1]["uuid"] == dup["uuid"]        # end of the user-owned top level


def test_get_builtin_and_unknown(app_ctx):
    germany = next(e for e in db.profile_templates_entries() if e["name"] == "Germany")
    got = db.profile_get(UUID(germany["uuid"]))
    assert got["builtin"] is True and got["data"]["full_name"] == "Karl Weierstraß"
    assert db.profile_get(uuid4()) is None
