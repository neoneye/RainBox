"""import_legacy.py against a fake core session — no network, no token read."""
import json
from typing import Any

import pytest

import import_legacy as il


class _Resp:
    def __init__(self, status: int, body: Any):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeSession:
    def __init__(self, rooms):
        self.rooms = rooms
        self.posts: list[tuple[str, dict]] = []

    def get(self, url, timeout=None):
        assert url.endswith("/chat/api/rooms")
        return _Resp(200, self.rooms)

    def post(self, url, json=None, timeout=None):
        self.posts.append((url, json))
        if url.endswith("/bridges/api/connectors"):
            return _Resp(201, {"connector": {"uuid": "c-1", **json}})
        return _Resp(201, {"binding": {"uuid": "b-1", **json}})


ENV = {"DISCORD_CHANNEL_ID": "777", "DISCORD_ALLOWED_USER_IDS": "111, 222", "DISCORD_POLL_SECONDS": "3",
       "DISCORD_ROOM_NAME": "discord", "RAINBOX_URL": "http://core:5000/", "DISCORD_BOT_TOKEN": "NEVER-READ"}


def test_import_creates_disabled_rows_and_wraps_state(tmp_path):
    legacy = tmp_path / "state.json"
    legacy.write_text(json.dumps({"discord_after": "50", "room_cursor": 9, "progress_messages": {"7": "m7"}}))
    session = FakeSession([{"uuid": "r-1", "name": "discord"}, {"uuid": "r-2", "name": "other"}])
    out = il.run(["--name", "Main Bot", "--state-dir", str(tmp_path / "svc")],
                 env={**ENV, "DISCORD_STATE_FILE": str(legacy)}, session=session)
    conn_body = session.posts[0][1]
    assert conn_body == {"name": "Main Bot", "platform": "discord", "token_env": "DISCORD_BOT_TOKEN",
                         "policy": {"allowed_senders": ["111", "222"], "poll_seconds": 3.0}}
    assert "NEVER-READ" not in json.dumps(session.posts)          # the value never travels
    assert session.posts[1][1] == {"connectorId": "c-1", "roomUuid": "r-1", "address": {"channel_id": "777"}}
    wrapped = json.loads((tmp_path / "svc" / "bridge-c-1.json").read_text())
    assert wrapped["schema_version"] == 2 and wrapped["connector_uuid"] == "c-1"
    assert wrapped["bindings"]["b-1"] == {"room_uuid": "r-1", "address": {"channel_id": "777"},
                                          "address_key": "channel_id=777", "discord_after": "50",
                                          "room_cursor": 9, "progress_messages": {"7": "m7"}}
    assert legacy.exists() and out["state_file"].endswith("bridge-c-1.json")


def test_import_without_legacy_state_writes_nothing(tmp_path):
    session = FakeSession([{"uuid": "r-1", "name": "discord"}])
    out = il.run(["--name", "X", "--state-dir", str(tmp_path)], env={**ENV, "DISCORD_STATE_FILE": str(tmp_path / "none.json")}, session=session)
    assert out["state_file"] is None and not list(tmp_path.glob("bridge-*.json"))


def test_import_refuses_ambiguous_or_missing_room_and_bad_env(tmp_path):
    with pytest.raises(SystemExit):
        il.run(["--name", "X", "--state-dir", str(tmp_path)], env=ENV,
               session=FakeSession([{"uuid": "r-1", "name": "discord"}, {"uuid": "r-2", "name": "discord"}]))
    with pytest.raises(SystemExit):
        il.run(["--name", "X", "--state-dir", str(tmp_path)], env=ENV, session=FakeSession([]))
    with pytest.raises(SystemExit):
        il.run(["--name", "X", "--state-dir", str(tmp_path)], env={**ENV, "DISCORD_CHANNEL_ID": "abc"},
               session=FakeSession([{"uuid": "r-1", "name": "discord"}]))


def test_import_never_overwrites_an_existing_target(tmp_path):
    legacy = tmp_path / "state.json"
    legacy.write_text("{}")
    (tmp_path / "bridge-c-1.json").write_text("{}")
    with pytest.raises(SystemExit):
        il.run(["--name", "X", "--state-dir", str(tmp_path)], env={**ENV, "DISCORD_STATE_FILE": str(legacy)},
               session=FakeSession([{"uuid": "r-1", "name": "discord"}]))
