"""Connector-mode bridge logic with in-memory fakes — no network."""
import json
import threading
import time
from typing import Any

import pytest

import connector_bridge as cb
from connector_bridge import (
    Applier,
    Bridge,
    ConfigError,
    ConfigState,
    Paused,
    Snapshot,
    StateStore,
    config_loop,
    inbound_loop,
    outbound_loop,
    parse_snapshot,
)

CONN = "0f7a1b2c-3d4e-4f50-8a9b-0c1d2e3f4a5b"
B1 = "11111111-1111-4111-8111-111111111111"
B2 = "22222222-2222-4222-8222-222222222222"
ROOM = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ROOM2 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


# --- fakes --------------------------------------------------------------


class FakeDiscord:
    def __init__(self, channels: dict[str, list[dict]] | None = None):
        self.channels = channels or {}
        self.sent: list[tuple[str, str]] = []       # (channel, text)
        self.edited: list[tuple[str, str, str]] = []
        self.deleted: list[tuple[str, str]] = []
        self.deleted_once: list[tuple[str, str, float]] = []
        self.fail_delete_once: set[str] = set()
        self._next = 1000
        self.me = {"id": "bot-1", "username": "rainbox"}

    def get_me(self):
        return self.me

    def get_messages(self, channel_id, after, limit=100):
        rows = sorted(self.channels.get(channel_id, []), key=lambda m: int(m["id"]))
        if after is None:
            return rows[-limit:]
        return [m for m in rows if int(m["id"]) > int(after)][:limit]

    def send_message(self, channel_id, text):
        self._next += 1
        self.sent.append((channel_id, text))
        return [str(self._next)]

    def edit_message(self, channel_id, message_id, text):
        self.edited.append((channel_id, message_id, text))

    def delete_message(self, channel_id, message_id):
        self.deleted.append((channel_id, message_id))

    def delete_message_once(self, channel_id, message_id, timeout):
        self.deleted_once.append((channel_id, message_id, timeout))
        if message_id in self.fail_delete_once:
            raise RuntimeError("429 Too Many Requests")


class FakeRainbox:
    def __init__(self, messages: dict[str, list[dict]] | None = None, config: Any = None):
        self.messages = messages or {}
        self.posted: list[tuple[str, str]] = []
        self.config = config            # dict | None (404) | Exception
        self.config_calls = 0
        self._next_id = 100

    def post_message(self, room_uuid, text):
        self.posted.append((room_uuid, text))
        self._next_id += 1
        return {"id": self._next_id}

    def get_messages_after(self, room_uuid, after_id):
        return [m for m in self.messages.get(room_uuid, []) if m["id"] > after_id]

    def get_message(self, room_uuid, message_id):
        return next((m for m in self.messages.get(room_uuid, []) if m["id"] == message_id), None)

    def get_connector_config(self, connector_uuid):
        self.config_calls += 1
        if isinstance(self.config, Exception):
            raise self.config
        return self.config


def _policy(**over):
    base = {"allowed_senders": ["111"], "forward_kinds": ["message", "notice", "progress"],
            "poll_seconds": 2, "mirror_progress": True, "direction": "both"}
    base.update(over)
    return base


def _binding(uuid=B1, room=ROOM, channel="777", enabled=True, effective=True, policy=None):
    return {"uuid": uuid, "room_uuid": room, "address": {"channel_id": channel}, "enabled": enabled,
            "effective_enabled": effective, "policy": policy or _policy()}


def _payload(bindings=None, enabled=True, revision="r1", **over):
    d = {"schema_version": 1, "revision": revision,
         "connector": {"uuid": CONN, "name": "Main Bot", "platform": "discord", "base_url": None,
                       "identity": None, "token_env": "DISCORD_TOKEN_T", "enabled": enabled, "launch_mode": "launcher"},
         "bindings": [_binding()] if bindings is None else bindings}
    d.update(over)
    return d


def _dmsg(mid: str, author_id: str = "111", content: str | None = "hello", bot: bool = False) -> dict[str, Any]:
    author: dict[str, Any] = {"id": author_id}
    if bot:
        author["bot"] = True
    return {"id": mid, "author": author, "content": content or ""}


def _row(mid: int, kind="message", sender_type="agent", text="t", streaming=False):
    return {"id": mid, "kind": kind, "sender_type": sender_type, "text": text, "streaming": streaming}


