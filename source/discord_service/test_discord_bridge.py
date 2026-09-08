"""Bridge logic with in-memory fakes — no network, no threads."""
import json
import threading
from typing import Any

import pytest

from bridge import (
    PROGRESS_PLACEHOLDER,
    Config,
    RateLimitedLogger,
    handle_event,
    inbound_loop,
    init_discord_cursor,
    init_room_cursor,
    load_config,
    load_state,
    outbound_catchup,
    outbound_loop,
    process_messages,
    reconcile_progress,
    redact,
    save_state,
    truncate_text,
)


# --- fakes --------------------------------------------------------------


class FakeDiscord:
    def __init__(self, channel_messages=None):
        self.channel_messages = channel_messages or []
        self.sent: list[str] = []      # texts posted
        self.edited: list[tuple[str, str]] = []
        self.deleted: list[str] = []
        self._next = 1000

    def get_messages(self, channel_id, after, limit=100):
        rows = sorted(self.channel_messages, key=lambda m: int(m["id"]))
        if after is None:
            return rows[-limit:]  # like Discord: no cursor = the newest page
        return [m for m in rows if int(m["id"]) > int(after)][:limit]

    def send_message(self, channel_id, text):
        ids = []
        for i in range(0, len(text), 2000):
            self._next += 1
            self.sent.append(text[i:i + 2000])
            ids.append(str(self._next))
        return ids

    def edit_message(self, channel_id, message_id, text):
        self.edited.append((message_id, text))

    def delete_message(self, channel_id, message_id):
        self.deleted.append(message_id)


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

    def get_message(self, room_uuid, message_id):
        return next((m for m in self.messages if m["id"] == message_id), None)


def _cfg(tmp_path, **overrides: Any) -> Config:
    base: dict[str, Any] = dict(
        bot_token="tok",
        channel_id="777",
        allowed_user_ids=frozenset({"111"}),
        rainbox_url="http://127.0.0.1:5000",
        room_name="discord",
        state_file=tmp_path / "state.json",
        poll_seconds=0.0,
    )
    base.update(overrides)
    return Config(**base)


def _dmsg(mid: str, author_id: str = "111", content: str | None = "hello", bot: bool = False) -> dict[str, Any]:
    author: dict[str, Any] = {"id": author_id}
    if bot:
        author["bot"] = True
    return {"id": mid, "author": author, "content": content or ""}


def _row(mid: int, kind: str = "message", sender_type: str = "agent", text: str = "t", streaming: bool = False) -> dict[str, Any]:
    return {"id": mid, "kind": kind, "sender_type": sender_type, "text": text, "streaming": streaming}


# --- config -------------------------------------------------------------


def test_load_config_reads_env(tmp_path):
    cfg = load_config({
        "DISCORD_BOT_TOKEN": "tok",
        "DISCORD_CHANNEL_ID": "777",
        "DISCORD_ALLOWED_USER_IDS": "111, 222",
        "DISCORD_STATE_FILE": str(tmp_path / "s.json"),
        "DISCORD_POLL_SECONDS": "5",
    })
    assert cfg.bot_token == "tok"
    assert cfg.channel_id == "777"
    assert cfg.allowed_user_ids == frozenset({"111", "222"})
    assert cfg.rainbox_url == "http://127.0.0.1:5000"
    assert cfg.room_name == "discord"
    assert cfg.poll_seconds == 5.0


def test_load_config_defaults_poll_seconds():
    cfg = load_config({"DISCORD_BOT_TOKEN": "t", "DISCORD_CHANNEL_ID": "1", "DISCORD_ALLOWED_USER_IDS": "2"})
    assert cfg.poll_seconds == 2.0


