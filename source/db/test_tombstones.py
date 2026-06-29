# db/test_tombstones.py
import pytest
from uuid import uuid4
import db
from db.memory import (with_note, write_tombstone, check_tombstone,
                       clear_tombstone, record_tombstone_hit, advisory_key)
from db import MemoryClaim
from db.models import MemoryRejectedValue


@pytest.fixture
def app_ctx():
    app = db.make_app(); db.init_db(app)
    ctx = app.app_context(); ctx.push()
    try:
        yield app
    finally:
        ctx.pop()


def _mk_claim(room, *, subject="alice", predicate="prefers", obj="tea"):
    return db.create_memory_claim(
        scope="room", kind="preference", text=f"{subject} {predicate} {obj}",
        confidence=1.0, status="active", room_uuid=room,
        subject=subject, predicate=predicate, object=obj,
        subj_pred_key=f"{subject}\x1f{predicate}", value_key=obj, key_version=1)


def _cleanup(room):
    db.db.session.query(MemoryClaim).filter_by(room_uuid=room).delete()
    db.db.session.query(MemoryRejectedValue).filter_by(room_uuid=room).delete()
    db.db.session.commit()


def test_with_note_appends_without_collision():
    out = with_note({"excerpt": "orig", "provenance": "x"}, "added")
    assert out["excerpt"] == "orig; added"
    assert with_note({"provenance": "x"}, "added")["excerpt"] == "added"


def test_write_then_check_tombstone(app_ctx):
    room = uuid4()
    c = _mk_claim(room)
    write_tombstone(c, reason="forgot")
    hit = check_tombstone("room", room, None, c.subj_pred_key, c.value_key)
    assert hit is not None and hit.claim_text == c.text
    assert check_tombstone("room", room, None, c.subj_pred_key, "coffee") is None
    _cleanup(room)


def test_write_tombstone_is_idempotent_on_key(app_ctx):
    room = uuid4()
    c = _mk_claim(room)
    write_tombstone(c, reason="one")
    write_tombstone(c, reason="two")   # same key -> upsert, not a 2nd row
    n = db.db.session.query(MemoryRejectedValue).filter_by(room_uuid=room).count()
    assert n == 1
    _cleanup(room)


def test_clear_and_hit(app_ctx):
    room = uuid4()
    c = _mk_claim(room)
    t = write_tombstone(c, reason="x")
    record_tombstone_hit(t)
    assert check_tombstone("room", room, None, c.subj_pred_key, c.value_key).hit_count == 1
    clear_tombstone(t)
    assert check_tombstone("room", room, None, c.subj_pred_key, c.value_key) is None
    _cleanup(room)


def test_advisory_key_is_stable_63bit():
    k = advisory_key("global", None, None, "a\x1fis", "b")
    assert k == advisory_key("global", None, None, "a\x1fis", "b")
    assert -(2**63) <= k < 2**63