def _bridge(tmp_path, discord=None, rainbox=None, snapshot: dict | None = None, fresh=True) -> Bridge:
    tmp_path.mkdir(parents=True, exist_ok=True)
    store = StateStore(tmp_path / f"bridge-{CONN}.json")
    store.load(CONN)
    config = ConfigState()
    b = Bridge(CONN, store, config, rainbox or FakeRainbox(), discord or FakeDiscord(), token="SECRET")
    if snapshot is not None:
        snap = parse_snapshot(snapshot, CONN)
        config.publish(snap, config.epoch)
        if not fresh:
            config.invalidate("test")
    return b


def _rec(room=ROOM, channel="777", after="0", cursor=0, progress=None):
    return {"room_uuid": room, "address": {"channel_id": channel}, "address_key": f"channel_id={channel}",
            "discord_after": after, "room_cursor": cursor, "progress_messages": progress or {}}


# --- snapshot parsing -----------------------------------------------------


def test_parse_snapshot_accepts_the_wire_shape_and_resolves_policy():
    snap = parse_snapshot(_payload(), CONN)
    assert snap.enabled and snap.token_env == "DISCORD_TOKEN_T" and snap.revision == "r1"
    b = snap.bindings[B1]
    assert b.channel_id == "777" and b.address_key == "channel_id=777" and b.room_uuid == ROOM
    assert b.policy.allowed_senders == frozenset({"111"}) and b.policy.inbound and b.policy.outbound
    assert snap.active() == [b] and snap.for_room(ROOM) == [b] and snap.for_room(ROOM2) == []


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(schema_version=2),
    lambda d: d.pop("revision"),
    lambda d: d["connector"].update(uuid=B1),
    lambda d: d["connector"].update(platform="telegram"),
    lambda d: d["connector"].update(token_env="not valid"),
    lambda d: d["connector"].update(enabled="yes"),
    lambda d: d.update(bindings=[_binding(channel="abc")]),
    lambda d: d.update(bindings=[_binding(), _binding(uuid=B2)]),          # same address twice
    lambda d: d.update(bindings=[_binding(policy=_policy(direction="sideways"))]),
    lambda d: d.update(bindings=[_binding(policy=_policy(poll_seconds=0.1))]),
    lambda d: d.update(bindings=[_binding(policy=_policy(forward_kinds=["thinking"]))]),
    lambda d: d.update(bindings=[_binding(policy=_policy(allowed_senders="111"))]),
    lambda d: d.update(bindings=[{**_binding(), "effective_enabled": "true"}]),
])
def test_parse_snapshot_rejects_bad_shapes(mutate):
    d = _payload()
    mutate(d)
    with pytest.raises(ConfigError):
        parse_snapshot(d, CONN)


# --- freshness --------------------------------------------------------------


def test_freshness_is_a_fact_not_a_clock():
    cs = ConfigState()
    stop = threading.Event()
    assert cs.current() == (None, False)
    cs.invalidate("stream connected")
    epoch = cs.wait_for_request(stop)
    snap = parse_snapshot(_payload(), CONN)
    assert cs.publish(snap, epoch) is True and cs.current() == (snap, True)
    # An event that lands during a fetch leaves the result stale and re-arms the request.
    cs.invalidate("bridge_config event")
    epoch2 = cs.wait_for_request(stop)
    cs.invalidate("stream dropped")
    assert cs.publish(snap, epoch2) is False and cs.current() == (snap, False)
    assert cs.wait_for_request(stop) == cs.epoch      # re-armed by the invalidation
    stop.set()
    assert cs.wait_for_request(stop) is None


def test_may_act_gates_on_freshness_connector_binding_and_direction(tmp_path):
    b = _bridge(tmp_path, snapshot=_payload(bindings=[
        _binding(), _binding(uuid=B2, room=ROOM2, channel="888", policy=_policy(direction="in"))]))
    assert b.may_act(B1, "in") and b.may_act(B1, "out")
    assert b.may_act(B2, "in") and not b.may_act(B2, "out")
    assert not b.may_act("nope", "in")
    b.config.invalidate("stream dropped")
    assert not b.may_act(B1, "in")
    with pytest.raises(Paused):
        b.require(B1, "out")
    b2 = _bridge(tmp_path / "x", snapshot=_payload(enabled=False))
    assert not b2.may_act(B1, "in")
    b3 = _bridge(tmp_path / "y", snapshot=_payload(bindings=[_binding(effective=False)]))
    assert not b3.may_act(B1, "out")


