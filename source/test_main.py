"""The launcher against real child interpreters and a fake core on a
socketpair.

No real service, model, or network is involved: a temporary catalogue points
at a `server.py` whose behaviour is chosen through the (declared) environment,
its `venv/bin/python` is a symlink to this interpreter, and the "core" is the
other end of a socketpair the test holds — it sends desired-state lines and
reads status lines exactly like `core.py` does over `--control-fd`. Time is a
fake monotonic clock the tests advance; real waits are only for real children
to exit."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

import main as L
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
if mode == "print":
    print("hello from stdout", flush=True)
    print("warning on stderr", file=sys.stderr, flush=True)
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

# A stand-in core for the real-child tests: takes --control-fd like core.py,
# sends one desired snapshot, then sleeps until SIGTERM (exit 0).
FAKE_CORE_PY = r'''
import json, os, signal, socket, sys, time
fd = int(sys.argv[sys.argv.index("--control-fd") + 1])
sock = socket.socket(fileno=fd)
sock.sendall((json.dumps({"type": "desired", "schema_version": 1, "core_pid": os.getpid(),
    "core_restart_nonce": None,
    "services": [{"key": "svc", "kind": "svc", "enabled": True, "restart_nonce": "n1", "env": {}}]}) + "\n").encode())
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
    """The test's end of the control socketpair."""

    def __init__(self) -> None:
        self.mine, self.theirs = socket.socketpair()
        self.mine.setblocking(False)
        self._buf = b""
        self.statuses: list[dict] = []

    def send_desired(self, snapshot: dict) -> None:
        self.mine.sendall((json.dumps(snapshot) + "\n").encode())

    def send_raw(self, data: bytes) -> None:
        self.mine.sendall(data)

    def drain(self) -> list[dict]:
        while True:
            try:
                chunk = self.mine.recv(65536)
            except BlockingIOError:
                break
            if not chunk:
                break
            self._buf += chunk
        while b"\n" in self._buf:
            raw, self._buf = self._buf.split(b"\n", 1)
            self.statuses.append(json.loads(raw))
        return self.statuses

    def close(self) -> None:
        self.mine.close()


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
    (tmp_path / "fake_core.py").write_text(FAKE_CORE_PY)
    return tmp_path


def _base_env() -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp"),
            "LC_ALL": "C", "SECRET_TOKEN": "s3cret", "PYTHONPATH": "/nope", "DATABASE_URL": "x"}


def _kill_all(l: L.Launcher) -> None:
    for rec in (*l.services.values(), l.core):
        if rec.pgid:
            try:
                os.killpg(rec.pgid, 9)
            except (ProcessLookupError, PermissionError):
                pass
        if rec.proc is not None:
            rec.proc.wait(timeout=5)


@pytest.fixture
def lch(tree: Path, core: FakeCore):
    clock = FakeClock()
    l = L.Launcher(state_dir=tree / "state", catalogue={"svc": KIND}, source_dir=tree,
                   spawn_core=False, base_env=_base_env(), clock=clock)
    l.acquire_lock()
    l.attach_core_socket(core.theirs)
    l.clock_obj = clock  # type: ignore[attr-defined]
    try:
        yield l
    finally:
        _kill_all(l)


def desired(*, enabled=True, nonce="n1", env=None, core_nonce=None):
    return {"type": "desired", "schema_version": 1, "core_pid": 4242, "core_restart_nonce": core_nonce,
            "services": [{"key": "svc", "kind": "svc", "enabled": enabled,
                          "restart_nonce": nonce, "env": env or {}}]}


def push(l: L.Launcher, core: FakeCore, snapshot: dict) -> None:
    """The core pushes a snapshot; the launcher's next tick reads it."""
    core.send_desired(snapshot)
    l.tick(l.clock_obj.t)  # type: ignore[attr-defined]


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


def svc(l: L.Launcher) -> L.Proc:
    return l.services["svc"]


# --- pure functions -----------------------------------------------------------


