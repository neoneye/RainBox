"""The launcher against real child interpreters and a fake in-process core.

No real service, model, or network is involved: a temporary catalogue points
at a `server.py` whose behaviour is chosen through the (declared) environment,
its `venv/bin/python` is a symlink to this interpreter, and the "core" is a
tiny `http.server` in a thread that serves whatever desired-state the test
sets and records every status post. Time is a fake monotonic clock the tests
advance; real waits are only for real children to exit."""
from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

import pytest

import launcher as L
from services.definitions import ServiceKind

SERVER_PY = r'''
import os, signal, subprocess, sys, time
mode = os.environ.get("TEST_MODE", "sleep")
out = os.environ.get("TEST_OUT")
if out:
    with open(out, "w") as f:
        f.write(repr({"ppid": os.getppid(), "pgid": os.getpgid(0), "pid": os.getpid(),
                      "env": dict(os.environ)}))
if mode == "exit":
    sys.exit(int(os.environ.get("TEST_CODE", "1")))
if mode == "grandchild":
    subprocess.Popen([sys.executable, "-c",
        "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"])
    time.sleep(0.8)  # let the grandchild install SIG_IGN before the leader dies
    sys.exit(0)
if os.environ.get("TEST_IGNORE_TERM"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
else:
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
while True:
    time.sleep(0.05)
'''

KIND = ServiceKind(
    kind="svc", directory="svc", argv=("venv/bin/python", "server.py"),
    bind="127.0.0.1:0",
    env_keys=("TEST_MODE", "TEST_CODE", "TEST_OUT", "TEST_IGNORE_TERM"),
    description="test service",
)


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class FakeCore:
    """Serves /services/api/desired from `self.desired()` and records status posts."""

    def __init__(self) -> None:
        self.statuses: list[dict] = []
        self.desired: Callable[[], dict | None] = lambda: None
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args):  # quiet
                pass

            def _send(self, status, body: bytes):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/services/api/desired":
                    d = outer.desired()
                    if d is None:
                        return self._send(409, b'{"error":"unmanaged"}')
                    return self._send(200, json.dumps(d).encode())
                if self.path == "/big":
                    return self._send(200, b"[" + b"1," * 600000 + b"1]")
                if self.path == "/trickle":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    for _ in range(40):
                        try:
                            self.wfile.write(b" ")
                            self.wfile.flush()
                        except OSError:
                            return
                        time.sleep(0.1)
                    return
                self._send(404, b"{}")

            def do_POST(self):
                n = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(n) or b"null")
                if self.path == "/services/api/status":
                    outer.statuses.append(body)
                    return self._send(200, b'{"ok":true}')
                self._send(404, b"{}")

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        host, port = self.server.server_address[:2]
        self.addr: tuple[str, int] = (str(host), int(port))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def core():
    c = FakeCore()
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    svc = tmp_path / "svc"
    (svc / "venv" / "bin").mkdir(parents=True)
    (svc / "venv" / "bin" / "python").symlink_to(sys.executable)
    (svc / "server.py").write_text(SERVER_PY)
    return tmp_path


def _base_env() -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp"),
            "LC_ALL": "C", "SECRET_TOKEN": "s3cret", "PYTHONPATH": "/nope", "DATABASE_URL": "x"}


@pytest.fixture
def lch(tree: Path, core: FakeCore):
    clock = FakeClock()
    l = L.Launcher(state_dir=tree / "state", catalogue={"svc": KIND}, source_dir=tree,
                   core_addr=core.addr, spawn_core=False, base_env=_base_env(), clock=clock)
    l.acquire_lock()
    l.clock_obj = clock  # type: ignore[attr-defined]
    try:
        yield l
    finally:
        for rec in (*l.services.values(), l.core):
            if rec.pgid:
                try:
                    os.killpg(rec.pgid, 9)
                except ProcessLookupError:
                    pass
            if rec.proc is not None:
                rec.proc.wait(timeout=5)