# --- state file ---------------------------------------------------------------


def test_state_store_loads_saves_and_refuses_foreign_or_corrupt_files(tmp_path):
    path = tmp_path / "s.json"
    store = StateStore(path)
    assert store.load(CONN) == "missing"
    store.set_binding(B1, _rec())
    again = StateStore(path)
    assert again.load(CONN) == "ok" and again.binding(B1)["address_key"] == "channel_id=777"
    with pytest.raises(ConfigError):
        StateStore(path).load(B2)               # another connector's file
    path.write_text("{not json")
    with pytest.raises(ConfigError):
        StateStore(path).load(CONN)
    path.write_text(json.dumps({"discord_after": "5", "room_cursor": 3}))   # legacy schema
    with pytest.raises(ConfigError, match="import_legacy"):
        StateStore(path).load(CONN)


def test_state_lock_is_exclusive(tmp_path):
    path = tmp_path / "s.json"
    handle = cb.acquire_state_lock(path)
    with pytest.raises(SystemExit) as info:
        cb.acquire_state_lock(path)
    assert info.value.code == cb.EXIT_LOCK_HELD
    handle.close()
    cb.acquire_state_lock(path).close()   # released with the handle; the file stays
    assert path.with_name("s.json.lock").exists()


# --- activation, inbound, outbound -------------------------------------------


def test_activation_sets_both_high_water_marks_and_never_replays(tmp_path):
    dc = FakeDiscord({"777": [_dmsg("10"), _dmsg("30")]})
    rb = FakeRainbox({ROOM: [_row(3), _row(9)]})
    b = _bridge(tmp_path, dc, rb, snapshot=_payload())
    b.apply_snapshot(b.config.snapshot)
    rec = b.store.binding(B1)
    assert rec["discord_after"] == "30" and rec["room_cursor"] == 9
    assert dc.sent == [] and rb.posted == []
    assert b.first_applied


def test_disabled_new_binding_is_not_activated_until_enabled(tmp_path):
    b = _bridge(tmp_path, snapshot=_payload(bindings=[_binding(enabled=False, effective=False)]))
    b.apply_snapshot(b.config.snapshot)
    assert b.store.binding(B1) is None


def test_inbound_routes_by_binding_policy_and_advances_cursor(tmp_path):
    dc = FakeDiscord()
    rb = FakeRainbox()
    b = _bridge(tmp_path, dc, rb, snapshot=_payload())
    rec = _rec()
    b.store.set_binding(B1, rec)
    binding = b.config.snapshot.bindings[B1]
    b.process_inbound(binding, rec, [
        _dmsg("10", content="hi"), _dmsg("11", author_id="999"), _dmsg("12", bot=True), _dmsg("13", content="  "), _dmsg("14", content="there")])
    assert rb.posted == [(ROOM, "hi"), (ROOM, "there")]
    assert rec["discord_after"] == "14"
    assert json.loads(b.store.path.read_text())["bindings"][B1]["discord_after"] == "14"


def test_inbound_stops_before_posting_when_config_goes_stale(tmp_path):
    rb = FakeRainbox()
    b = _bridge(tmp_path, FakeDiscord(), rb, snapshot=_payload())
    rec = _rec(after="9")
    binding = b.config.snapshot.bindings[B1]
    b.config.invalidate("bridge_config event")
    with pytest.raises(Paused):
        b.process_inbound(binding, rec, [_dmsg("10")])
    assert rb.posted == [] and rec["discord_after"] == "9"   # not advanced: fetched again later


def test_outbound_forwards_only_policy_kinds_and_checks_each_chunk(tmp_path):
    dc = FakeDiscord()
    rb = FakeRainbox({ROOM: [_row(1, text="x" * 2001), _row(2, kind="notice", text="n"), _row(3, kind="thinking")]})
    b = _bridge(tmp_path, dc, rb, snapshot=_payload(bindings=[_binding(policy=_policy(forward_kinds=["message"]))]))
    rec = _rec()
    b.outbound_catchup(b.config.snapshot.bindings[B1], rec)
    assert [len(t) for _, t in dc.sent] == [2000, 1] and rec["room_cursor"] == 3
    # A second binding of the same room, direction "in", gets nothing outbound.
    b2 = _bridge(tmp_path / "z", dc, rb, snapshot=_payload(bindings=[_binding(policy=_policy(direction="in"))]))
    with pytest.raises(Paused):
        b2.send(b2.config.snapshot.bindings[B1], "hello")


