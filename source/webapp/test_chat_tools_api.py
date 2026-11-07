"""The room Settings panel's Tools list: /chat/api/rooms/<room>/tools."""
from unittest.mock import patch
from uuid import uuid4

import pytest

import db
from agents.config import ASSISTANT_UUID
from webapp.core import app


@pytest.fixture
def ctx():
    a = db.make_app()
    db.init_db(a)
    c = a.app_context()
    c.push()
    try:
        yield
    finally:
        c.pop()


def _room(members):
    human = db.get_human_user()
    return db.create_chatroom(f"api-tools-{uuid4().hex[:8]}", human.uuid, members)


@pytest.fixture
def room(ctx):
    r = _room([ASSISTANT_UUID])
    try:
        yield r
    finally:
        db.delete_chatroom(r.uuid)


def _families(room_uuid, disabled=frozenset()):
    with patch("agents.assistant._disabled_capability_names", return_value=set(disabled)), \
         patch("agents.model_groups.assistant_models_all_local", return_value=(True, None)):
        resp = app.test_client().get(f"/chat/api/rooms/{room_uuid}/tools")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return {f["family"]: f for f in resp.get_json()["tools"]}


def test_families_are_listed_in_order_without_the_conversation_family(room):
    with patch("agents.assistant._disabled_capability_names", return_value=set()), \
         patch("agents.model_groups.assistant_models_all_local", return_value=(True, None)):
        tools = app.test_client().get(f"/chat/api/rooms/{room.uuid}/tools").get_json()["tools"]
    names = [f["family"] for f in tools]
    assert names[:3] == ["memory", "diary", "kanban"]
    assert "conversation" not in names
    assert tools[0]["label"] == "Memory"


def test_tools_the_model_never_picks_are_left_out(room):
    memory = {t["name"] for t in _families(room.uuid)["memory"]["tools"]}
    assert "memory_query" in memory
    assert "memory_reactivate" not in memory


def test_a_tool_turned_off_in_settings_says_so(room):
    kanban = _families(room.uuid, disabled={"kanban_read"})["kanban"]["tools"]
    read = next(t for t in kanban if t["name"] == "kanban_read")
    assert read["on"] is False and read["off_reason"] == "turned off in Settings"
    assert any(t["on"] for t in kanban)


def test_write_tools_are_marked(room):
    memory = {t["name"]: t for t in _families(room.uuid)["memory"]["tools"]}
    assert memory["memory_remember"]["write"] is True
    assert memory["memory_query"]["write"] is False


def test_diary_without_a_source_in_the_room_is_off_with_the_reason(room):
    diary = _families(room.uuid)["diary"]
    assert diary["note"] == "No diary source is readable in this room."
    assert diary["sources"] == []
    assert all(not t["on"] and t["off_reason"] == "not offered in this room"
               for t in diary["tools"])


def test_a_room_without_the_assistant_lists_nothing(ctx):
    human = db.get_human_user()
    r = db.create_chatroom(f"api-tools-{uuid4().hex[:8]}", human.uuid, [])
    try:
        body = app.test_client().get(f"/chat/api/rooms/{r.uuid}/tools").get_json()
        assert body == {"tools": []}
    finally:
        db.delete_chatroom(r.uuid)


def test_unknown_room_is_a_404(ctx):
    assert app.test_client().get(f"/chat/api/rooms/{uuid4()}/tools").status_code == 404