@pytest.mark.parametrize("env", [
    {"DISCORD_CHANNEL_ID": "1", "DISCORD_ALLOWED_USER_IDS": "2"},
    {"DISCORD_BOT_TOKEN": "t", "DISCORD_ALLOWED_USER_IDS": "2"},
    {"DISCORD_BOT_TOKEN": "t", "DISCORD_CHANNEL_ID": "1"},
    {"DISCORD_BOT_TOKEN": "t", "DISCORD_CHANNEL_ID": "abc", "DISCORD_ALLOWED_USER_IDS": "2"},
    {"DISCORD_BOT_TOKEN": "t", "DISCORD_CHANNEL_ID": "1", "DISCORD_ALLOWED_USER_IDS": "not-a-number"},
])
def test_load_config_rejects_missing_or_non_numeric(env):
    with pytest.raises(SystemExit):
        load_config(env)


def test_redact_hides_token():
    assert redact("Bot tok failed", "tok") == "Bot <redacted> failed"
    assert redact("plain", "") == "plain"


# --- state --------------------------------------------------------------


def test_state_round_trip_and_missing_file(tmp_path):
    path = tmp_path / "state.json"
    assert load_state(path) == {}
    save_state(path, {"discord_after": "5"})
    assert load_state(path) == {"discord_after": "5"}
    assert json.loads(path.read_text())["discord_after"] == "5"


def test_init_discord_cursor_uses_newest_message(tmp_path):
    cfg = _cfg(tmp_path)
    state: dict[str, Any] = {}
    init_discord_cursor(cfg, state, FakeDiscord([_dmsg("10"), _dmsg("30"), _dmsg("20")]))
    assert state["discord_after"] == "30"
    init_discord_cursor(cfg, state, FakeDiscord([_dmsg("99")]))
    assert state["discord_after"] == "30"  # kept


def test_init_discord_cursor_empty_channel(tmp_path):
    state: dict[str, Any] = {}
    init_discord_cursor(_cfg(tmp_path), state, FakeDiscord([]))
    assert state["discord_after"] == "0"


def test_init_room_cursor_uses_latest_message(tmp_path):
    state: dict[str, Any] = {}
    init_room_cursor(_cfg(tmp_path), state, FakeRainbox([_row(3), _row(9)]), "room")
    assert state["room_cursor"] == 9
    init_room_cursor(_cfg(tmp_path), state, FakeRainbox([_row(50)]), "room")
    assert state["room_cursor"] == 9


# --- inbound ------------------------------------------------------------


def test_inbound_posts_text_and_advances_after(tmp_path):
    cfg = _cfg(tmp_path)
    state: dict[str, Any] = {}
    rb = FakeRainbox()
    process_messages([_dmsg("10", content="hi"), _dmsg("11", content="there")], cfg, state, rb, "room", RateLimitedLogger(60))
    assert rb.posted == [("room", "hi"), ("room", "there")]
    assert state["discord_after"] == "11"
    assert load_state(cfg.state_file)["discord_after"] == "11"


def test_inbound_drops_unauthorized_and_bots_but_advances(tmp_path):
    cfg = _cfg(tmp_path)
    state: dict[str, Any] = {}
    rb = FakeRainbox()
    process_messages([_dmsg("10", author_id="999"), _dmsg("11", bot=True)], cfg, state, rb, "room", RateLimitedLogger(60))
    assert rb.posted == []
    assert state["discord_after"] == "11"


def test_inbound_skips_empty_content_but_advances(tmp_path):
    cfg = _cfg(tmp_path)
    state: dict[str, Any] = {}
    rb = FakeRainbox()
    process_messages([_dmsg("10", content="   ")], cfg, state, rb, "room", RateLimitedLogger(60))
    assert rb.posted == []
    assert state["discord_after"] == "10"


def test_inbound_post_failure_does_not_advance(tmp_path):
    cfg = _cfg(tmp_path)
    state: dict[str, Any] = {"discord_after": "9"}
    with pytest.raises(RuntimeError):
        process_messages([_dmsg("10")], cfg, state, FakeRainbox(post_fails=True), "room", RateLimitedLogger(60))
    assert state["discord_after"] == "9"