def test_progress_mirroring_and_mirror_progress_false(tmp_path):
    dc = FakeDiscord()
    rb = FakeRainbox({ROOM: [_row(7, kind="progress", text="step 1")]})
    b = _bridge(tmp_path, dc, rb, snapshot=_payload())
    rec = _rec()
    b.store.set_binding(B1, rec)
    b.handle_room_event(ROOM, {"room_uuid": ROOM, "message_id": 7, "event": "insert", "kind": "progress"})
    assert dc.sent == [("777", "step 1")] and rec["progress_messages"] == {"7": "1001"}
    b.handle_room_event(ROOM, {"room_uuid": ROOM, "message_id": 7, "event": "update", "kind": "progress", "streaming": False, "text": "step 2"})
    assert dc.edited == [("777", "1001", "step 2")]
    rb.messages[ROOM] = [_row(8, text="reply")]
    b.handle_room_event(ROOM, {"room_uuid": ROOM, "message_id": 8, "event": "insert", "kind": "message", "deleted_progress_ids": [7]})
    assert dc.deleted == [("777", "1001")] and rec["progress_messages"] == {} and dc.sent[-1] == ("777", "reply")
    # mirror_progress=false: every update is a new message; nothing is edited.
    dc2 = FakeDiscord()
    b2 = _bridge(tmp_path / "m", dc2, FakeRainbox(), snapshot=_payload(bindings=[_binding(policy=_policy(mirror_progress=False))]))
    rec2 = _rec()
    b2.store.set_binding(B1, rec2)
    for text in ("a", "b"):
        b2.handle_room_event(ROOM, {"room_uuid": ROOM, "message_id": 7, "event": "update", "kind": "progress", "streaming": False, "text": text})
    assert [t for _, t in dc2.sent] == ["a", "b"] and dc2.edited == [] and rec2["progress_messages"] == {}


def test_room_events_are_dropped_while_stale_and_two_bindings_share_a_room(tmp_path):
    dc = FakeDiscord()
    rb = FakeRainbox({ROOM: [_row(5, text="reply")]})
    b = _bridge(tmp_path, dc, rb, snapshot=_payload(bindings=[_binding(), _binding(uuid=B2, channel="888")]))
    b.store.set_binding(B1, _rec())
    b.store.set_binding(B2, _rec(channel="888"))
    b.config.invalidate("stream dropped")
    b.handle_room_event(ROOM, {"room_uuid": ROOM, "message_id": 5, "event": "insert", "kind": "message"})
    assert dc.sent == []
    b.config.publish(b.config.snapshot, b.config.epoch)
    b.handle_room_event(ROOM, {"room_uuid": ROOM, "message_id": 5, "event": "insert", "kind": "message"})
    assert sorted(dc.sent) == [("777", "reply"), ("888", "reply")]
    assert b.store.binding(B1)["room_cursor"] == 5 and b.store.binding(B2)["room_cursor"] == 5


# --- removal and orphans -----------------------------------------------------


def test_removed_binding_cleanup_is_bounded_once_per_message_then_pruned(tmp_path, caplog):
    dc = FakeDiscord()
    dc.fail_delete_once.add("m2")
    b = _bridge(tmp_path, dc, snapshot=_payload(bindings=[]))
    b.first_applied = True
    rec = _rec(progress={"1": "m1", "2": "m2", "3": "m3"})
    b.store.set_binding(B1, rec)
    with caplog.at_level("WARNING"):
        b.apply_snapshot(b.config.snapshot)
    assert [(c, m) for c, m, _ in dc.deleted_once] == [("777", "m1"), ("777", "m2"), ("777", "m3")]
    assert all(t <= cb.CLEANUP_REQUEST_TIMEOUT_SECONDS for _, _, t in dc.deleted_once)
    assert dc.deleted == []                       # never the retrying path
    assert b.store.binding(B1) is None
    assert "m2" in caplog.text and "delete them by hand" in caplog.text


