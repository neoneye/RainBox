"""Bridge logic with in-memory fakes — no network, no threads."""
import json
import threading
from typing import Any

import pytest

from bridge import (
    Config,
    RateLimitedLogger,
    inbound_loop,
    init_room_cursor,
    load_config,
    load_state,
    outbound_catchup,
    outbound_loop,
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


def _cfg(tmp_path, **overrides: Any) -> Config:
    base: dict[str, Any] = dict(
        bot_token="tok",
        allowed_user_ids=frozenset({111}),
        rainbox_url="http://127.0.0.1:5000",
        room_name="telegram",
        state_file=tmp_path / "state.json",
    )
    base.update(overrides)
    return Config(**base)


def _update(update_id: int, from_id: int = 111, chat_id: int = 222, text: str | None = "hello") -> dict[str, Any]:
    msg: dict[str, Any] = {"from": {"id": from_id}, "chat": {"id": chat_id}}
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


# --- outbound -----------------------------------------------------------


def _agent_msg(mid: int, text: str = "reply", kind: str = "message", sender_type: str = "agent", streaming: bool = False) -> dict[str, Any]:
    return {
        "id": mid, "uuid": f"m{mid}", "text": text, "kind": kind,
        "sender_type": sender_type, "streaming": streaming,
    }


def test_outbound_forwards_agent_messages_only(tmp_path):
    cfg = _cfg(tmp_path)
    state = {"operator_chat_id": 222, "room_cursor": 0}
    rainbox = FakeRainbox(messages=[
        _agent_msg(1, text="from human", sender_type="human"),
        _agent_msg(2, text="debug row", kind="debug-router"),
        _agent_msg(3, text="real reply"),
    ])
    telegram = FakeTelegram()
    outbound_catchup(cfg, state, rainbox, telegram, "room-1")
    assert telegram.sent == [(222, "real reply")]
    assert state["room_cursor"] == 3


def test_outbound_stops_at_streaming_row(tmp_path):
    cfg = _cfg(tmp_path)
    state = {"operator_chat_id": 222, "room_cursor": 0}
    rainbox = FakeRainbox(messages=[
        _agent_msg(1, text="done reply"),
        _agent_msg(2, text="half a repl", streaming=True),
        _agent_msg(3, text="later reply"),
    ])
    telegram = FakeTelegram()
    outbound_catchup(cfg, state, rainbox, telegram, "room-1")
    # forwarded the finished row, then STOPPED at the streaming row without
    # advancing past it — its finalizing SSE event re-triggers catch-up
    assert telegram.sent == [(222, "done reply")]
    assert state["room_cursor"] == 1


def test_outbound_without_chat_id_advances_cursor_without_sending(tmp_path):
    cfg = _cfg(tmp_path)
    state = {"room_cursor": 0}
    rainbox = FakeRainbox(messages=[_agent_msg(1)])
    telegram = FakeTelegram()
    outbound_catchup(cfg, state, rainbox, telegram, "room-1")
    assert telegram.sent == []
    assert state["room_cursor"] == 1


# --- loops / main wiring --------------------------------------------------


class FakeTelegramPolling(FakeTelegram):
    """get_updates returns one batch, then sets stop so loops exit in tests."""

    def __init__(self, batches: list[list[dict[str, Any]]], stop: threading.Event) -> None:
        super().__init__()
        self.batches = list(batches)
        self.stop = stop

    def get_updates(self, offset: int, timeout: int = 50) -> list[dict[str, Any]]:
        if self.batches:
            return self.batches.pop(0)
        self.stop.set()
        return []


class FakeRainboxSSE(FakeRainbox):
    def __init__(self, events: list[dict[str, Any]], stop: threading.Event, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.events = list(events)
        self.stop = stop

    def iter_sse_events(self) -> Any:
        yield from self.events
        self.stop.set()


def test_init_room_cursor_uses_latest_message(tmp_path):
    cfg = _cfg(tmp_path)
    rainbox = FakeRainbox(messages=[_agent_msg(4), _agent_msg(9)])
    state: dict[str, Any] = {}
    init_room_cursor(cfg, state, rainbox, "room-1")
    assert state["room_cursor"] == 9  # never replay history


def test_init_room_cursor_keeps_existing(tmp_path):
    cfg = _cfg(tmp_path)
    state: dict[str, Any] = {"room_cursor": 5}
    init_room_cursor(cfg, state, FakeRainbox(messages=[_agent_msg(9)]), "room-1")
    assert state["room_cursor"] == 5


def test_inbound_loop_processes_then_stops(tmp_path):
    cfg = _cfg(tmp_path)
    stop = threading.Event()
    telegram = FakeTelegramPolling([[_update(7)]], stop)
    rainbox = FakeRainbox()
    state: dict[str, Any] = {}
    inbound_loop(cfg, state, rainbox, telegram, "room-1", stop)
    assert rainbox.posted == [("room-1", "hello")]
    assert state["telegram_offset"] == 7


def test_outbound_loop_catches_up_on_matching_room_event(tmp_path):
    cfg = _cfg(tmp_path)
    stop = threading.Event()
    rainbox = FakeRainboxSSE(
        events=[{"room_uuid": "other", "message_id": 1},
                {"room_uuid": "room-1", "message_id": 2}],
        stop=stop,
        messages=[_agent_msg(2)],
    )
    telegram = FakeTelegram()
    state: dict[str, Any] = {"operator_chat_id": 222, "room_cursor": 0}
    outbound_loop(cfg, state, rainbox, telegram, "room-1", stop)
    assert telegram.sent == [(222, "reply")]


def test_loop_errors_never_log_the_bot_token(tmp_path, caplog):
    """requests exceptions embed the Telegram URL, which contains the bot
    token — loop error logging must redact it (message and traceback)."""

    cfg = _cfg(tmp_path)
    stop = threading.Event()

    class ExplodingTelegram(FakeTelegram):
        def get_updates(self, offset, timeout=50):
            stop.set()
            raise RuntimeError(
                "401 Client Error: Unauthorized for url: "
                "https://api.telegram.org/bottok/getUpdates?timeout=50"
            )

    with caplog.at_level("ERROR"):
        inbound_loop(cfg, {}, FakeRainbox(), ExplodingTelegram(), "room-1", stop)
    assert "bottok" not in caplog.text
    assert "bot<redacted>" in caplog.text
