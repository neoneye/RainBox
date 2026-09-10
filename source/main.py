"""Launcher: one small process that starts the rest.

    cd source && venv/bin/python main.py [--state-dir DIR] [--core-only]

The launcher is the root of rainbox's process tree and the way rainbox is
started. It starts the core (`core.py`: supervisor + webserver) and every side
service the operator has enabled on /settings, as *siblings*, keeps them
running, stops what is disabled, and reports what it sees. It never becomes
the core: it imports only the standard library plus the data-only catalogue
in `services/definitions.py`, so its footprint stays small and a restart of
the core never takes a service down.

Design: docs/superpowers/specs/2026-09-10-launcher-design.md. In short:

- Children are spawned with fork+exec (`subprocess.Popen`, own session), never
  fork alone; each child's parent pid is this process, which is what makes
  Activity Monitor's hierarchy legible.
- The core gets one end of a `socketpair()` as an inherited fd (`core.py
  --control-fd N`), the same way the core hands its agents theirs. Both
  directions are JSON lines and both are pushes: the core sends a desired-
  state snapshot at startup and whenever a service setting changes; the
  launcher sends its status table whenever a process changes. EOF on that
  socket is liveness. Nothing polls.
- Restarts are nonces: the core rewrites a service's nonce, the launcher sees
  it change and restarts the process. The core never executes anything.
- One single-threaded loop, asleep in `select()` on the core socket and the
  signal pipe until something happens: a line from the core, `SIGCHLD` for a
  child exit, `SIGTERM`/`SIGINT` to shut down, or one of its own deadlines
  (a stop escalation, a backoff retry, a shutdown phase). Idle, its only
  timed wakeups are the inactivity log lines at 1, 10, and 60 minutes and
  then hourly — enough to see it is alive without flooding the log.
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import json
import logging
import os
import re
import select
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from services.definitions import (
    ALL_KINDS,
    BASELINE_ENV_KEYS,
    BASELINE_ENV_PREFIXES,
    BRIDGE_KEY_PREFIX,
    CORE_KEY,
    EXIT_CONFIG_REJECTED,
    EXIT_LOCK_HELD,
    SCHEMA_VERSION,
    ServiceKind,
)

logger = logging.getLogger("launcher")

# The launcher is POSIX-only by construction — select() on a pipe, SIGCHLD,
# process groups, fcntl locks — as is the core (posix_spawn, socketpair fds).
# Say so at import rather than failing obscurely somewhere in the loop.
if os.name != "posix":
    raise SystemExit("main.py (the rainbox launcher) requires a POSIX system (macOS or Linux)")

SOURCE_DIR: Path = Path(__file__).resolve().parent
REPO_ROOT: Path = SOURCE_DIR.parent
DEFAULT_STATE_DIR: Path = REPO_ROOT / "var" / "services"
CORE_PORT_ENV = "RAINBOX_CORE_PORT"  # read by core.py too; default 5000


def core_addr_from_env(env: dict[str, str] | None = None) -> tuple[str, int]:
    raw = (os.environ if env is None else env).get(CORE_PORT_ENV, "5000")
    try:
        port = int(raw)
    except ValueError:
        raise SystemExit(f"{CORE_PORT_ENV} must be an integer port (got {raw!r})") from None
    return ("127.0.0.1", port)


CORE_ADDR: tuple[str, int] = ("127.0.0.1", 5000)

TERM_GRACE: float = 10.0          # SIGTERM -> SIGKILL, same as core.py's agents
BACKOFF_BASE: float = 2.0
BACKOFF_CAP: float = 60.0
CRASH_BUDGET: int = 5             # unexpected exits ...
CRASH_WINDOW: float = 120.0       # ... within this many seconds latch `failed`
BACKOFF_RESET_AFTER: float = 120.0
MAX_SLEEP: float = 3600.0         # longest the loop sleeps between passes
MAX_LINE_BYTES: int = 1 << 20     # a control line longer than this is garbage
MAX_OUTPUT_LINE: int = 1 << 16    # a child line without a newline is flushed at this size

# Inactivity log: after this long with nothing to do, say so — then at the
# next step, then every INACTIVITY_REPEAT. Long enough to prove liveness,
# sparse enough not to flood the log.
INACTIVITY_STEPS: tuple[float, ...] = (60.0, 600.0, 3600.0)
INACTIVITY_REPEAT: float = 3600.0

# Variables that must never leak from the launcher into a service, whatever
# the operator exported (loader injection, interpreter overrides, DB access).
FORBIDDEN_ENV_KEYS: frozenset[str] = frozenset({
    "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "DATABASE_URL",
    "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH", "LD_PRELOAD", "LD_LIBRARY_PATH",
})

_ENV_NAME_OK = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _print_child_line(key: str, line: str) -> None:
    """Default sink for child output: the launcher's own stdout, prefixed
    with the service key so interleaved logs stay attributable."""
    sys.stdout.write(f"[{key}] {line}\n")
    sys.stdout.flush()


# --- credentials file -------------------------------------------------------


class CredentialsError(ValueError):
    """A malformed `credentials.env`; the message names a line number, never
    the line's contents."""