def test_cleanup_stops_on_deadline_disablement_or_staleness(tmp_path):
    dc = FakeDiscord()
    b = _bridge(tmp_path, dc, snapshot=_payload(bindings=[]))
    rec = _rec(progress={"1": "m1", "2": "m2"})
    clock = [0.0]

    def tick():
        clock[0] += 20.0
        return clock[0]
    b.cleanup_removed(B1, rec, clock=tick)             # first check at 20s, second past 30s
    assert [m for _, m, _ in dc.deleted_once] == ["m1"]
    dc2 = FakeDiscord()
    b2 = _bridge(tmp_path / "d", dc2, snapshot=_payload(bindings=[], enabled=False))
    b2.cleanup_removed(B1, _rec(progress={"1": "m1"}))
    assert dc2.deleted_once == []                        # disabled connector: no remote request
    dc3 = FakeDiscord()
    b3 = _bridge(tmp_path / "s", dc3, snapshot=_payload(bindings=[]), fresh=False)
    b3.cleanup_removed(B1, _rec(progress={"1": "m1"}))
    assert dc3.deleted_once == []                        # stale: no remote request


def test_orphaned_state_on_first_snapshot_is_reported_and_pruned_without_remote_calls(tmp_path, caplog):
    dc = FakeDiscord()
    b = _bridge(tmp_path, dc, snapshot=_payload(bindings=[]))
    b.store.set_binding(B1, _rec(progress={"1": "m1"}))
    with caplog.at_level("WARNING"):
        b.apply_snapshot(b.config.snapshot)
    assert dc.deleted_once == [] and dc.deleted == []
    assert b.store.binding(B1) is None and "manual cleanup" in caplog.text and "m1" in caplog.text


def test_state_room_or_address_mismatch_is_not_used(tmp_path, caplog):
    dc = FakeDiscord()
    rb = FakeRainbox({ROOM: [_row(5, text="reply")]})
    b = _bridge(tmp_path, dc, rb, snapshot=_payload())
    b.store.set_binding(B1, _rec(room=ROOM2))            # state claims another room
    with caplog.at_level("ERROR"):
        b.apply_snapshot(b.config.snapshot)
    assert dc.sent == [] and "not using that state" in caplog.text


def test_delete_and_create_between_fetches_retires_old_before_new(tmp_path):
    dc = FakeDiscord({"777": [_dmsg("50")]})
    rb = FakeRainbox({ROOM: [_row(5)]})
    b = _bridge(tmp_path, dc, rb, snapshot=_payload())
    b.apply_snapshot(b.config.snapshot)
    b.store.binding(B1)["progress_messages"] = {"9": "old-bubble"}
    b.store.save()
    replacement = parse_snapshot(_payload(bindings=[_binding(uuid=B2)], revision="r2"), CONN)
    b.config.publish(replacement, b.config.epoch)
    b.apply_snapshot(replacement)
    assert [m for _, m, _ in dc.deleted_once] == ["old-bubble"]
    assert b.store.binding(B1) is None
    assert b.store.binding(B2)["discord_after"] == "50" and b.store.binding(B2)["progress_messages"] == {}


# --- loops --------------------------------------------------------------------


def test_config_loop_fetches_on_request_publishes_and_backs_off_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(cb, "_backoff", lambda attempt: 0.0)
    rb = FakeRainbox(config=_payload())
    b = _bridge(tmp_path, FakeDiscord(), rb)
    stop = threading.Event()
    got: list[Snapshot] = []

    def on_snapshot(snap):
        got.append(snap)
        stop.set()
    b.config.invalidate("stream connected")
    config_loop(b, stop, on_snapshot)
    assert len(got) == 1 and b.config.current()[1] is True and rb.config_calls == 1
    # A 404 pauses without retrying; a failure retries with backoff.
    rb.config = None
    stop.clear()
    b.config.invalidate("bridge_config event")
    t = threading.Thread(target=config_loop, args=(b, stop, on_snapshot), daemon=True)
    t.start()
    time.sleep(0.2)
    assert b.config.current()[1] is False and rb.config_calls == 2
    rb.config = RuntimeError("timeout")
    b.config.invalidate("bridge_config event")
    time.sleep(0.2)
    assert rb.config_calls > 3                              # retrying
    rb.config = _payload(revision="r3")
    time.sleep(0.3)
    stop.set(); t.join(2)
    assert got[-1].revision == "r3" and b.config.current()[1] is True


class _OneShotDiscord(FakeDiscord):
    def __init__(self, batch, stop):
        super().__init__()
        self._batch, self._stop = batch, stop

    def get_messages(self, channel_id, after, limit=100):
        self._stop.set()
        return self._batch