def desired_for(l: L.Launcher, *, enabled=True, nonce="n1", env=None, core_nonce=None):
    return {"schema_version": 1, "launcher_id": l.launcher_id, "core_instance_id": l.core_instance_id,
            "core_pid": 4242, "core_restart_nonce": core_nonce,
            "services": [{"key": "svc", "kind": "svc", "enabled": enabled,
                          "restart_nonce": nonce, "env": env or {}}]}


def wait_for(l: L.Launcher, pred, timeout=8.0):
    clock = l.clock_obj  # type: ignore[attr-defined]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        l.tick(clock.t)
        if pred():
            return
        time.sleep(0.03)
        clock.t += 0.03
    raise AssertionError("condition not met in time")


def poll_now(l: L.Launcher):
    l._next_poll = 0.0
    l.tick(l.clock_obj.t)  # type: ignore[attr-defined]


def svc(l: L.Launcher) -> L.Proc:
    return l.services["svc"]


# --- pure functions -----------------------------------------------------------


def test_launcher_imports_only_the_standard_library():
    code = ("import sys; import launcher; "
            "print('\\n'.join(sorted(m for m in sys.modules)))")
    out = subprocess.run([sys.executable, "-c", code], cwd=str(Path(__file__).parent),
                         capture_output=True, text=True, check=True).stdout.split()
    heavy = ("flask", "sqlalchemy", "requests", "db", "webapp", "llama_index",
             "torch", "psycopg", "services.registry", "dotenv", "providers")
    leaked = [m for m in out if m == "db" or any(m == h or m.startswith(h + ".") for h in heavy)]
    assert leaked == []


def test_parse_credentials_grammar():
    text = "\n".join([
        "# comment", "", "  A = plain value with # hash ", 'B="quoted # kept"',
        "C='single'", "D=", "E=\"\"",
    ])
    assert L.parse_credentials(text) == {
        "A": "plain value with # hash", "B": "quoted # kept", "C": "single", "D": "", "E": "",
    }
    for bad in ("A=1\nA=2", "A=\"open", "1A=x", "novalue", "A='x'y", "A=\"a\"b\""):
        with pytest.raises(L.CredentialsError) as info:
            L.parse_credentials(bad)
        assert "line" in str(info.value)
        assert "open" not in str(info.value) and "novalue" not in str(info.value)


def test_service_environment_is_built_from_scratch():
    env = L.service_environment(_base_env(), {"TEST_MODE": "sleep"}, ("BOT_TOKEN", "t"))
    assert env["PATH"] and env["HOME"] and env["LC_ALL"] == "C"
    assert env["TEST_MODE"] == "sleep" and env["BOT_TOKEN"] == "t"
    for forbidden in ("SECRET_TOKEN", "PYTHONPATH", "DATABASE_URL"):
        assert forbidden not in env
    with pytest.raises(ValueError):
        L.service_environment(_base_env(), {"PYTHONPATH": "/x"})


def test_validate_desired_rejects_everything_off():
    cat = {"svc": KIND}
    good = {"schema_version": 1, "launcher_id": "L", "core_instance_id": "C", "core_pid": 1,
            "core_restart_nonce": None,
            "services": [{"key": "svc", "kind": "svc", "enabled": True, "restart_nonce": "n", "env": {"TEST_MODE": "x"}}]}
    snap = L.validate_desired(good, "L", "C", cat)
    assert snap.services["svc"].env == {"TEST_MODE": "x"} and snap.core_pid == 1

    def variant(**changes):
        d = json.loads(json.dumps(good))
        d.update(changes)
        return d

    bad_cases = [
        variant(schema_version=2), variant(launcher_id="other"), variant(core_instance_id="other"),
        variant(core_pid="1"), variant(services=[]),
        variant(services=good["services"] * 2),
        variant(services=[{**good["services"][0], "kind": "nope"}]),
        variant(services=[{**good["services"][0], "env": {"SECRET": "x"}}]),
        variant(services=[{**good["services"][0], "enabled": "yes"}]),
        variant(services=[{**good["services"][0], "token_env": "X"}]),
        "not an object",
    ]
    for bad in bad_cases:
        with pytest.raises(ValueError):
            L.validate_desired(bad, "L", "C", cat)