# --- outbound -----------------------------------------------------------


def test_truncate_text():
    assert truncate_text("abc", 5) == "abc"
    assert truncate_text("abcdefgh", 5) == "abcd…"


def test_catchup_forwards_agent_message_and_notice_only(tmp_path):
    cfg = _cfg(tmp_path)
    state: dict[str, Any] = {"room_cursor": 0}
    rb = FakeRainbox([
        _row(1, sender_type="human", text="mine"),
        _row(2, kind="thinking", text="hmm"),
        _row(3, kind="progress", text="step"),
        _row(4, kind="message", text="reply"),
        _row(5, kind="notice", text="failed"),
    ])
    dc = FakeDiscord()
    outbound_catchup(cfg, state, rb, dc, "room")
    assert dc.sent == ["reply", "failed"]
    assert state["room_cursor"] == 5


def test_catchup_stops_at_streaming_row(tmp_path):
    cfg = _cfg(tmp_path)
    state: dict[str, Any] = {"room_cursor": 0}
    rb = FakeRainbox([_row(1, text="done"), _row(2, text="partial", streaming=True), _row(3, text="later")])
    dc = FakeDiscord()
    outbound_catchup(cfg, state, rb, dc, "room")
    assert dc.sent == ["done"]
    assert state["room_cursor"] == 1
    rb.messages[1]["streaming"] = False
    outbound_catchup(cfg, state, rb, dc, "room")
    assert dc.sent == ["done", "partial", "later"]
    assert state["room_cursor"] == 3


def test_catchup_chunks_long_reply(tmp_path):
    cfg = _cfg(tmp_path)
    state: dict[str, Any] = {"room_cursor": 0}
    dc = FakeDiscord()
    outbound_catchup(cfg, state, FakeRainbox([_row(1, text="x" * 2001)]), dc, "room")
    assert [len(t) for t in dc.sent] == [2000, 1]


def test_progress_event_sends_then_edits_then_deletes(tmp_path):
    cfg = _cfg(tmp_path)
    state: dict[str, Any] = {"room_cursor": 0}
    rb = FakeRainbox([_row(7, kind="progress", text="step 1")])
    dc = FakeDiscord()
    handle_event(cfg, state, rb, dc, "room", {"room_uuid": "room", "message_id": 7, "event": "insert", "kind": "progress"})
    assert dc.sent == ["step 1"]           # text fetched by id (insert carries none)
    assert state["progress_messages"] == {"7": "1001"}
    handle_event(cfg, state, rb, dc, "room", {"room_uuid": "room", "message_id": 7, "event": "update", "kind": "progress", "streaming": False, "text": "step 2"})
    assert dc.edited == [("1001", "step 2")]
    rb.messages = [_row(8, text="reply")]
    handle_event(cfg, state, rb, dc, "room", {"room_uuid": "room", "message_id": 8, "event": "insert", "kind": "message", "deleted_progress_ids": [7]})
    assert dc.deleted == ["1001"]
    assert state["progress_messages"] == {}
    assert dc.sent == ["step 1", "reply"]
    assert load_state(cfg.state_file)["progress_messages"] == {}


def test_progress_empty_text_uses_placeholder_and_long_text_truncates(tmp_path):
    cfg = _cfg(tmp_path)
    state: dict[str, Any] = {"room_cursor": 0}
    dc = FakeDiscord()
    handle_event(cfg, state, FakeRainbox(), dc, "room", {"room_uuid": "room", "message_id": 7, "event": "update", "kind": "progress", "streaming": False, "text": ""})
    assert dc.sent == [PROGRESS_PLACEHOLDER]
    handle_event(cfg, state, FakeRainbox(), dc, "room", {"room_uuid": "room", "message_id": 7, "event": "update", "kind": "progress", "streaming": False, "text": "y" * 2500})
    assert len(dc.edited[0][1]) == 2000


