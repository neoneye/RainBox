"""Bridge logic with in-memory fakes — no network, no threads."""
import json

import pytest

from bridge import (
    Config,
    RateLimitedLogger,
    load_config,
    load_state,
    process_updates,
    save_state,
)


# --- fakes --------------------------------------------------------------


class FakeTelegram:
    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


class FakeRainbox:
    def __init__(self, messages=None, post_fails=False):
        self.posted = []
        self.messages = messages or []
        self.post_fails = post_fails
        self._next_id = 100

    def post_message(self, room_uuid, text):
        if self.post_fails:
            raise RuntimeError("rainbox down")
        self.posted.append((room_uuid, text))
        self._next_id += 1
        return {"id": self._next_id, "uuid": f"m{self._next_id}"}

    def get_messages_after(self, room_uuid, after_id):
        return [m for m in self.messages if m["id"] > after_id]


def _cfg(tmp_path, **overrides):
    base = dict(
        bot_token="tok",
        allowed_user_ids=frozenset({111}),
        rainbox_url="http://127.0.0.1:5000",
        room_name="telegram",
        state_file=tmp_path / "state.json",
    )
    base.update(overrides)
    return Config(**base)


def _update(update_id, from_id=111, chat_id=222, text="hello"):
    msg = {"from": {"id": from_id}, "chat": {"id": chat_id}}
    if text is not None:
        msg["text"] = text
    return {"update_id": update_id, "message": msg}


# --- config -------------------------------------------------------------


def test_load_config_reads_env(tmp_path):
    env = {
        "TELEGRAM_BOT_TOKEN": "tok",
        "TELEGRAM_ALLOWED_USER_IDS": "111, 222",
        "TELEGRAM_STATE_FILE": str(tmp_path / "s.json"),
    }
    cfg = load_config(env)
    assert cfg.bot_token == "tok"
    assert cfg.allowed_user_ids == frozenset({111, 222})
    assert cfg.rainbox_url == "http://127.0.0.1:5000"
    assert cfg.room_name == "telegram"


def test_load_config_missing_token_exits():
    with pytest.raises(SystemExit):
        load_config({"TELEGRAM_ALLOWED_USER_IDS": "111"})


def test_load_config_empty_allowlist_exits():
    with pytest.raises(SystemExit):
        load_config({"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_ALLOWED_USER_IDS": " "})


# --- state --------------------------------------------------------------


def test_state_round_trip_and_missing_file(tmp_path):
    path = tmp_path / "state.json"
    assert load_state(path) == {}
    save_state(path, {"telegram_offset": 5})
    assert load_state(path) == {"telegram_offset": 5}
    assert json.loads(path.read_text())["telegram_offset"] == 5


# --- inbound ------------------------------------------------------------


def test_inbound_posts_text_and_advances_offset(tmp_path):
    cfg = _cfg(tmp_path)
    state, rainbox = {}, FakeRainbox()
    process_updates([_update(7)], cfg, state, rainbox, "room-1", RateLimitedLogger(60))
    assert rainbox.posted == [("room-1", "hello")]
    assert state["telegram_offset"] == 7
    assert state["operator_chat_id"] == 222
    assert load_state(cfg.state_file)["telegram_offset"] == 7


def test_inbound_drops_unauthorized_but_advances_offset(tmp_path):
    cfg = _cfg(tmp_path)
    state, rainbox = {}, FakeRainbox()
    process_updates(
        [_update(8, from_id=999)], cfg, state, rainbox, "room-1", RateLimitedLogger(60)
    )
    assert rainbox.posted == []
    assert state["telegram_offset"] == 8
    assert "operator_chat_id" not in state


def test_inbound_skips_non_text_but_advances_offset(tmp_path):
    cfg = _cfg(tmp_path)
    state, rainbox = {}, FakeRainbox()
    process_updates([_update(9, text=None)], cfg, state, rainbox, "room-1", RateLimitedLogger(60))
    assert rainbox.posted == []
    assert state["telegram_offset"] == 9


def test_inbound_post_failure_does_not_advance_offset(tmp_path):
    cfg = _cfg(tmp_path)
    state, rainbox = {"telegram_offset": 6}, FakeRainbox(post_fails=True)
    with pytest.raises(RuntimeError, match="rainbox down"):
        process_updates([_update(7)], cfg, state, rainbox, "room-1", RateLimitedLogger(60))
    assert state["telegram_offset"] == 6  # redelivered on next poll