def test_main_launcher_imports_only_the_standard_library():
    code = ("import sys; import main; "
            "print('\\n'.join(sorted(m for m in sys.modules)))")
    out = subprocess.run([sys.executable, "-c", code], cwd=str(Path(__file__).parent),
                         capture_output=True, text=True, check=True).stdout.split()
    heavy = ("flask", "sqlalchemy", "requests", "db", "webapp", "llama_index",
             "torch", "psycopg", "services.registry", "dotenv", "providers", "http")
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
    assert L.parse_credentials("API_KEY=foo=bar") == {"API_KEY": "foo=bar"}
    assert L.parse_credentials('PAD="  spaced  "') == {"PAD": "  spaced  "}
    assert L.parse_credentials("URL=https://h/x?a=1&b=2#frag") == {"URL": "https://h/x?a=1&b=2#frag"}
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
    good = desired(env={"TEST_MODE": "x"})
    snap = L.validate_desired(good, cat)
    assert snap.services["svc"].env == {"TEST_MODE": "x"} and snap.core_pid == 4242

    def variant(**changes):
        d = json.loads(json.dumps(good))
        d.update(changes)
        return d

    bad_cases = [
        variant(schema_version=2), variant(core_pid="1"), variant(services=[]),
        variant(services=good["services"] * 2),
        variant(services=[{**good["services"][0], "kind": "nope"}]),
        variant(services=[{**good["services"][0], "env": {"SECRET": "x"}}]),
        variant(services=[{**good["services"][0], "enabled": "yes"}]),
        variant(services=[{**good["services"][0], "token_env": "X"}]),
        "not an object",
    ]
    for bad in bad_cases:
        with pytest.raises(ValueError):
            L.validate_desired(bad, cat)


def test_core_port_override_is_shared_with_core():
    assert L.core_addr_from_env({}) == ("127.0.0.1", 5000)
    assert L.core_addr_from_env({"RAINBOX_CORE_PORT": "5090"}) == ("127.0.0.1", 5090)
    with pytest.raises(SystemExit):
        L.core_addr_from_env({"RAINBOX_CORE_PORT": "x"})
    core_py = (Path(__file__).parent / "core.py").read_text()
    assert 'os.environ.get("RAINBOX_CORE_PORT"' in core_py


# --- the control channel ------------------------------------------------------


def test_no_service_before_first_valid_snapshot(lch: L.Launcher, core: FakeCore):
    lch.tick(lch.clock_obj.t)  # type: ignore[attr-defined]
    assert svc(lch).proc is None and core.drain() == []  # nothing said yet, nothing sent
    core.send_raw(b'{"garbage": true}\n{"type": "desired"}\nnot json\n')
    lch.tick(lch.clock_obj.t)  # type: ignore[attr-defined]
    assert svc(lch).proc is None and svc(lch).state == "stopped"


def test_first_snapshot_from_a_core_triggers_a_full_status_push(lch: L.Launcher, core: FakeCore):
    push(lch, core, desired(enabled=False))
    statuses = core.drain()
    assert len(statuses) == 1
    assert statuses[0]["type"] == "status" and statuses[0]["sequence"] == 1
    assert statuses[0]["services"]["svc"]["state"] == "stopped"
    assert statuses[0]["services"]["core"]["state"] == "stopped"
    lch.tick(lch.clock_obj.t)  # type: ignore[attr-defined]
    assert len(core.drain()) == 1  # nothing changed: nothing more is sent, ever


def test_status_is_pushed_only_on_change(lch: L.Launcher, core: FakeCore):
    clock = lch.clock_obj  # type: ignore[attr-defined]
    push(lch, core, desired(enabled=False))
    n = len(core.drain())
    clock.t += 3600 * 5
    for _ in range(10):
        lch.tick(clock.t)
    assert len(core.drain()) == n
    push(lch, core, desired(enabled=True))
    wait_for(lch, lambda: svc(lch).state == "running")
    seqs = [s["sequence"] for s in core.drain()]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs) and len(seqs) > n
    assert core.statuses[-1]["services"]["svc"]["state"] == "running"


def test_core_eof_closes_the_channel(lch: L.Launcher, core: FakeCore):
    push(lch, core, desired(enabled=True))
    wait_for(lch, lambda: svc(lch).state == "running")
    core.mine.close()
    lch.tick(lch.clock_obj.t)  # type: ignore[attr-defined]
    assert lch.core_channel is None
    assert svc(lch).state == "running"  # losing the core never stops a service


def test_new_core_channel_learns_status_after_its_first_snapshot(lch: L.Launcher, core: FakeCore):
    push(lch, core, desired(enabled=True))
    wait_for(lch, lambda: svc(lch).state == "running")
    core.drain()
    replacement = FakeCore()
    try:
        lch.attach_core_socket(replacement.theirs)
        lch.tick(lch.clock_obj.t)  # type: ignore[attr-defined]
        assert replacement.drain() == []  # not before it has spoken
        push(lch, replacement, desired(enabled=True))
        table = replacement.drain()
        assert table and table[-1]["services"]["svc"]["state"] == "running"
    finally:
        replacement.close()