def test_http_json_bounds_size_and_elapsed_time(core: FakeCore):
    with pytest.raises(L.ControlError):
        L.http_json(core.addr, "GET", "/big")
    t0 = time.monotonic()
    with pytest.raises(L.ControlError):
        L.http_json(core.addr, "GET", "/trickle", deadline_s=0.5)
    assert time.monotonic() - t0 < 2.0
    status, data = L.http_json(core.addr, "GET", "/nothing")
    assert status == 404 and data == {}


# --- supervision with real children -------------------------------------------


def test_enable_spawns_child_as_own_session_child_of_launcher(lch: L.Launcher, core: FakeCore, tree: Path):
    out = tree / "out.txt"
    core.desired = lambda: desired_for(lch, env={"TEST_OUT": str(out)})
    poll_now(lch)
    wait_for(lch, lambda: svc(lch).state == "running" and out.exists())
    time.sleep(0.1)
    info = eval(out.read_text())
    assert info["ppid"] == os.getpid()
    assert info["pgid"] == info["pid"] == svc(lch).pid
    env = info["env"]
    assert env["TEST_OUT"] == str(out) and env["LC_ALL"] == "C" and "PATH" in env
    for forbidden in ("SECRET_TOKEN", "PYTHONPATH", "DATABASE_URL"):
        assert forbidden not in env
    assert core.statuses and core.statuses[-1]["services"]["svc"]["state"] == "running"
    assert core.statuses[-1]["services"]["svc"]["pid"] == svc(lch).pid


def test_no_service_before_first_valid_snapshot(lch: L.Launcher, core: FakeCore):
    core.desired = lambda: None  # unmanaged 409
    poll_now(lch)
    assert svc(lch).state == "stopped" and svc(lch).proc is None
    core.desired = lambda: {"garbage": True}
    poll_now(lch)
    assert svc(lch).proc is None


def test_disable_is_a_clean_stop_not_a_crash(lch: L.Launcher, core: FakeCore):
    core.desired = lambda: desired_for(lch)
    poll_now(lch)
    wait_for(lch, lambda: svc(lch).state == "running")
    time.sleep(0.4)  # let the child install its SIGTERM handler
    core.desired = lambda: desired_for(lch, enabled=False)
    poll_now(lch)
    assert svc(lch).state == "stopping"
    wait_for(lch, lambda: svc(lch).state == "stopped")
    assert svc(lch).last_exit == 0 and svc(lch).crash_times == [] and svc(lch).proc is None
    assert core.statuses[-1]["services"]["svc"]["state"] == "stopped"


def test_nonce_change_restarts_only_that_service(lch: L.Launcher, core: FakeCore):
    core.desired = lambda: desired_for(lch, nonce="n1")
    poll_now(lch)
    wait_for(lch, lambda: svc(lch).state == "running")
    first = svc(lch).pid
    time.sleep(0.4)  # let the child install its SIGTERM handler
    poll_now(lch)  # same nonce again: nothing happens
    assert svc(lch).pid == first and svc(lch).state == "running"
    core.desired = lambda: desired_for(lch, nonce="n2")
    poll_now(lch)
    wait_for(lch, lambda: svc(lch).state == "running" and svc(lch).pid != first)
    assert svc(lch).last_exit == 0  # the old one was stopped, not crashed


def test_exit_2_latches_failed_until_a_new_nonce(lch: L.Launcher, core: FakeCore):
    core.desired = lambda: desired_for(lch, env={"TEST_MODE": "exit", "TEST_CODE": "2"})
    poll_now(lch)
    wait_for(lch, lambda: svc(lch).state == "failed")
    assert "exit 2" in svc(lch).message
    lch.clock_obj.t += 200  # type: ignore[attr-defined]
    poll_now(lch)
    assert svc(lch).state == "failed" and svc(lch).proc is None
    core.desired = lambda: desired_for(lch, nonce="n2", env={"TEST_MODE": "sleep"})
    poll_now(lch)
    wait_for(lch, lambda: svc(lch).state == "running")


