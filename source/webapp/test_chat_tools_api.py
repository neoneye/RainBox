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


def test_the_diary_starts_switched_off_and_says_so(room):
    diary = _families(room.uuid)["diary"]
    assert diary["room_on"] is False
    assert diary["note"].startswith("Off until switched on here")
    assert all(not t["on"] and t["off_reason"] == "switched off in this room"
               for t in diary["tools"])


def test_switching_the_diary_on_is_stored_for_this_room_only(room):
    other = _room([ASSISTANT_UUID])
    try:
        resp = _put(room.uuid, "diary", {"on": True})
        assert resp.status_code == 200, resp.get_data(as_text=True)
        diary = {f["family"]: f for f in resp.get_json()["tools"]}["diary"]
        assert diary["room_on"] is True
        assert "diary" not in db.member_disabled_tool_families(room.uuid, ASSISTANT_UUID)
        assert _families(other.uuid)["diary"]["room_on"] is False
        _put(room.uuid, "diary", {"on": False})
        assert "diary" in db.member_disabled_tool_families(room.uuid, ASSISTANT_UUID)
    finally:
        db.delete_chatroom(other.uuid)


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


def _put(room_uuid, family, body):
    with patch("agents.assistant._disabled_capability_names", return_value=set()), \
         patch("agents.model_groups.assistant_models_all_local", return_value=(True, None)):
        return app.test_client().put(f"/chat/api/rooms/{room_uuid}/tools/{family}", json=body)


def test_every_family_but_the_diary_starts_switched_on(room):
    families = _families(room.uuid)
    assert all(f["room_on"] for name, f in families.items() if name != "diary")
    assert families["diary"]["room_on"] is False


def test_switching_a_family_off_stores_it_for_this_room_only(room):
    other = _room([ASSISTANT_UUID])
    try:
        resp = _put(room.uuid, "kanban", {"on": False})
        assert resp.status_code == 200, resp.get_data(as_text=True)
        kanban = {f["family"]: f for f in resp.get_json()["tools"]}["kanban"]
        assert kanban["room_on"] is False
        assert all(not t["on"] and t["off_reason"] == "switched off in this room"
                   for t in kanban["tools"])
        assert db.member_disabled_tool_families(room.uuid, ASSISTANT_UUID) == {"kanban", "diary"}
        assert _families(other.uuid)["kanban"]["room_on"] is True
    finally:
        db.delete_chatroom(other.uuid)


def test_switching_back_on_restores_the_family(room):
    _put(room.uuid, "kanban", {"on": False})
    kanban = {f["family"]: f for f in _put(room.uuid, "kanban", {"on": True}).get_json()["tools"]}["kanban"]
    assert kanban["room_on"] is True and all(t["on"] for t in kanban["tools"])


def test_a_room_switch_never_turns_on_what_settings_turned_off(room):
    tools = _families(room.uuid, disabled={"kanban_read"})["kanban"]["tools"]
    read = next(t for t in tools if t["name"] == "kanban_read")
    assert read["on"] is False and read["off_reason"] == "turned off in Settings"


def test_the_conversation_family_has_no_switch(room):
    assert _put(room.uuid, "conversation", {"on": False}).status_code == 400
    assert _put(room.uuid, "nonsense", {"on": False}).status_code == 400


def test_the_switch_body_must_say_on_or_off(room):
    assert _put(room.uuid, "kanban", {"on": "no"}).status_code == 400
    assert _put(room.uuid, "kanban", {}).status_code == 400


def test_a_room_without_the_assistant_has_no_switches(ctx):
    human = db.get_human_user()
    r = db.create_chatroom(f"api-tools-{uuid4().hex[:8]}", human.uuid, [])
    try:
        assert _put(r.uuid, "kanban", {"on": False}).status_code == 404
    finally:
        db.delete_chatroom(r.uuid)
