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
    jid = uuid4()
    result = agent._handle_with_heartbeat(jid, {})
    assert result == {"ok": True, "did": "work"}
    beats = [m for m in sent if m.get("status") == "heartbeat"]
    assert len(beats) >= 2, sent          # ~0.22s / 0.05s should give several
    # journal_id is emitted as a string (uuid) so the status JSON stays serializable.
    assert all(b["journal_id"] == str(jid) for b in beats)


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


def test_beat_loop_survives_a_failed_send(monkeypatch):
    """One failed send used to `return`, silencing heartbeats for the rest of
    the turn — the supervisor then SIGKILLed a healthy run HEARTBEAT_TIMEOUT
    later, far enough from the hiccup to look unrelated. A transient failure
    must not cost the turn."""
    import threading

    from agents.base import Agent

    sent: list[dict] = []
    calls = {"n": 0}

    def flaky(msg):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("transient socket hiccup")
        sent.append(msg)

    agent = Agent.__new__(Agent)
    agent.name = "probe"
    agent._send = flaky
    agent._send_lock = threading.Lock()
    agent._active_journal_id = uuid4()

    # Beat by hand rather than waiting on the real 20s timer.
    for _ in range(3):
        try:
            agent._emit_heartbeat()
        except Exception:  # what the loop now swallows
            pass

    assert calls["n"] == 3
    # The two sends after the failure still landed.
    assert len(sent) == 2


def test_beat_loop_body_does_not_return_on_exception():
    """Pins the shape, since the behavioural test above drives _emit_heartbeat
    directly rather than the thread: the except branch must not exit the loop."""
    import inspect

    from agents.base import Agent

    src = inspect.getsource(Agent._handle_with_heartbeat)
    beat = src[src.index("def _beat()"):src.index("hb = threading.Thread")]
    assert "except Exception:" in beat
    # No exit from the loop other than the stop event.
    assert "return" not in beat
    assert "break" not in beat