def test_crash_backs_off_then_exhausts_budget(lch: L.Launcher, core: FakeCore):
    clock = lch.clock_obj  # type: ignore[attr-defined]
    core.desired = lambda: desired_for(lch, env={"TEST_MODE": "exit", "TEST_CODE": "1"})
    poll_now(lch)
    wait_for(lch, lambda: svc(lch).state == "backoff")
    assert svc(lch).next_retry == pytest.approx(clock.t + 2.0, abs=0.5)
    pids = set()
    for n in range(1, L.CRASH_BUDGET):
        clock.t = svc(lch).next_retry + 0.01
        wait_for(lch, lambda: svc(lch).state in ("backoff", "failed"))
        pids.add(svc(lch).pid)
        if svc(lch).state == "failed":
            break
    assert svc(lch).state == "failed" and "unexpected exits" in svc(lch).message


def test_unexpected_exit_0_counts_as_a_crash(lch: L.Launcher, core: FakeCore):
    core.desired = lambda: desired_for(lch, env={"TEST_MODE": "exit", "TEST_CODE": "0"})
    poll_now(lch)
    wait_for(lch, lambda: svc(lch).state == "backoff")
    assert svc(lch).last_exit == 0


def test_missing_interpreter_is_not_installed_not_a_crash(lch: L.Launcher, core: FakeCore, tree: Path):
    (tree / "svc" / "venv" / "bin" / "python").unlink()
    core.desired = lambda: desired_for(lch)
    poll_now(lch)
    assert svc(lch).state == "not installed" and svc(lch).proc is None
    assert "venv/bin/python" in svc(lch).message
    assert core.statuses[-1]["services"]["svc"]["state"] == "not installed"


def test_sigterm_ignorer_is_killed_after_the_grace(lch: L.Launcher, core: FakeCore):
    clock = lch.clock_obj  # type: ignore[attr-defined]
    core.desired = lambda: desired_for(lch, env={"TEST_IGNORE_TERM": "1"})
    poll_now(lch)
    wait_for(lch, lambda: svc(lch).state == "running")
    time.sleep(0.3)  # let the child install its handler
    core.desired = lambda: desired_for(lch, enabled=False)
    poll_now(lch)
    assert svc(lch).state == "stopping"
    time.sleep(0.3)
    lch.tick(clock.t)
    assert svc(lch).state == "stopping"  # SIGTERM ignored
    clock.t += L.TERM_GRACE + 0.1
    wait_for(lch, lambda: svc(lch).state == "stopped")
    assert svc(lch).last_exit == -9


def test_grandchild_survivors_block_respawn_until_group_is_gone(lch: L.Launcher, core: FakeCore):
    clock = lch.clock_obj  # type: ignore[attr-defined]
    core.desired = lambda: desired_for(lch, env={"TEST_MODE": "grandchild"})
    poll_now(lch)
    wait_for(lch, lambda: svc(lch).state == "backoff")
    time.sleep(0.3)
    lch.tick(clock.t)
    assert svc(lch).group_drain_deadline is not None
    assert lch._group_alive(svc(lch).pgid)
    clock.t = svc(lch).next_retry + 0.01  # retry is due, but the group lingers
    lch.tick(clock.t)
    assert svc(lch).proc is None
    crashes_before = len(svc(lch).crash_times)
    clock.t += L.TERM_GRACE + 0.1
    lch.tick(clock.t)  # escalation kills the group
    time.sleep(0.2)
    # The respawn happens only once the group is gone; its leader exits at
    # once again (grandchild mode), so count crashes rather than watch pid.
    wait_for(lch, lambda: len(svc(lch).crash_times) > crashes_before or svc(lch).state == "failed")


def test_invalid_snapshot_keeps_the_last_valid_one(lch: L.Launcher, core: FakeCore):
    core.desired = lambda: desired_for(lch)
    poll_now(lch)
    wait_for(lch, lambda: svc(lch).state == "running")
    pid = svc(lch).pid
    good = desired_for(lch, enabled=False)
    core.desired = lambda: {**good, "launcher_id": "someone-else"}
    poll_now(lch)
    core.desired = lambda: {**good, "services": []}
    poll_now(lch)
    core.desired = lambda: None
    poll_now(lch)
    assert svc(lch).state == "running" and svc(lch).pid == pid