# --- supervision with real children -------------------------------------------


def test_enable_spawns_child_as_own_session_child_of_launcher(lch: L.Launcher, core: FakeCore, tree: Path):
    out = tree / "out.txt"
    push(lch, core, desired(env={"TEST_OUT": str(out)}))
    wait_for(lch, lambda: svc(lch).state == "running" and out.exists())
    time.sleep(0.1)
    info = eval(out.read_text())
    assert info["ppid"] == os.getpid()
    assert info["pgid"] == info["pid"] == svc(lch).pid
    env = info["env"]
    assert env["TEST_OUT"] == str(out) and env["LC_ALL"] == "C" and "PATH" in env
    for forbidden in ("SECRET_TOKEN", "PYTHONPATH", "DATABASE_URL"):
        assert forbidden not in env
    assert core.drain()[-1]["services"]["svc"]["state"] == "running"
    assert core.statuses[-1]["services"]["svc"]["pid"] == svc(lch).pid


def test_disable_is_a_clean_stop_not_a_crash(lch: L.Launcher, core: FakeCore):
    push(lch, core, desired())
    wait_for(lch, lambda: svc(lch).state == "running")
    time.sleep(0.4)  # let the child install its SIGTERM handler
    push(lch, core, desired(enabled=False))
    assert svc(lch).state == "stopping"
    wait_for(lch, lambda: svc(lch).state == "stopped")
    assert svc(lch).last_exit == 0 and svc(lch).crash_times == [] and svc(lch).proc is None
    assert core.drain()[-1]["services"]["svc"]["state"] == "stopped"


def test_nonce_change_restarts_only_that_service(lch: L.Launcher, core: FakeCore):
    push(lch, core, desired(nonce="n1"))
    wait_for(lch, lambda: svc(lch).state == "running")
    first = svc(lch).pid
    time.sleep(0.4)
    push(lch, core, desired(nonce="n1"))  # same nonce again: nothing happens
    assert svc(lch).pid == first and svc(lch).state == "running"
    push(lch, core, desired(nonce="n2"))
    wait_for(lch, lambda: svc(lch).state == "running" and svc(lch).pid != first)
    assert svc(lch).last_exit == 0  # the old one was stopped, not crashed


def test_exit_2_latches_failed_until_a_new_nonce(lch: L.Launcher, core: FakeCore):
    push(lch, core, desired(env={"TEST_MODE": "exit", "TEST_CODE": "2"}))
    wait_for(lch, lambda: svc(lch).state == "failed")
    assert "exit 2" in svc(lch).message
    lch.clock_obj.t += 200  # type: ignore[attr-defined]
    lch.tick(lch.clock_obj.t)  # type: ignore[attr-defined]
    assert svc(lch).state == "failed" and svc(lch).proc is None
    push(lch, core, desired(nonce="n2", env={"TEST_MODE": "sleep"}))
    wait_for(lch, lambda: svc(lch).state == "running")


def test_crash_backs_off_then_exhausts_budget(lch: L.Launcher, core: FakeCore):
    clock = lch.clock_obj  # type: ignore[attr-defined]
    push(lch, core, desired(env={"TEST_MODE": "exit", "TEST_CODE": "1"}))
    wait_for(lch, lambda: svc(lch).state == "backoff")
    assert svc(lch).next_retry == pytest.approx(clock.t + 2.0, abs=0.5)
    for _ in range(1, L.CRASH_BUDGET):
        clock.t = svc(lch).next_retry + 0.01
        wait_for(lch, lambda: svc(lch).state in ("backoff", "failed"))
        if svc(lch).state == "failed":
            break
    assert svc(lch).state == "failed" and "unexpected exits" in svc(lch).message


def test_unexpected_exit_0_counts_as_a_crash(lch: L.Launcher, core: FakeCore):
    push(lch, core, desired(env={"TEST_MODE": "exit", "TEST_CODE": "0"}))
    wait_for(lch, lambda: svc(lch).state == "backoff")
    assert svc(lch).last_exit == 0