def test_progress_event_for_vanished_row_is_dropped(tmp_path):
    cfg = _cfg(tmp_path)
    state: dict[str, Any] = {"room_cursor": 0, "progress_messages": {"7": "1001"}}
    dc = FakeDiscord()
    handle_event(cfg, state, FakeRainbox([]), dc, "room", {"room_uuid": "room", "message_id": 7, "event": "insert", "kind": "progress"})
    assert dc.deleted == ["1001"]
    assert state["progress_messages"] == {}


def test_reconcile_deletes_orphaned_bubbles(tmp_path):
    cfg = _cfg(tmp_path)
    state: dict[str, Any] = {"progress_messages": {"7": "1001", "8": "1002"}}
    dc = FakeDiscord()
    reconcile_progress(cfg, state, FakeRainbox([_row(8, kind="progress")]), dc, "room")
    assert dc.deleted == ["1001"]
    assert state["progress_messages"] == {"8": "1002"}


def test_delete_event_triggers_reconcile(tmp_path):
    cfg = _cfg(tmp_path)
    state: dict[str, Any] = {"room_cursor": 0, "progress_messages": {"7": "1001"}}
    dc = FakeDiscord()
    handle_event(cfg, state, FakeRainbox([]), dc, "room", {"room_uuid": "room", "message_id": 0, "event": "delete"})
    assert dc.deleted == ["1001"]


# --- loops --------------------------------------------------------------


class _OneShotDiscord(FakeDiscord):
    """get_messages returns the batch once, then sets stop."""

    def __init__(self, batch, stop):
        super().__init__()
        self._batch = batch
        self._stop = stop

    def get_messages(self, channel_id, after, limit=100):
        self._stop.set()
        return self._batch


def test_inbound_loop_processes_then_stops(tmp_path):
    cfg = _cfg(tmp_path)
    stop = threading.Event()
    state: dict[str, Any] = {"discord_after": "0"}
    rb = FakeRainbox()
    inbound_loop(cfg, state, rb, _OneShotDiscord([_dmsg("5", content="yo")], stop), "room", stop)
    assert rb.posted == [("room", "yo")]
    assert state["discord_after"] == "5"


class _SSERainbox(FakeRainbox):
    def __init__(self, events, stop, **kw):
        super().__init__(**kw)
        self._events = events
        self._stop = stop

    def iter_sse_events(self):
        for e in self._events:
            yield e
        self._stop.set()


def test_outbound_loop_reconciles_then_handles_room_events(tmp_path):
    cfg = _cfg(tmp_path)
    stop = threading.Event()
    state: dict[str, Any] = {"room_cursor": 0, "progress_messages": {"3": "900"}}
    rb = _SSERainbox(
        [{"room_uuid": "other", "message_id": 1, "event": "insert", "kind": "message"},
         {"room_uuid": "room", "message_id": 4, "event": "insert", "kind": "message"}],
        stop, messages=[_row(4, text="reply")],
    )
    dc = FakeDiscord()
    outbound_loop(cfg, state, rb, dc, "room", stop)
    assert dc.deleted == ["900"]   # orphan from before the connect
    assert dc.sent == ["reply"]
    assert state["room_cursor"] == 4


def test_loop_errors_never_log_the_bot_token(tmp_path, caplog):
    cfg = _cfg(tmp_path, bot_token="SECRET-TOKEN")
    stop = threading.Event()

    class Boom(FakeDiscord):
        def get_messages(self, channel_id, after, limit=100):
            stop.set()
            raise RuntimeError("401 for Bot SECRET-TOKEN")

    with caplog.at_level("ERROR"):
        inbound_loop(cfg, {"discord_after": "0"}, FakeRainbox(), Boom(), "room", stop)
    assert "SECRET-TOKEN" not in caplog.text
    assert "<redacted>" in caplog.text
