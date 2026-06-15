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