def test_restart_blocked_keeps_running_child(lch: L.Launcher, core: FakeCore, tree: Path):
    core.desired = lambda: desired_for(lch, nonce="n1")
    poll_now(lch)
    wait_for(lch, lambda: svc(lch).state == "running")
    pid = svc(lch).pid
    (tree / "svc" / "server.py").unlink()
    core.desired = lambda: desired_for(lch, nonce="n2")
    poll_now(lch)
    assert svc(lch).state == "running" and svc(lch).pid == pid
    assert svc(lch).message.startswith("restart blocked")
    assert not svc(lch).pending_restart
    poll_now(lch)  # the same nonce is not retried
    assert svc(lch).pid == pid


def test_core_only_suppresses_services(tree: Path, core: FakeCore):
    clock = FakeClock()
    l = L.Launcher(state_dir=tree / "state2", catalogue={"svc": KIND}, source_dir=tree,
                   core_addr=core.addr, spawn_core=False, base_env=_base_env(), clock=clock,
                   core_only=True)
    l.acquire_lock()
    core.desired = lambda: desired_for(l)
    l._next_poll = 0.0
    l.tick(clock.t)
    assert l.services["svc"].proc is None
    assert l.services["svc"].message == "suppressed by --core-only"
    assert core.statuses[-1]["launcher"]["core_only"] is True


def test_status_sequence_and_heartbeat(lch: L.Launcher, core: FakeCore):
    clock = lch.clock_obj  # type: ignore[attr-defined]
    core.desired = lambda: desired_for(lch, enabled=False)
    poll_now(lch)
    seqs = [s["sequence"] for s in core.statuses]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    n = len(core.statuses)
    lch.tick(clock.t)
    assert len(core.statuses) == n  # nothing changed, nothing posted
    clock.t += L.HEARTBEAT_INTERVAL + 0.1
    lch.tick(clock.t)
    assert len(core.statuses) == n + 1
    assert core.statuses[-1]["services"]["core"]["state"] == "stopped"


def test_lock_is_exclusive(tree: Path, core: FakeCore, lch: L.Launcher):
    other = L.Launcher(state_dir=lch.state_dir, catalogue={"svc": KIND}, source_dir=tree,
                       core_addr=core.addr, spawn_core=False, base_env=_base_env())
    with pytest.raises(SystemExit) as info:
        other.acquire_lock()
    assert info.value.code == 3
    assert (lch.state_dir / "launcher.lock").exists()


def test_two_phase_shutdown_stops_services_before_the_core(tree: Path, core: FakeCore):
    clock = FakeClock()
    fake_core = [sys.executable, "-c",
                 "import signal,sys,time; signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))\n"
                 "while True: time.sleep(0.05)"]
    l = L.Launcher(state_dir=tree / "state3", catalogue={"svc": KIND}, source_dir=tree,
                   core_addr=core.addr, core_argv=fake_core, spawn_core=True,
                   base_env=_base_env(), clock=clock)
    l.acquire_lock()
    l.clock_obj = clock  # type: ignore[attr-defined]
    core.desired = lambda: desired_for(l)
    try:
        l.tick(clock.t)
        assert l.core.state == "running"
        poll_now(l)
        wait_for(l, lambda: l.services["svc"].state == "running")
        core_pid = l.core.pid
        time.sleep(0.5)  # let both children install their SIGTERM handlers
        l.request_shutdown()
        l.tick(clock.t)
        assert l.services["svc"].state == "stopping"
        assert l.core.proc is not None and l.core.proc.poll() is None  # core alive in phase 1
        wait_for(l, lambda: l.shutdown_phase == 2)
        assert l.services["svc"].state == "stopped"
        assert l.core.stop_requested or l.core.proc is None
        wait_for(l, lambda: l.core.proc is None)
        assert l.tick(clock.t) is False
        assert l.core.last_exit == 0 and core_pid is not None
    finally:
        for rec in (l.core, *l.services.values()):
            if rec.pgid:
                try:
                    os.killpg(rec.pgid, 9)
                except ProcessLookupError:
                    pass