def test_missing_interpreter_is_not_installed_not_a_crash(lch: L.Launcher, core: FakeCore, tree: Path):
    (tree / "svc" / "venv" / "bin" / "python").unlink()
    push(lch, core, desired())
    assert svc(lch).state == "not installed" and svc(lch).proc is None
    assert "venv/bin/python" in svc(lch).message
    assert core.drain()[-1]["services"]["svc"]["state"] == "not installed"


def test_sigterm_ignorer_is_killed_after_the_grace(lch: L.Launcher, core: FakeCore):
    clock = lch.clock_obj  # type: ignore[attr-defined]
    push(lch, core, desired(env={"TEST_IGNORE_TERM": "1"}))
    wait_for(lch, lambda: svc(lch).state == "running")
    time.sleep(0.3)
    push(lch, core, desired(enabled=False))
    assert svc(lch).state == "stopping"
    time.sleep(0.3)
    lch.tick(clock.t)
    assert svc(lch).state == "stopping"  # SIGTERM ignored
    clock.t += L.TERM_GRACE + 0.1
    wait_for(lch, lambda: svc(lch).state == "stopped")
    assert svc(lch).last_exit == -9


def test_grandchild_survivors_block_respawn_until_group_is_gone(lch: L.Launcher, core: FakeCore):
    clock = lch.clock_obj  # type: ignore[attr-defined]
    push(lch, core, desired(env={"TEST_MODE": "grandchild"}))
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
    wait_for(lch, lambda: len(svc(lch).crash_times) > crashes_before or svc(lch).state == "failed")


def test_invalid_snapshot_keeps_the_last_valid_one(lch: L.Launcher, core: FakeCore):
    push(lch, core, desired())
    wait_for(lch, lambda: svc(lch).state == "running")
    pid = svc(lch).pid
    good = desired(enabled=False)
    push(lch, core, {**good, "schema_version": 9})
    push(lch, core, {**good, "services": []})
    core.send_raw(b"garbage\n")
    lch.tick(lch.clock_obj.t)  # type: ignore[attr-defined]
    assert svc(lch).state == "running" and svc(lch).pid == pid


def test_restart_blocked_keeps_running_child(lch: L.Launcher, core: FakeCore, tree: Path):
    push(lch, core, desired(nonce="n1"))
    wait_for(lch, lambda: svc(lch).state == "running")
    pid = svc(lch).pid
    (tree / "svc" / "server.py").unlink()
    push(lch, core, desired(nonce="n2"))
    assert svc(lch).state == "running" and svc(lch).pid == pid
    assert svc(lch).message.startswith("restart blocked")
    assert not svc(lch).pending_restart
    push(lch, core, desired(nonce="n2"))  # the same nonce is not retried
    assert svc(lch).pid == pid


def test_core_only_suppresses_services(tree: Path, core: FakeCore):
    clock = FakeClock()
    l = L.Launcher(state_dir=tree / "state2", catalogue={"svc": KIND}, source_dir=tree,
                   spawn_core=False, base_env=_base_env(), clock=clock, core_only=True)
    l.acquire_lock()
    l.attach_core_socket(core.theirs)
    core.send_desired(desired())
    l.tick(clock.t)
    assert l.services["svc"].proc is None
    assert l.services["svc"].message == "suppressed by --core-only"
    assert core.drain()[-1]["launcher"]["core_only"] is True


def test_lock_is_exclusive(tree: Path, lch: L.Launcher):
    other = L.Launcher(state_dir=lch.state_dir, catalogue={"svc": KIND}, source_dir=tree,
                       spawn_core=False, base_env=_base_env())
    with pytest.raises(SystemExit) as info:
        other.acquire_lock()
    assert info.value.code == 3
    assert (lch.state_dir / "launcher.lock").exists()


def test_shutdown_requested_while_a_snapshot_is_pending_spawns_nothing(lch: L.Launcher, core: FakeCore):
    """A signal can land before a queued snapshot is read; the snapshot must
    not re-enable services."""
    core.send_desired(desired(enabled=True))
    lch.request_shutdown()
    lch.tick(lch.clock_obj.t)  # type: ignore[attr-defined]
    assert lch.shutting_down and svc(lch).proc is None and not svc(lch).desired
    assert lch.tick(lch.clock_obj.t) is False  # nothing to stop: shutdown completes