def parse_credentials(text: str) -> dict[str, str]:
    """The deliberately limited `<state-dir>/credentials.env` grammar: one
    `NAME=value` per line, blank lines and `#` comment lines ignored,
    whitespace stripped around name and value, one matching pair of single or
    double quotes may surround the value (contents literal). No interpolation,
    escapes, multiline values, `export`, or inline comments — an unquoted `#`
    is part of the value. Malformed quoting and duplicate names raise."""
    out: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise CredentialsError(f"line {lineno}: expected NAME=value")
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not _ENV_NAME_OK.match(name):
            raise CredentialsError(f"line {lineno}: invalid variable name")
        if name in out:
            raise CredentialsError(f"line {lineno}: duplicate name {name}")
        if value[:1] in ("'", '"'):
            quote = value[0]
            if len(value) < 2 or value[-1] != quote:
                raise CredentialsError(f"line {lineno}: unterminated quote")
            value = value[1:-1]
            if quote in value:
                raise CredentialsError(f"line {lineno}: stray quote inside value")
        elif value.endswith(("'", '"')):
            raise CredentialsError(f"line {lineno}: unbalanced quote")
        out[name] = value
    return out


# --- environment ------------------------------------------------------------


def baseline_environment(source_env: dict[str, str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for key, value in source_env.items():
        if key in BASELINE_ENV_KEYS or key.startswith(BASELINE_ENV_PREFIXES):
            env[key] = value
    return env


def service_environment(
    source_env: dict[str, str], declared: dict[str, str],
    credential: tuple[str, str] | None = None,
) -> dict[str, str]:
    """A service child's environment, built from scratch: the baseline keys
    present in `source_env`, the catalogue-approved values in `declared`, and
    at most one credential. Nothing else from the parent gets through."""
    env = baseline_environment(source_env)
    for key, value in declared.items():
        if key in FORBIDDEN_ENV_KEYS:
            raise ValueError(f"forbidden environment key {key}")
        env[key] = str(value)
    if credential is not None:
        env[credential[0]] = credential[1]
    return env


# --- desired-state validation -------------------------------------------------


@dataclass(frozen=True)
class DesiredEntry:
    key: str
    kind: str
    enabled: bool
    restart_nonce: str | None
    env: dict[str, str]
    label: str | None = None
    token_env: str | None = None
    state_file_name: str | None = None


@dataclass(frozen=True)
class Snapshot:
    core_pid: int
    core_restart_nonce: str | None
    services: dict[str, DesiredEntry]


def validate_desired(payload: Any, catalogue: dict[str, ServiceKind]) -> Snapshot:
    """The whole message is validated before any of it is acted on; anything
    off leaves the last valid snapshot in force."""
    if not isinstance(payload, dict):
        raise ValueError("not an object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported schema_version")
    core_pid = payload.get("core_pid")
    if not isinstance(core_pid, int) or isinstance(core_pid, bool):
        raise ValueError("core_pid")
    core_nonce = payload.get("core_restart_nonce")
    if core_nonce is not None and not isinstance(core_nonce, str):
        raise ValueError("core_restart_nonce")
    raw = payload.get("services")
    if not isinstance(raw, list):
        raise ValueError("services must be a list")
    services: dict[str, DesiredEntry] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("service entry must be an object")
        key, kind = item.get("key"), item.get("kind")
        if not isinstance(key, str) or not isinstance(kind, str):
            raise ValueError("service key/kind")
        if key in services:
            raise ValueError(f"duplicate service key {key}")
        if kind not in catalogue:
            raise ValueError(f"unknown kind {kind!r}: launcher and core versions disagree")
        spec = catalogue[kind]
        label = token_env = state_name = None
        if spec.dynamic:
            # bridge:<uuid> entries: the uuid ties the key, BRIDGE_CONNECTOR,
            # and the state-file name together; credentials travel by NAME.
            if not key.startswith(BRIDGE_KEY_PREFIX) or not _UUID_RE.match(key[len(BRIDGE_KEY_PREFIX):]):
                raise ValueError(f"dynamic key {key!r} must be bridge:<uuid>")
            uuid_part = key[len(BRIDGE_KEY_PREFIX):]
            if set(item) - {"key", "kind", "enabled", "restart_nonce", "env", "label", "token_env", "state_file"}:
                raise ValueError(f"unexpected fields on {key}")
            token_env = item.get("token_env")
            if not isinstance(token_env, str) or not _ENV_NAME_OK.match(token_env):
                raise ValueError(f"{key}: token_env must be an environment variable name")
            sf = item.get("state_file")
            if (not isinstance(sf, dict) or sf.get("env") != spec.state_file_env
                    or sf.get("name") != f"bridge-{uuid_part}.json"):
                raise ValueError(f"{key}: state_file must name {spec.state_file_env} and bridge-<uuid>.json")
            state_name = sf["name"]
            raw_label = item.get("label", key)
            if not isinstance(raw_label, str) or not raw_label.isprintable() or len(raw_label) > 60:
                raise ValueError(f"{key}: label must be one printable line")
            label = raw_label.strip() or key
            if not isinstance(item.get("env"), dict) or item["env"].get("BRIDGE_CONNECTOR") != uuid_part:
                raise ValueError(f"{key}: env.BRIDGE_CONNECTOR must equal the key's uuid")
            if spec.state_file_env in item["env"] or token_env in item["env"]:
                raise ValueError(f"{key}: env may not carry the state-file or credential variable")
        else:
            if key != kind:
                raise ValueError(f"static service key {key!r} must equal its kind")
            if set(item) - {"key", "kind", "enabled", "restart_nonce", "env"}:
                raise ValueError(f"unexpected fields on {key}")
        enabled = item.get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError(f"{key}: enabled must be a boolean")
        nonce = item.get("restart_nonce")
        if nonce is not None and not isinstance(nonce, str):
            raise ValueError(f"{key}: restart_nonce")
        env = item.get("env", {})
        if not isinstance(env, dict):
            raise ValueError(f"{key}: env must be an object")
        allowed = set(catalogue[kind].env_keys)
        for var, value in env.items():
            if var not in allowed or not isinstance(value, str):
                raise ValueError(f"{key}: env key {var!r} not allowed")
        services[key] = DesiredEntry(key, kind, enabled, nonce, dict(env), label, token_env, state_name)
    missing = {k for k, spec in catalogue.items() if not spec.dynamic} - set(services)
    if missing:
        raise ValueError(f"snapshot lacks static services: {sorted(missing)}")
    return Snapshot(core_pid=core_pid, core_restart_nonce=core_nonce, services=services)


# --- the control channel ------------------------------------------------------


class CoreChannel:
    """The launcher's end of the control socketpair: the socket operations
    the loop needs, and nothing else. Subclass it to change behaviour — the
    tests subclass it to simulate a core that is slow to read (send raises
    BlockingIOError) or gone (send raises OSError) without touching a real
    socket's methods, which cannot be patched."""

    def __init__(self, sock: socket.socket) -> None:
        sock.setblocking(False)
        self._sock = sock

    def fileno(self) -> int:
        return self._sock.fileno()

    def send(self, data: bytes) -> int:
        """Non-blocking send; BlockingIOError when the socket is full,
        OSError when the peer is gone."""
        return self._sock.send(data)

    def recv(self, size: int) -> bytes:
        """Non-blocking receive; BlockingIOError when nothing is waiting,
        b"" at EOF."""
        return self._sock.recv(size)

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


# --- process records ---------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class Proc:
    key: str
    kind: ServiceKind | None            # None for the core
    state: str = "stopped"
    proc: subprocess.Popen | None = None
    pid: int | None = None
    pgid: int | None = None
    since: str | None = None
    last_exit: int | None = None
    message: str | None = None
    desired: bool = False
    env: dict[str, str] = field(default_factory=dict)
    nonce: str | None = None            # last consumed nonce (baseline)
    nonce_known: bool = False
    pending_restart: bool = False
    stop_requested: bool = False
    stop_deadline: float | None = None
    next_retry: float | None = None
    crash_times: list[float] = field(default_factory=list)
    backoff_exp: int = 0
    running_since: float | None = None
    group_drain_deadline: float | None = None
    credential_source: str | None = None
    out_fd: int | None = None            # the child's stdout+stderr pipe, non-blocking
    out_buf: bytes = b""
    label: str | None = None             # display/prefix name (dynamic entries)
    token_env: str | None = None         # credential variable NAME (dynamic entries)
    state_file_name: str | None = None   # basename under the state dir (dynamic entries)
    removed: bool = False                # a dynamic key that vanished from the snapshot

    def status(self) -> dict[str, Any]:
        rec: dict[str, Any] = {
            "state": self.state, "pid": self.pid if self.state in ("running", "stopping") else None,
            "since": self.since, "last_exit": self.last_exit, "message": self.message,
        }
        if self.state == "backoff" and self.next_retry is not None:
            rec["next_retry"] = self.next_retry
        if self.credential_source:
            rec["credential_source"] = self.credential_source
        if self.label:
            rec["label"] = self.label
        return rec


class Launcher:
    def __init__(
        self, *, state_dir: Path, core_only: bool = False,
        catalogue: dict[str, ServiceKind] | None = None,
        source_dir: Path = SOURCE_DIR, core_addr: tuple[str, int] = CORE_ADDR,
        core_argv: list[str] | None = None, spawn_core: bool = True,
        base_env: dict[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
        on_output: Callable[[str, str], None] | None = None,
        channel_cls: type[CoreChannel] = CoreChannel,
    ) -> None:
        self.state_dir = Path(state_dir).resolve()
        self.core_only = core_only
        self.catalogue = dict(ALL_KINDS if catalogue is None else catalogue)
        self.source_dir = Path(source_dir).resolve()
        self.core_addr = core_addr
        self.core_argv = core_argv or [sys.executable, str(self.source_dir / "core.py")]
        self.spawn_core = spawn_core
        self.base_env = dict(os.environ if base_env is None else base_env)
        self.clock = clock
        self.started_at = _utc_now()
        self.sequence = 0
        self.snapshot: Snapshot | None = None
        self.snapshot_valid_once = False
        self.core = Proc(CORE_KEY, None, desired=True)
        self.services: dict[str, Proc] = {
            key: Proc(key, kind) for key, kind in self.catalogue.items() if not kind.dynamic
        }
        self.shutting_down = False
        self.shutdown_phase = 0
        self.phase_deadline: float | None = None
        self.exit_code = 0
        self._status_dirty = True
        self._lock_fh = None
        self._signals = 0
        self.on_output = on_output or _print_child_line
        self.channel_cls = channel_cls
        # Outbound status: the line being written (may be partially sent) and
        # at most one newer complete table waiting behind it. A slow core
        # costs us nothing but a buffered line; only a dead peer closes.
        self._send_buf = b""
        self._send_buf_started = False   # some bytes of _send_buf already went out
        self._queued_status = b""
        # The control channel to the current core, and the read buffer.
        self.core_channel: CoreChannel | None = None
        self._core_buf = b""
        self._core_snapshot_seen = False  # this core has sent its first snapshot
        # Inactivity log bookkeeping.
        now = self.clock()
        self._last_activity = now
        self._inactivity_step = 0
        self._next_inactivity_log = now + INACTIVITY_STEPS[0]

    # --- lock / port -----------------------------------------------------------

    def acquire_lock(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.state_dir, 0o700)
        except OSError:
            pass
        fh = open(self.state_dir / "launcher.lock", "a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                raise SystemExit(EXIT_LOCK_HELD) from None
            raise
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        self._lock_fh = fh  # held for the launcher's lifetime; never unlinked

    # --- activity / inactivity log ---------------------------------------------

    def _note_activity(self, now: float) -> None:
        self._last_activity = now
        self._inactivity_step = 0
        self._next_inactivity_log = now + INACTIVITY_STEPS[0]

    def _inactivity_tick(self, now: float) -> None:
        if now < self._next_inactivity_log:
            return
        idle = now - self._last_activity
        logger.info("no activity for %d minutes", int(round(idle / 60.0)))
        self._inactivity_step += 1
        if self._inactivity_step < len(INACTIVITY_STEPS):
            self._next_inactivity_log = self._last_activity + INACTIVITY_STEPS[self._inactivity_step]
        else:
            self._next_inactivity_log = self._next_inactivity_log + INACTIVITY_REPEAT

    # --- spawning --------------------------------------------------------------

    def _read_credentials(self) -> dict[str, str]:
        path = self.state_dir / "credentials.env"
        if not path.exists():
            return {}
        return parse_credentials(path.read_text())

    def resolve_credential(self, name: str) -> tuple[str, str] | None:
        """The credentials file, re-read now, is the source of truth; the
        launcher's startup environment is the fallback for a name the file
        does not set. So editing the file and pressing Restart takes effect
        even if the same name was exported when the launcher booted. An empty
        value counts as unset at either level. Returns (source, value)."""
        file_value = self._read_credentials().get(name)
        if file_value:
            return ("file", file_value)
        env_value = self.base_env.get(name)
        if env_value:
            return ("environment", env_value)
        return None

    def _service_paths(self, kind: ServiceKind) -> tuple[Path, list[str]]:
        directory = self.source_dir / kind.directory
        argv = [str(directory / kind.argv[0]), *[str(directory / a) if a.endswith(".py") else a for a in kind.argv[1:]]]
        return directory, argv

    def _preflight(self, rec: Proc) -> str | None:
        """None when the service can be spawned, else the blocking state."""
        assert rec.kind is not None
        _directory, argv = self._service_paths(rec.kind)
        if not Path(argv[0]).exists() or not Path(argv[-1]).exists():
            rec.message = f"missing {argv[0] if not Path(argv[0]).exists() else argv[-1]}"
            return "not installed"
        if rec.kind.dynamic and rec.token_env and self.resolve_credential(rec.token_env) is None:
            rec.message = (f"{rec.token_env} is set neither in {self.state_dir / 'credentials.env'} "
                           "nor in the launcher's environment")
            rec.credential_source = None
            return "credential missing"
        return None

    def _spawn(self, rec: Proc, now: float) -> None:
        pass_fds: tuple[int, ...] = ()
        child_sock: socket.socket | None = None
        if rec.kind is None:
            # The core gets its end of a fresh socketpair as an inherited fd;
            # our end replaces whatever channel the previous core had.
            self._close_core_channel()
            parent_sock, child_sock = socket.socketpair()
            os.set_inheritable(child_sock.fileno(), True)
            argv = [*self.core_argv, "--control-fd", str(child_sock.fileno())]
            cwd = self.source_dir
            env = dict(self.base_env)
            # The port the launcher probes, stated explicitly: the core loads
            # .env (override=False) before reading RAINBOX_CORE_PORT, so a value
            # set only in .env would otherwise send it elsewhere.
            env[CORE_PORT_ENV] = str(self.core_addr[1])
            pass_fds = (child_sock.fileno(),)
        else:
            parent_sock = None
            cwd, argv = self._service_paths(rec.kind)
            credential = None
            declared = dict(rec.env)
            if rec.kind.dynamic:
                assert rec.token_env and rec.state_file_name and rec.kind.state_file_env
                found = self.resolve_credential(rec.token_env)
                if found is None:  # vanished since preflight; the next wake re-checks
                    rec.state, rec.credential_source = "credential missing", None
                    self._status_dirty = True
                    return
                rec.credential_source, value = found
                credential = (rec.token_env, value)
                declared[rec.kind.state_file_env] = str(self.state_dir / rec.state_file_name)
            env = service_environment(self.base_env, declared, credential)
        try:
            proc = subprocess.Popen(
                argv, cwd=str(cwd), env=env, start_new_session=True, shell=False,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                close_fds=True, pass_fds=pass_fds,
            )
        except FileNotFoundError as exc:
            rec.state, rec.message = "not installed", f"missing {exc.filename}"
            self._status_dirty = True
            if parent_sock is not None:
                parent_sock.close()
            if child_sock is not None:
                child_sock.close()
            return
        except OSError as exc:
            rec.state, rec.message = "failed", f"spawn error: {exc}"
            self._status_dirty = True
            if parent_sock is not None:
                parent_sock.close()
            if child_sock is not None:
                child_sock.close()
            return
        if child_sock is not None:
            child_sock.close()  # the child holds its own copy now
        if parent_sock is not None:
            self.core_channel = self.channel_cls(parent_sock)
            self._core_buf = b""
            self._core_snapshot_seen = False
        rec.proc, rec.pid, rec.pgid = proc, proc.pid, proc.pid
        assert proc.stdout is not None
        self._close_output(rec)
        # Own the pipe independently of the Popen object: its stdout file
        # object closes the fd when it is garbage-collected (we drop the
        # Popen on reap), which would turn the crash traceback still sitting
        # in the pipe into EBADF — and hand the fd number to the next child.
        rec.out_fd = os.dup(proc.stdout.fileno())
        proc.stdout.close()
        os.set_blocking(rec.out_fd, False)
        rec.out_buf = b""
        rec.state, rec.since, rec.message = "running", _utc_now(), None
        rec.running_since = now
        rec.stop_requested, rec.pending_restart = False, False
        rec.stop_deadline, rec.next_retry = None, None
        self._status_dirty = True
        logger.info("spawned %s pid=%d", rec.key, proc.pid)

    # --- the control channel -------------------------------------------------------

    def attach_core_socket(self, sock: socket.socket) -> None:
        """Tests: adopt a channel to a fake core instead of spawning one."""
        self._close_core_channel()
        self.core_channel = self.channel_cls(sock)
        self._core_buf = b""
        self._core_snapshot_seen = False

    def _close_core_channel(self) -> None:
        channel, self.core_channel = self.core_channel, None
        if channel is not None:
            channel.close()
        self._core_buf = b""
        self._core_snapshot_seen = False

    def _read_core(self, now: float) -> None:
        """Drain whatever the core has sent (non-blocking); EOF closes the
        channel — the core is gone or going, SIGCHLD reaps it."""
        channel = self.core_channel
        if channel is None:
            return
        while True:
            try:
                chunk = channel.recv(65536)
            except BlockingIOError:
                break
            except OSError:
                chunk = b""
            if not chunk:
                logger.info("core control channel closed")
                self._close_core_channel()
                self._note_activity(now)
                return
            self._note_activity(now)
            self._core_buf += chunk
            if len(self._core_buf) > MAX_LINE_BYTES:
                logger.warning("core sent an oversized control line; closing the channel")
                self._close_core_channel()
                return
            while b"\n" in self._core_buf:
                raw, self._core_buf = self._core_buf.split(b"\n", 1)
                self._handle_core_line(raw, now)

    def _handle_core_line(self, raw: bytes, now: float) -> None:
        try:
            message = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.warning("unparseable control line from the core")
            return
        if not isinstance(message, dict) or message.get("type") != "desired":
            logger.warning("unknown control message from the core: %r", message.get("type") if isinstance(message, dict) else message)
            return
        try:
            snap = validate_desired(message, self.catalogue)
        except ValueError as exc:
            logger.warning("desired snapshot rejected: %s", exc)
            return
        first_from_this_core = not self._core_snapshot_seen
        self._core_snapshot_seen = True
        self._apply_snapshot(snap, now)
        if first_from_this_core:
            # A freshly started core has no status table yet: send the whole
            # picture as soon as it is listening.
            self._status_dirty = True

    def _send_status(self) -> None:
        """Queue the full table for the channel. Event-driven: only after a
        change, never on a timer. The send is non-blocking: if the core is
        slow to read, the newest table waits (an older unsent one is replaced
        — only the latest picture matters) and the socket joins select()'s
        write set. Backpressure never closes the channel; only a peer that is
        gone does, and the next core learns the table after its first
        snapshot."""
        self._status_dirty = False
        if self.core_channel is None or not self._core_snapshot_seen:
            return
        self.sequence += 1
        line = (json.dumps(self.status_payload()) + "\n").encode()
        if self._send_buf and self._send_buf_started:
            self._queued_status = line   # a line already on the wire must finish; the newer table waits
        else:
            self._send_buf = line        # nothing of the old line went out: the newer table replaces it
            self._queued_status = b""
        self._flush_status()

    @property
    def wants_write(self) -> bool:
        return self.core_channel is not None and bool(self._send_buf or self._queued_status)

    def _flush_status(self) -> None:
        """Write as much pending status as the socket takes right now."""
        channel = self.core_channel
        while channel is not None and (self._send_buf or self._queued_status):
            if not self._send_buf:
                self._send_buf, self._queued_status = self._queued_status, b""
                self._send_buf_started = False
            try:
                n = channel.send(self._send_buf)
            except BlockingIOError:
                return  # the core will read later; select() wakes us when writable
            except OSError as exc:
                # EPIPE/ECONNRESET: the peer is gone. Expected while we are
                # stopping the core ourselves; a warning otherwise.
                (logger.debug if self.shutting_down else logger.warning)(
                    "core channel gone on send (%s)", exc)
                self._send_buf, self._queued_status = b"", b""
                self._send_buf_started = False
                self._close_core_channel()
                return
            self._send_buf = self._send_buf[n:]
            self._send_buf_started = bool(self._send_buf)

    # --- child output --------------------------------------------------------------

    def _close_output(self, rec: Proc) -> None:
        if rec.out_fd is not None:
            if rec.out_buf:
                self.on_output(rec.label or rec.key, rec.out_buf.decode("utf-8", "replace"))
            try:
                os.close(rec.out_fd)
            except OSError:
                pass
            rec.out_fd, rec.out_buf = None, b""

    def _drain_outputs(self, now: float) -> None:
        """Read whatever the children have written (non-blocking) and hand
        each line to on_output with its service key, so interleaved logs stay
        attributable. A pipe stays open until EOF — a grandchild that inherited
        it keeps it open, and its lines carry the parent's key."""
        for rec in (self.core, *self.services.values()):
            fd = rec.out_fd
            if fd is None:
                continue
            while True:
                try:
                    chunk = os.read(fd, 65536)
                except BlockingIOError:
                    break
                except OSError:
                    chunk = b""
                if not chunk:
                    self._close_output(rec)
                    break
                self._note_activity(now)
                rec.out_buf += chunk
                while b"\n" in rec.out_buf:
                    line, rec.out_buf = rec.out_buf.split(b"\n", 1)
                    self.on_output(rec.label or rec.key, line.decode("utf-8", "replace"))
                if len(rec.out_buf) >= MAX_OUTPUT_LINE:
                    self.on_output(rec.label or rec.key, rec.out_buf.decode("utf-8", "replace"))
                    rec.out_buf = b""

    def output_fds(self) -> list[int]:
        return [rec.out_fd for rec in (self.core, *self.services.values()) if rec.out_fd is not None]

    def status_payload(self) -> dict[str, Any]:
        services = {key: rec.status() for key, rec in self.services.items()}
        services[CORE_KEY] = self.core.status()
        return {
            "type": "status",
            "schema_version": SCHEMA_VERSION,
            "sequence": self.sequence,
            "launcher": {"pid": os.getpid(), "state_dir": str(self.state_dir),
                         "started_at": self.started_at, "core_only": self.core_only},
            "services": services,
        }

    # --- stopping ----------------------------------------------------------------

    def _group_alive(self, pgid: int | None) -> bool:
        """Whether a group this launcher created still has members. EPERM
        means the id has been reused by a process we may not signal — it is
        not ours any more, so it counts as gone; we never adopt ids."""
        if pgid is None:
            return False
        try:
            os.killpg(pgid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    def _signal_group(self, rec: Proc, sig: int) -> None:
        if rec.pgid is None:
            return
        try:
            os.killpg(rec.pgid, sig)
        except (ProcessLookupError, PermissionError):
            pass  # gone, or the id was reused by a process that is not ours

    def _request_stop(self, rec: Proc, now: float, *, reason: str) -> None:
        if rec.proc is None or rec.stop_requested:
            return
        rec.stop_requested = True
        rec.stop_deadline = now + TERM_GRACE
        rec.state = "stopping"
        rec.message = reason
        self._status_dirty = True
        if rec.kind is None:
            # The core first gets a plain SIGTERM to its pid so its supervisor
            # can handle the agents; the group is killed only on escalation.
            try:
                os.kill(rec.pid or 0, signal.SIGTERM)
            except ProcessLookupError:
                pass
        else:
            self._signal_group(rec, signal.SIGTERM)
        logger.info("stopping %s pid=%s (%s)", rec.key, rec.pid, reason)

    # --- reaping ------------------------------------------------------------------

    def _reap(self, now: float) -> None:
        for rec in (self.core, *self.services.values()):
            if rec.proc is None:
                continue
            rc = rec.proc.poll()
            if rc is None:
                continue
            self._note_activity(now)
            self._handle_exit(rec, rc, now)

    def _handle_exit(self, rec: Proc, rc: int, now: float) -> None:
        rec.proc = None
        rec.last_exit = rc
        rec.running_since = None
        was_intentional = rec.stop_requested
        rec.stop_requested, rec.stop_deadline = False, None
        if rec.kind is None:
            self._close_core_channel()
        # The leader is gone; give any survivors in its group the same grace.
        if self._group_alive(rec.pgid):
            self._signal_group(rec, signal.SIGTERM)
            rec.group_drain_deadline = now + TERM_GRACE
        else:
            rec.pgid = None
        self._status_dirty = True
        if was_intentional:
            rec.state, rec.message = "stopped", None
            logger.info("%s stopped (exit %s)", rec.key, rc)
            return
        if rc in (EXIT_CONFIG_REJECTED, EXIT_LOCK_HELD):
            rec.state = "failed"
            rec.message = ("configuration or credential rejected (exit 2); see its log"
                           if rc == EXIT_CONFIG_REJECTED else
                           "port or lock held by another process (exit 3); see its log")
            rec.pending_restart = False
            logger.error("%s failed deterministically: %s", rec.key, rec.message)
            return
        rec.crash_times = [t for t in rec.crash_times if now - t <= CRASH_WINDOW] + [now]
        if len(rec.crash_times) >= CRASH_BUDGET:
            rec.state = "failed"
            rec.message = f"{CRASH_BUDGET} unexpected exits within {CRASH_WINDOW:.0f}s; last exit {rc}"
            rec.pending_restart = False
            logger.error("%s: %s", rec.key, rec.message)
            if rec.kind is None:
                logger.critical(
                    "CORE FAILED and will not be restarted; running services are kept "
                    "as they are. Fix the cause and restart the launcher.")
            return
        rec.backoff_exp += 1
        delay = min(BACKOFF_CAP, BACKOFF_BASE ** rec.backoff_exp)
        rec.state, rec.next_retry = "backoff", now + delay
        rec.message = f"unexpected exit {rc}; retry in {delay:.0f}s"
        logger.warning("%s exited unexpectedly (%s); retry in %.0fs", rec.key, rc, delay)

    # --- reconcile ---------------------------------------------------------------

    def _apply_snapshot(self, snap: Snapshot, now: float) -> None:
        if self.shutting_down:
            return  # request_shutdown() already decided every desired flag
        first = not self.snapshot_valid_once
        self.snapshot = snap
        self.snapshot_valid_once = True
        # Core nonce: the first valid snapshot after startup is the baseline.
        if first or not self.core.nonce_known:
            self.core.nonce, self.core.nonce_known = snap.core_restart_nonce, True
        elif snap.core_restart_nonce != self.core.nonce:
            self.core.nonce = snap.core_restart_nonce
            self.core.pending_restart = True
            self.core.crash_times, self.core.backoff_exp = [], 0
            if self.core.state == "failed":
                self.core.state = "stopped"
        # Dynamic keys: create a record for a new one; a known one missing
        # from a valid snapshot is a removal (stop it, then forget it).
        for key, entry in snap.services.items():
            if key not in self.services:
                self.services[key] = Proc(key, self.catalogue[entry.kind])
                self._status_dirty = True
        for key, rec in self.services.items():
            if rec.kind is not None and rec.kind.dynamic and key not in snap.services and not rec.removed:
                rec.removed, rec.desired = True, False
                self._status_dirty = True
        for key, entry in snap.services.items():
            rec = self.services[key]
            rec.removed = False
            rec.desired = entry.enabled and not self.core_only
            rec.env = dict(entry.env)
            rec.label, rec.token_env, rec.state_file_name = entry.label, entry.token_env, entry.state_file_name
            if not rec.nonce_known:
                rec.nonce, rec.nonce_known = entry.restart_nonce, True
            elif entry.restart_nonce != rec.nonce:
                rec.nonce = entry.restart_nonce
                rec.pending_restart = rec.desired
                rec.crash_times, rec.backoff_exp, rec.next_retry = [], 0, None
                if rec.state in ("failed", "backoff", "credential missing", "not installed"):
                    rec.state = "stopped"
                    rec.message = None
            if self.core_only and entry.enabled:
                rec.message = "suppressed by --core-only"

    def _reconcile_one(self, rec: Proc, now: float) -> None:
        # Group survivors after the leader died: escalate at the deadline.
        if rec.group_drain_deadline is not None:
            if not self._group_alive(rec.pgid):
                rec.group_drain_deadline, rec.pgid = None, None
            elif now >= rec.group_drain_deadline:
                self._signal_group(rec, signal.SIGKILL)
                rec.group_drain_deadline, rec.pgid = None, None
            else:
                return  # never spawn a replacement while its old group lingers
        if rec.proc is not None:
            if rec.stop_requested:
                if rec.stop_deadline is not None and now >= rec.stop_deadline:
                    self._signal_group(rec, signal.SIGKILL)
                    rec.stop_deadline = None
                return
            if not rec.desired:
                self._request_stop(rec, now, reason="disabled")
            elif rec.pending_restart:
                block = self._preflight(rec) if rec.kind is not None else None
                if block:
                    # Keep the running child; report the blocked restart and
                    # consume the nonce as attempted.
                    rec.pending_restart = False
                    rec.message = f"restart blocked: {rec.message}"
                    self._status_dirty = True
                else:
                    self._request_stop(rec, now, reason="restart")
            elif rec.running_since is not None and now - rec.running_since >= BACKOFF_RESET_AFTER:
                if rec.backoff_exp or rec.crash_times:
                    rec.backoff_exp, rec.crash_times = 0, []
            return
        # Not running.
        if not rec.desired:
            if rec.state in ("backoff", "not installed", "credential missing"):
                rec.state, rec.next_retry, rec.message = "stopped", None, None
                self._status_dirty = True
            return
        if rec.state == "failed":
            return
        if rec.state == "backoff" and rec.next_retry is not None and now < rec.next_retry:
            return
        if rec.kind is not None:
            block = self._preflight(rec)
            if block:
                if rec.state != block:
                    rec.state = block
                    self._status_dirty = True
                return
        self._spawn(rec, now)

    def _reconcile(self, now: float) -> None:
        if self.spawn_core:
            self._reconcile_one(self.core, now)
        if self.snapshot is None:
            return  # no side services before the first valid snapshot
        for rec in list(self.services.values()):
            self._reconcile_one(rec, now)
            if (rec.removed and rec.proc is None and rec.out_fd is None
                    and not self._group_alive(rec.pgid) and rec.group_drain_deadline is None):
                del self.services[rec.key]
                self._status_dirty = True
                logger.info("%s removed", rec.label or rec.key)

    # --- shutdown ----------------------------------------------------------------

    def request_shutdown(self) -> None:
        self._signals += 1
        self._note_activity(self.clock())
        if self._signals >= 2:
            logger.warning("second signal: killing everything now")
            # Every group this launcher still owns — including one whose
            # leader already exited but whose descendants linger.
            for rec in (*self.services.values(), self.core):
                if rec.pgid is not None:
                    self._signal_group(rec, signal.SIGKILL)
            self.shutting_down = True
            self.shutdown_phase = 3
            return
        if self.shutting_down:
            return
        self.shutting_down = True
        self.shutdown_phase = 1
        self.phase_deadline = None
        for rec in self.services.values():
            rec.desired = False
        logger.info("shutdown: phase 1, stopping services (core stays up)")

    def _shutdown_tick(self, now: float) -> bool:
        """Advance the two-phase shutdown; True when everything is gone."""
        live_services = [r for r in self.services.values() if r.proc is not None or self._group_alive(r.pgid)]
        if self.shutdown_phase == 1:
            for rec in self.services.values():
                if rec.proc is not None and not rec.stop_requested:
                    self._request_stop(rec, now, reason="shutdown")
            if self.phase_deadline is None:
                self.phase_deadline = now + TERM_GRACE
            if live_services and now >= self.phase_deadline:
                for rec in live_services:
                    self._signal_group(rec, signal.SIGKILL)
            if live_services:
                return False
            self.shutdown_phase = 2
            self.phase_deadline = None
            logger.info("shutdown: phase 2, stopping the core")
        if self.shutdown_phase == 2:
            if self.core.proc is None and not self._group_alive(self.core.pgid):
                return True
            if self.core.proc is not None and not self.core.stop_requested:
                self._request_stop(self.core, now, reason="shutdown")
            if self.phase_deadline is None:
                self.phase_deadline = now + TERM_GRACE
            if now >= self.phase_deadline:
                self._signal_group(self.core, signal.SIGKILL)
            return False
        if self.shutdown_phase == 3:
            for rec in (*self.services.values(), self.core):
                if rec.pgid is not None and self._group_alive(rec.pgid):
                    self._signal_group(rec, signal.SIGKILL)
            return all(r.proc is None and not self._group_alive(r.pgid)
                       for r in (*self.services.values(), self.core))
        return False

    # --- the loop ------------------------------------------------------------------

    def next_deadline(self, now: float) -> float:
        """The earliest moment the loop has something to do: a pending stop
        escalation, backoff retry, group drain, shutdown phase deadline, or
        the next inactivity log line. Everything else arrives as an event on
        the core socket or the signal pipe."""
        candidates = [self._next_inactivity_log]
        if self.phase_deadline is not None:
            candidates.append(self.phase_deadline)
        for rec in (self.core, *self.services.values()):
            for t in (rec.stop_deadline, rec.next_retry, rec.group_drain_deadline):
                if t is not None:
                    candidates.append(t)
        return max(now, min(candidates))

    def tick(self, now: float | None = None) -> bool:
        """One pass. Returns False once shutdown has completed."""
        now = self.clock() if now is None else now
        self._reap(now)
        self._drain_outputs(now)
        self._read_core(now)
        self._flush_status()
        if self.shutting_down:
            self.core.desired = self.core.desired and self.shutdown_phase < 2
            for rec in (*self.services.values(), self.core):
                if rec.group_drain_deadline is not None and (
                        not self._group_alive(rec.pgid) or now >= rec.group_drain_deadline):
                    if self._group_alive(rec.pgid):
                        self._signal_group(rec, signal.SIGKILL)
                    rec.group_drain_deadline, rec.pgid = None, None
            done = self._shutdown_tick(now)
            if done:
                self._send_status()
                return False
            if self._status_dirty:
                self._send_status()
            return True
        self._reconcile(now)
        if self._status_dirty:
            self._send_status()
        self._inactivity_tick(now)
        return True

    def run(self) -> int:
        rfd, wfd = os.pipe()
        os.set_blocking(wfd, False)
        signal.set_wakeup_fd(wfd)

        def _on_signal(signum: int, _frame: Any) -> None:
            logger.info("signal %d", signum)
            self.request_shutdown()

        signal.signal(signal.SIGINT, _on_signal)
        signal.signal(signal.SIGTERM, _on_signal)
        # SIGCHLD needs no handler body: its arrival writes a byte to the
        # wakeup fd, which is what ends the select() below so the child is
        # reaped at once instead of at a timer.
        signal.signal(signal.SIGCHLD, lambda *_: None)
        try:
            while True:
                if not self.tick():
                    break
                rlist: list[Any] = [rfd, *self.output_fds()]
                if self.core_channel is not None:
                    rlist.append(self.core_channel)
                wlist = [self.core_channel] if self.wants_write else []
                timeout = min(MAX_SLEEP, self.next_deadline(self.clock()) - self.clock())
                r, _, _ = select.select(rlist, wlist, [], max(0.0, timeout))
                if rfd in r:
                    os.read(rfd, 4096)
                    self._note_activity(self.clock())
        finally:
            signal.set_wakeup_fd(-1)
            os.close(rfd)
            os.close(wfd)
            self._close_core_channel()
            for rec in (self.core, *self.services.values()):
                self._close_output(rec)
        logger.info("bye")
        return self.exit_code


# --- entrypoint ------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="rainbox launcher: starts the core and the enabled side services")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR,
                        help=f"runtime state (lock, credentials.env); default {DEFAULT_STATE_DIR}")
    parser.add_argument("--core-only", action="store_true",
                        help="run the core and nothing else, regardless of toggles")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s launcher: %(message)s")
    launcher = Launcher(state_dir=args.state_dir, core_only=args.core_only,
                        core_addr=core_addr_from_env())
    launcher.acquire_lock()
    logger.info("launcher pid %d; state dir %s; core-only=%s", os.getpid(), launcher.state_dir, launcher.core_only)
    return launcher.run()


if __name__ == "__main__":
    sys.exit(main())
