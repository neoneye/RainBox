"""Tests for chat folders: schema, tree load/validate/save, recursive delete.

Uses the live local Postgres (rainbox_claude via conftest). Each test cleans up
the rows it creates so artifacts don't accumulate.
"""
from uuid import uuid4

import pytest
import sqlalchemy as sa

import db
from db import Chatroom, ChatroomFolder


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


def test_backfill_orders_top_level_rooms_by_created_at(app_ctx):
    s = db.db.session
    human = db.get_human_user()
    # Three rooms all at position 0 (the freshly-migrated state).
    r1 = db.create_chatroom(f"bf-1-{uuid4().hex[:6]}", human.uuid, [])
    r2 = db.create_chatroom(f"bf-2-{uuid4().hex[:6]}", human.uuid, [])
    r3 = db.create_chatroom(f"bf-3-{uuid4().hex[:6]}", human.uuid, [])
    mine = [r1.uuid, r2.uuid, r3.uuid]
    try:
        s.execute(sa.update(Chatroom).values(position=0))
        s.commit()
        db._backfill_chatroom_positions()
        rows = {r.uuid: r.position for r in s.execute(
            sa.select(Chatroom).where(Chatroom.uuid.in_(mine))).scalars()}
        # created_at order is r1 < r2 < r3, so their positions strictly increase.
        assert rows[r1.uuid] < rows[r2.uuid] < rows[r3.uuid]
    finally:
        s.execute(sa.delete(Chatroom).where(Chatroom.uuid.in_(mine)))
        s.commit()


def test_backfill_is_noop_when_positions_already_distinct(app_ctx):
    s = db.db.session
    human = db.get_human_user()
    r1 = db.create_chatroom(f"nb-1-{uuid4().hex[:6]}", human.uuid, [])
    r2 = db.create_chatroom(f"nb-2-{uuid4().hex[:6]}", human.uuid, [])
    try:
        s.execute(sa.update(Chatroom).where(Chatroom.uuid == r1.uuid).values(position=5))
        s.execute(sa.update(Chatroom).where(Chatroom.uuid == r2.uuid).values(position=9))
        s.commit()
        db._backfill_chatroom_positions()  # distinct positions exist -> must not touch anything
        p1 = s.execute(sa.select(Chatroom.position).where(Chatroom.uuid == r1.uuid)).scalar()
        p2 = s.execute(sa.select(Chatroom.position).where(Chatroom.uuid == r2.uuid)).scalar()
        assert (p1, p2) == (5, 9)
    finally:
        s.execute(sa.delete(Chatroom).where(Chatroom.uuid.in_([r1.uuid, r2.uuid])))
        s.commit()


def test_create_and_list_folders(app_ctx):
    s = db.db.session
    f = db.create_chatroom_folder("Work")
    sub = db.create_chatroom_folder("Sub", parent_uuid=f.uuid)
    try:
        ids = {row["id"] for row in db.list_chatroom_folders()}
        assert str(f.uuid) in ids and str(sub.uuid) in ids
        sub_row = next(r for r in db.list_chatroom_folders() if r["id"] == str(sub.uuid))
        assert sub_row["parentId"] == str(f.uuid)
        assert sub_row["name"] == "Sub"
    finally:
        s.execute(sa.delete(ChatroomFolder).where(
            ChatroomFolder.uuid.in_([f.uuid, sub.uuid])))
        s.commit()


def test_load_tree_shape_and_version_ignores_messages(app_ctx):
    s = db.db.session
    human = db.get_human_user()
    f = db.create_chatroom_folder("TreeFolder")
    room = db.create_chatroom(f"tree-{uuid4().hex[:6]}", human.uuid, [])
    s.execute(sa.update(Chatroom).where(Chatroom.uuid == room.uuid).values(
        folder_uuid=f.uuid))
    s.commit()
    try:
        tree = db.chat_load_tree()
        assert {"folders", "rooms", "version"} <= set(tree)
        assert any(r["uuid"] == str(room.uuid) and r["folderId"] == str(f.uuid)
                   for r in tree["rooms"])
        assert all("member_count" in r and "folderId" in r for r in tree["rooms"])
        v1 = db.chat_tree_version()
        # A new message must NOT change the structural version token.
        db.post_chat_message(room.uuid, human.uuid, "hello")
        assert db.chat_tree_version() == v1
        # A structural change (reparent) MUST change it.
        s.execute(sa.update(Chatroom).where(Chatroom.uuid == room.uuid).values(
            folder_uuid=None))
        s.commit()
        assert db.chat_tree_version() != v1
    finally:
        s.execute(sa.delete(Chatroom).where(Chatroom.uuid == room.uuid))
        s.execute(sa.delete(ChatroomFolder).where(ChatroomFolder.uuid == f.uuid))
        s.commit()


def test_validate_rejects_bad_structures(app_ctx):
    rid = str(uuid4())
    fid = str(uuid4())
    # dangling folder parent
    with pytest.raises(db.ChatTreeError):
        db.validate_chat_tree([{"id": fid, "name": "F", "parentId": str(uuid4())}], [])
    # folder cycle (a -> b -> a)
    a, b = str(uuid4()), str(uuid4())
    with pytest.raises(db.ChatTreeError):
        db.validate_chat_tree(
            [{"id": a, "name": "A", "parentId": b},
             {"id": b, "name": "B", "parentId": a}], [])
    # room references a missing folder
    with pytest.raises(db.ChatTreeError):
        db.validate_chat_tree([], [{"uuid": rid, "folderId": str(uuid4())}])
    # room uuid collides with a folder id
    with pytest.raises(db.ChatTreeError):
        db.validate_chat_tree([{"id": fid, "name": "F", "parentId": None}],
                              [{"uuid": fid, "folderId": None}])
    # duplicate folder id
    with pytest.raises(db.ChatTreeError):
        db.validate_chat_tree([{"id": fid, "name": "A", "parentId": None},
                               {"id": fid, "name": "B", "parentId": None}], [])