def test_second_signal_kills_orphaned_groups_before_exiting(lch: L.Launcher, core: FakeCore):
    clock = lch.clock_obj  # type: ignore[attr-defined]
    push(lch, core, desired(env={"TEST_MODE": "grandchild"}))
    wait_for(lch, lambda: svc(lch).state == "backoff")
    time.sleep(0.3)
    lch.tick(clock.t)
    pgid = svc(lch).pgid
    assert svc(lch).proc is None and lch._group_alive(pgid)
    lch.request_shutdown()
    lch.request_shutdown()  # second Ctrl-C
    assert lch.shutdown_phase == 3
    deadline = time.monotonic() + 5
    done = False
    while time.monotonic() < deadline and not done:
        done = not lch.tick(clock.t)
        time.sleep(0.05)
    assert done and not lch._group_alive(pgid)


def test_two_phase_shutdown_stops_services_before_the_core(tree: Path):
    """A real core child: it receives --control-fd, pushes a snapshot, and is
    stopped last."""
    clock = FakeClock()
    l = L.Launcher(state_dir=tree / "state3", catalogue={"svc": KIND}, source_dir=tree,
                   core_argv=[sys.executable, str(tree / "fake_core.py")], spawn_core=True,
                   base_env=_base_env(), clock=clock)
    l.acquire_lock()
    l.clock_obj = clock  # type: ignore[attr-defined]
    try:
        l.tick(clock.t)
        assert l.core.state == "running" and l.core_channel is not None
        wait_for(l, lambda: l.services["svc"].state == "running")  # the snapshot arrived
        time.sleep(0.5)  # let both children install their SIGTERM handlers
        l.request_shutdown()
        l.tick(clock.t)
        assert l.services["svc"].state == "stopping"
        assert l.core.proc is not None and l.core.proc.poll() is None  # core alive in phase 1
        wait_for(l, lambda: l.shutdown_phase == 2)
        assert l.services["svc"].state == "stopped"
        wait_for(l, lambda: l.core.proc is None)
        assert l.tick(clock.t) is False
        assert l.core.last_exit == 0
    finally:
        _kill_all(l)


def test_core_child_gets_the_launcher_selected_port_and_fd(tree: Path, monkeypatch):
    captured = {}

    class FakePopen:
        def __init__(self, argv, **kw):
            captured["argv"], captured["env"], captured["pass_fds"] = argv, kw["env"], kw["pass_fds"]
            self.pid = 424242
            r, w = os.pipe()
            os.close(w)
            self.stdout = os.fdopen(r, "rb")

        def poll(self):
            return None

    monkeypatch.setattr(L.subprocess, "Popen", FakePopen)
    l = L.Launcher(state_dir=tree / "state5", catalogue={"svc": KIND}, source_dir=tree,
                   core_addr=("127.0.0.1", 5090), spawn_core=True, base_env=_base_env())
    l.acquire_lock()
    l.tick(0.0)
    assert captured["env"][L.CORE_PORT_ENV] == "5090"
    fd = int(captured["argv"][captured["argv"].index("--control-fd") + 1])
    assert captured["pass_fds"] == (fd,)
    assert l.core_channel is not None


# --- idle behaviour -----------------------------------------------------------


def test_idle_loop_has_no_timer_but_the_inactivity_log(lch: L.Launcher, core: FakeCore):
    clock = lch.clock_obj  # type: ignore[attr-defined]
    push(lch, core, desired(enabled=False))
    t0 = clock.t
    assert lch.next_deadline(t0) - t0 == pytest.approx(L.INACTIVITY_STEPS[0], abs=0.01)
    svc(lch).next_retry = t0 + 1.5
    assert lch.next_deadline(t0) - t0 == pytest.approx(1.5)
    svc(lch).next_retry = None
    svc(lch).stop_deadline = t0 + 0.7
    assert lch.next_deadline(t0) - t0 == pytest.approx(0.7)
    svc(lch).stop_deadline = None
    assert lch.next_deadline(t0 + 100000) == t0 + 100000  # never in the past


