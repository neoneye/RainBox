"""Base-agent heartbeat: while handle() runs, a background thread emits periodic
heartbeat status messages so the supervisor's 60s silence-watchdog doesn't
SIGKILL a slow-but-healthy turn (e.g. a reasoning model). No DB/model needed —
we drive Agent._handle_with_heartbeat directly with a recording sender."""

import threading
import time
from uuid import uuid4

from agents.base import Agent


class _SlowAgent(Agent):
    HEARTBEAT_INTERVAL = 0.05

    def handle(self, journal_id, payload):
        time.sleep(0.22)  # ~4 heartbeat intervals
        return {"ok": True, "did": "work"}


class _FastAgent(Agent):
    HEARTBEAT_INTERVAL = 0.05

    def handle(self, journal_id, payload):
        return {"ok": True}


class _BoomAgent(Agent):
    HEARTBEAT_INTERVAL = 0.05

    def handle(self, journal_id, payload):
        time.sleep(0.12)
        raise RuntimeError("boom")


def _recorder():
    sent = []
    lock = threading.Lock()

    def send(msg):
        with lock:
            sent.append(msg)

    return sent, send


def test_heartbeats_emitted_during_slow_handle():
    sent, send = _recorder()
    agent = _SlowAgent(agent_uuid=uuid4(), name="slow", send=send)
    result = agent._handle_with_heartbeat(42, {})
    assert result == {"ok": True, "did": "work"}
    beats = [m for m in sent if m.get("status") == "heartbeat"]
    assert len(beats) >= 2, sent          # ~0.22s / 0.05s should give several
    assert all(b["journal_id"] == 42 for b in beats)


def test_no_heartbeat_for_fast_handle():
    sent, send = _recorder()
    agent = _FastAgent(agent_uuid=uuid4(), name="fast", send=send)
    agent._handle_with_heartbeat(1, {})
    assert [m for m in sent if m.get("status") == "heartbeat"] == []


def test_heartbeat_thread_stops_after_handle():
    sent, send = _recorder()
    agent = _SlowAgent(agent_uuid=uuid4(), name="slow", send=send)
    agent._handle_with_heartbeat(7, {})
    before = sum(1 for m in sent if m.get("status") == "heartbeat")
    time.sleep(0.2)  # well past the interval; the beat thread must be gone
    after = sum(1 for m in sent if m.get("status") == "heartbeat")
    assert after == before
    assert not any(t.name.startswith("hb-") for t in threading.enumerate())


def test_heartbeat_stops_when_handle_raises():
    sent, send = _recorder()
    agent = _BoomAgent(agent_uuid=uuid4(), name="boom", send=send)
    try:
        agent._handle_with_heartbeat(9, {})
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected the handle exception to propagate")
    assert not any(t.name.startswith("hb-") for t in threading.enumerate())