def test_validate_accepts_a_well_formed_tree(app_ctx):
    fid = str(uuid4())
    db.validate_chat_tree(
        [{"id": fid, "name": "Work", "parentId": None}],
        [{"uuid": str(uuid4()), "folderId": fid},
         {"uuid": str(uuid4()), "folderId": None}],
    )  # no exception


@pytest.fixture
def two_rooms(app_ctx):
    """Two fresh top-level rooms. Yields (room_a_uuid, room_b_uuid)."""
    s = db.db.session
    human = db.get_human_user()
    a = db.create_chatroom(f"sv-a-{uuid4().hex[:6]}", human.uuid, [])
    b = db.create_chatroom(f"sv-b-{uuid4().hex[:6]}", human.uuid, [])
    try:
        yield a.uuid, b.uuid
    finally:
        s.execute(sa.delete(Chatroom).where(Chatroom.uuid.in_([a.uuid, b.uuid])))
        s.execute(sa.delete(ChatroomFolder).where(ChatroomFolder.name.like("svf-%")))
        s.commit()


def _all_rooms_payload(extra_overrides=None):
    """Every existing room as {uuid, folderId}, so a save never omits a room.
    extra_overrides: {room_uuid_str: folderId} to set placement for some."""
    overrides = extra_overrides or {}
    return [
        {"uuid": r["uuid"], "folderId": overrides.get(r["uuid"], r["folderId"])}
        for r in db.list_chatrooms()
    ]


def test_save_tree_moves_room_into_folder(two_rooms):
    a, b = two_rooms
    fid = str(uuid4())
    folders = [{"id": fid, "name": "svf-work", "parentId": None}]
    rooms = _all_rooms_payload({str(a): fid})
    db.chat_save_tree(folders, rooms, base_version=db.chat_tree_version())
    moved = next(r for r in db.list_chatrooms() if r["uuid"] == str(a))
    assert moved["folderId"] == fid


def test_save_tree_refuses_to_drop_a_room(two_rooms):
    a, b = two_rooms
    # Payload omitting room b would silently delete it -> must raise.
    rooms = [r for r in _all_rooms_payload() if r["uuid"] != str(b)]
    with pytest.raises(db.ChatTreeError):
        db.chat_save_tree([], rooms, base_version=db.chat_tree_version())


def test_save_tree_409_on_stale_version(two_rooms):
    with pytest.raises(db.ChatTreeConflict):
        db.chat_save_tree([], _all_rooms_payload(), base_version="staleversion0000")


def test_recursive_delete_preview_and_delete(app_ctx):
    s = db.db.session
    human = db.get_human_user()
    # parent -> child folder; one room in each; an unrelated top-level room.
    parent = db.create_chatroom_folder("delf-parent")
    child = db.create_chatroom_folder("delf-child", parent_uuid=parent.uuid)
    r_parent = db.create_chatroom(f"rp-{uuid4().hex[:6]}", human.uuid, [])
    r_child = db.create_chatroom(f"rc-{uuid4().hex[:6]}", human.uuid, [])
    r_other = db.create_chatroom(f"ro-{uuid4().hex[:6]}", human.uuid, [])
    s.execute(sa.update(Chatroom).where(Chatroom.uuid == r_parent.uuid).values(folder_uuid=parent.uuid))
    s.execute(sa.update(Chatroom).where(Chatroom.uuid == r_child.uuid).values(folder_uuid=child.uuid))
    s.commit()
    db.post_chat_message(r_parent.uuid, human.uuid, "one")
    db.post_chat_message(r_child.uuid, human.uuid, "two")
    db.post_chat_message(r_child.uuid, human.uuid, "three")
    try:
        preview = db.chatroom_folder_delete_preview(parent.uuid)
        assert preview["folder_name"] == "delf-parent"
        assert preview["room_count"] == 2          # r_parent + r_child
        assert preview["message_count"] == 3       # 1 + 2
        db.delete_chatroom_folder(parent.uuid)
        # Both folders + both contained rooms gone; the unrelated room survives.
        remaining = {r["uuid"] for r in db.list_chatrooms()}
        assert str(r_parent.uuid) not in remaining
        assert str(r_child.uuid) not in remaining
        assert str(r_other.uuid) in remaining
        folder_ids = {f["id"] for f in db.list_chatroom_folders()}
        assert str(parent.uuid) not in folder_ids and str(child.uuid) not in folder_ids
    finally:
        # Defensive cleanup: remove this test's folders + rooms even if an
        # assertion failed before delete_chatroom_folder ran, so nothing leaks
        # (a leaked folder + surviving room would orphan the room and break the
        # save-tree tests, which validate every room's folderId).
        s.execute(sa.delete(Chatroom).where(
            Chatroom.uuid.in_([r_parent.uuid, r_child.uuid, r_other.uuid])))
        s.execute(sa.delete(ChatroomFolder).where(
            ChatroomFolder.uuid.in_([parent.uuid, child.uuid])))
        s.commit()


def test_chatroom_delete_preview(app_ctx):
    human = db.get_human_user()
    room = db.create_chatroom(f"dp-{uuid4().hex[:6]}", human.uuid, [])
    db.post_chat_message(room.uuid, human.uuid, "x")
    try:
        preview = db.chatroom_delete_preview(room.uuid)
        assert preview["room_name"] == room.name
        assert preview["message_count"] == 1
    finally:
        db.delete_chatroom(room.uuid)