def test_inactivity_is_logged_at_1_10_60_minutes_then_hourly(lch: L.Launcher, core: FakeCore, caplog):
    clock = lch.clock_obj  # type: ignore[attr-defined]
    push(lch, core, desired(enabled=False))
    t0 = clock.t
    caplog.set_level("INFO", logger="launcher")

    def lines():
        return [r.getMessage() for r in caplog.records if "no activity" in r.getMessage()]

    for offset in (30, 59):
        clock.t = t0 + offset
        lch.tick(clock.t)
    assert lines() == []
    clock.t = t0 + 60
    lch.tick(clock.t)
    assert lines() == ["no activity for 1 minutes"]
    clock.t = t0 + 599
    lch.tick(clock.t)
    assert len(lines()) == 1
    clock.t = t0 + 600
    lch.tick(clock.t)
    assert lines()[-1] == "no activity for 10 minutes"
    clock.t = t0 + 3600
    lch.tick(clock.t)
    assert lines()[-1] == "no activity for 60 minutes"
    clock.t = t0 + 7200
    lch.tick(clock.t)
    assert lines()[-1] == "no activity for 120 minutes"
    assert len(lines()) == 4
    # Activity resets the ladder.
    push(lch, core, desired(enabled=False, nonce="n2"))
    assert lch.next_deadline(clock.t) - clock.t == pytest.approx(L.INACTIVITY_STEPS[0], abs=0.01)


# --- backpressure, output, credentials ------------------------------------------


class SlowChannel(L.CoreChannel):
    """A CoreChannel whose send() reports a full socket while `blocked`."""
    blocked = False

    def send(self, data: bytes) -> int:
        if self.blocked:
            raise BlockingIOError()
        return super().send(data)


def test_slow_core_never_loses_the_channel_and_gets_the_newest_table(tree: Path, core: FakeCore):
    clock = FakeClock()
    lch = L.Launcher(state_dir=tree / "state-slow", catalogue={"svc": KIND}, source_dir=tree,
                     spawn_core=False, base_env=_base_env(), clock=clock, channel_cls=SlowChannel)
    lch.acquire_lock()
    lch.attach_core_socket(core.theirs)
    lch.clock_obj = clock  # type: ignore[attr-defined]
    push(lch, core, desired(enabled=False))
    n = len(core.drain())
    channel = lch.core_channel
    assert isinstance(channel, SlowChannel)
    channel.blocked = True
    lch._status_dirty = True
    lch.tick(lch.clock_obj.t)  # type: ignore[attr-defined]
    lch._status_dirty = True
    lch.tick(lch.clock_obj.t)  # type: ignore[attr-defined]
    assert lch.core_channel is channel and lch.wants_write   # kept, waiting for writable
    assert len(core.drain()) == n
    channel.blocked = False
    lch.tick(lch.clock_obj.t)  # type: ignore[attr-defined]
    delivered = core.drain()[n:]
    assert len(delivered) == 1                             # two changes coalesced to the newest table
    assert delivered[0]["sequence"] == lch.sequence
    assert not lch.wants_write


def test_peer_gone_on_send_drops_the_channel(lch: L.Launcher, core: FakeCore):
    push(lch, core, desired(enabled=False))
    core.drain()
    core.mine.close()
    lch._status_dirty = True
    lch.tick(lch.clock_obj.t)  # type: ignore[attr-defined]
    assert lch.core_channel is None and not lch.wants_write


def test_child_output_is_prefixed_with_its_key(tree: Path, core: FakeCore):
    clock = FakeClock()
    seen: list[tuple[str, str]] = []
    l = L.Launcher(state_dir=tree / "state8", catalogue={"svc": KIND}, source_dir=tree,
                   spawn_core=False, base_env=_base_env(), clock=clock, on_output=lambda k, line: seen.append((k, line)))
    l.acquire_lock()
    l.attach_core_socket(core.theirs)
    l.clock_obj = clock  # type: ignore[attr-defined]
    try:
        push(l, core, desired(env={"TEST_MODE": "print"}))
        wait_for(l, lambda: len(seen) >= 2)
        assert ("svc", "hello from stdout") in seen and ("svc", "warning on stderr") in seen
    finally:
        _kill_all(l)


def test_credentials_file_wins_over_the_boot_environment(tree: Path, core: FakeCore):
    l = L.Launcher(state_dir=tree / "state9", catalogue={"svc": KIND}, source_dir=tree,
                   spawn_core=False, base_env={**_base_env(), "BOT_TOKEN": "from-env", "ONLY_ENV": "e"})
    l.acquire_lock()
    assert l.resolve_credential("BOT_TOKEN") == ("environment", "from-env")
    (l.state_dir / "credentials.env").write_text("BOT_TOKEN=from-file\nEMPTY=\n")
    assert l.resolve_credential("BOT_TOKEN") == ("file", "from-file")   # an edit takes effect at once
    assert l.resolve_credential("ONLY_ENV") == ("environment", "e")     # fallback for names the file lacks
    assert l.resolve_credential("EMPTY") is None                        # empty = unset at either level
    assert l.resolve_credential("NOPE") is None