def test_inbound_loop_polls_active_bindings_then_stops(tmp_path):
    stop = threading.Event()
    rb = FakeRainbox()
    b = _bridge(tmp_path, _OneShotDiscord([_dmsg("5", content="yo")], stop), rb, snapshot=_payload())
    b.store.set_binding(B1, _rec())
    inbound_loop(b, stop)
    assert rb.posted == [(ROOM, "yo")] and b.store.binding(B1)["discord_after"] == "5"


def test_inbound_loop_waits_for_config_when_nothing_is_active(tmp_path):
    stop = threading.Event()
    dc = FakeDiscord({"777": [_dmsg("5")]})
    b = _bridge(tmp_path, dc, FakeRainbox(), snapshot=_payload(enabled=False))
    b.store.set_binding(B1, _rec())
    threading.Timer(0.2, stop.set).start()
    inbound_loop(b, stop)
    assert b.store.binding(B1)["discord_after"] == "0"      # never polled


class _SSERainbox(FakeRainbox):
    def __init__(self, events, stop, **kw):
        super().__init__(**kw)
        self._events, self._stop = events, stop

    def iter_sse_events(self):
        for e in self._events:
            yield e
        self._stop.set()


def test_outbound_loop_refetches_on_connect_and_on_its_own_bridge_config_event(tmp_path):
    stop = threading.Event()
    rb = _SSERainbox([
        {"event": "stream_open"},
        {"event": "bridge_config", "connector_uuid": B2},
        {"event": "bridge_config", "connector_uuid": CONN},
        {"room_uuid": ROOM, "message_id": 5, "event": "insert", "kind": "message"},
    ], stop, messages={ROOM: [_row(5, text="reply")]})
    dc = FakeDiscord()
    b = _bridge(tmp_path, dc, rb, snapshot=_payload())
    b.store.set_binding(B1, _rec())
    epochs = []
    orig = b.config.invalidate

    def spy(reason):
        epochs.append(reason)
        orig(reason)
    b.config.invalidate = spy  # type: ignore[method-assign]
    outbound_loop(b, stop)
    assert epochs == ["stream connected", "bridge_config event"]
    assert dc.sent == []                          # the room event arrived while stale: dropped, cursor catches up later


def test_applier_runs_latest_snapshot_and_catches_up(tmp_path):
    dc = FakeDiscord({"777": []})
    rb = FakeRainbox({ROOM: [_row(1, text="old")]})
    b = _bridge(tmp_path, dc, rb, snapshot=_payload())
    applier = Applier(b)
    stop = threading.Event()
    t = threading.Thread(target=applier.run, args=(stop,), daemon=True)
    t.start()
    applier.submit(b.config.snapshot)
    time.sleep(0.2)
    assert b.store.binding(B1)["room_cursor"] == 1 and dc.sent == []   # activated at newest; no replay
    rb.messages[ROOM].append(_row(2, text="new"))
    applier.submit(b.config.snapshot)                                  # a re-apply catches up
    time.sleep(0.2)
    stop.set(); t.join(2)
    assert dc.sent == [("777", "new")]


# --- startup helpers -----------------------------------------------------------


def test_verify_identity_exit_codes_and_recording(tmp_path):
    class Rejecting(FakeDiscord):
        def get_me(self):
            exc = RuntimeError("401 on GET /users/@me: Unauthorized")
            exc.status = 401  # type: ignore[attr-defined]
            raise exc
    b = _bridge(tmp_path, Rejecting(), snapshot=_payload())
    with pytest.raises(SystemExit) as info:
        cb.verify_identity(b, threading.Event())
    assert info.value.code == cb.EXIT_CONFIG_REJECTED
    good = _bridge(tmp_path / "g", FakeDiscord(), snapshot=_payload())
    assert cb.verify_identity(good, threading.Event())["id"] == "bot-1"
    assert good.store.data["remote_identity"] == "bot-1"
    other = FakeDiscord(); other.me = {"id": "bot-2"}
    mismatch = Bridge(CONN, good.store, good.config, FakeRainbox(), other, token="t")
    with pytest.raises(SystemExit) as info2:
        cb.verify_identity(mismatch, threading.Event())
    assert info2.value.code == cb.EXIT_CONFIG_REJECTED


def test_errors_never_log_the_token(tmp_path, caplog):
    b = _bridge(tmp_path, FakeDiscord(), FakeRainbox(), snapshot=_payload())
    with caplog.at_level("ERROR"):
        b.log_error("x", RuntimeError("401 for Bot SECRET"))
    assert "SECRET" not in caplog.text and "<redacted>" in caplog.text
