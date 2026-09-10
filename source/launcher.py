"""Launcher: one small process that starts the rest.

    cd source && venv/bin/python launcher.py [--state-dir DIR] [--core-only]

The launcher is the root of rainbox's process tree. It starts the core
(`main.py`) and every side service the operator has enabled on /settings, as
*siblings*, keeps them running, stops what is disabled, and reports what it
sees. It never becomes the core: it imports only the standard library plus the
data-only catalogue in `services/definitions.py`, so its footprint stays small
and a restart of the core never takes a service down.

Design: docs/superpowers/specs/2026-09-10-launcher-design.md. In short:

- Children are spawned with fork+exec (`subprocess.Popen`, own session), never
  fork alone; each child's parent pid is this process, which is what makes
  Activity Monitor's hierarchy legible.
- The core is told it is managed through two environment markers; it echoes
  them from `GET /services/api/desired`, and this launcher accepts a snapshot
  only from the core it spawned.
- Restarts are nonces: the core rewrites a service's nonce, the launcher sees
  it change and restarts the process. The core never executes anything.
- A single-threaded loop over monotonic deadlines: reap, escalate, retry,
  poll, heartbeat, and a two-phase shutdown (services first, core last).
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import http.client
import json
import logging
import os
import select
import signal
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from services.definitions import (
    BASELINE_ENV_KEYS,
    BASELINE_ENV_PREFIXES,
    CORE_KEY,
    EXIT_CONFIG_REJECTED,
    EXIT_LOCK_HELD,
    SCHEMA_VERSION,
    STATIC_SERVICES,
    ServiceKind,
)

logger = logging.getLogger("launcher")

SOURCE_DIR: Path = Path(__file__).resolve().parent
REPO_ROOT: Path = SOURCE_DIR.parent
DEFAULT_STATE_DIR: Path = REPO_ROOT / "var" / "services"
CORE_PORT_ENV = "RAINBOX_CORE_PORT"  # read by main.py too; default 5000


def core_addr_from_env(env: dict[str, str] | None = None) -> tuple[str, int]:
    raw = (os.environ if env is None else env).get(CORE_PORT_ENV, "5000")
    try:
        port = int(raw)
    except ValueError:
        raise SystemExit(f"{CORE_PORT_ENV} must be an integer port (got {raw!r})") from None
    return ("127.0.0.1", port)


CORE_ADDR: tuple[str, int] = ("127.0.0.1", 5000)

LAUNCHER_ID_ENV = "RAINBOX_LAUNCHER_ID"
CORE_INSTANCE_ID_ENV = "RAINBOX_CORE_INSTANCE_ID"

POLL_INTERVAL: float = 5.0        # desired-state poll
HEARTBEAT_INTERVAL: float = 30.0  # full status re-post
TERM_GRACE: float = 10.0          # SIGTERM -> SIGKILL, same as main.py's agents
HTTP_DEADLINE: float = 1.0        # elapsed-time bound per control request
HTTP_MAX_BYTES: int = 1 << 20     # 1 MiB response cap
BACKOFF_BASE: float = 2.0
BACKOFF_CAP: float = 60.0
CRASH_BUDGET: int = 5             # unexpected exits ...
CRASH_WINDOW: float = 120.0       # ... within this many seconds latch `failed`
BACKOFF_RESET_AFTER: float = 120.0
STATUS_RETRY_AFTER: float = 2.0   # a failed status post is retried this soon

# Variables that must never leak from the launcher into a service, whatever
# the operator exported (loader injection, interpreter overrides, DB access).
FORBIDDEN_ENV_KEYS: frozenset[str] = frozenset({
    "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "DATABASE_URL",
    "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH", "LD_PRELOAD", "LD_LIBRARY_PATH",
})

_ENV_NAME_OK = __import__("re").compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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


# --- HTTP (stdlib, elapsed-time bounded) ------------------------------------


class ControlError(Exception):
    """A control request to the core failed (transport, deadline, size)."""


def http_json(
    addr: tuple[str, int], method: str, path: str, body: Any = None, *,
    deadline_s: float = HTTP_DEADLINE, max_bytes: int = HTTP_MAX_BYTES,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[int, Any]:
    """One request to the fixed loopback control endpoint. No proxies, no
    redirects (http.client follows none). The socket timeout bounds each
    blocking read and the elapsed-time check between chunks bounds the whole
    request, so a trickling response cannot hold supervision hostage; the
    body is capped at `max_bytes`."""
    started = clock()
    payload = None if body is None else json.dumps(body).encode()
    headers = {"Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    conn = http.client.HTTPConnection(addr[0], addr[1], timeout=deadline_s)
    try:
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        chunks: list[bytes] = []
        size = 0
        while True:
            if clock() - started > deadline_s:
                raise ControlError(f"{method} {path}: exceeded {deadline_s:.1f}s")
            # read1: at most ONE underlying socket read, so the elapsed-time
            # check above runs between every arrival; plain read(n) would
            # block until n bytes or EOF and a trickle would never trip it.
            chunk = resp.read1(65536)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise ControlError(f"{method} {path}: response over {max_bytes} bytes")
            chunks.append(chunk)
        raw = b"".join(chunks)
    except (OSError, http.client.HTTPException) as exc:
        raise ControlError(f"{method} {path}: {type(exc).__name__}: {exc}") from None
    finally:
        conn.close()
    try:
        data = json.loads(raw.decode("utf-8")) if raw else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ControlError(f"{method} {path}: response is not JSON") from None
    return resp.status, data


# --- desired-state validation -------------------------------------------------


@dataclass(frozen=True)
class DesiredEntry:
    key: str
    kind: str
    enabled: bool
    restart_nonce: str | None
    env: dict[str, str]


@dataclass(frozen=True)
class Snapshot:
    core_pid: int
    core_restart_nonce: str | None
    services: dict[str, DesiredEntry]


def validate_desired(
    payload: Any, launcher_id: str, core_instance_id: str,
    catalogue: dict[str, ServiceKind],
) -> Snapshot:
    """The whole response is validated before any of it is acted on; anything
    off leaves the last valid snapshot in force."""
    if not isinstance(payload, dict):
        raise ValueError("not an object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported schema_version")
    if payload.get("launcher_id") != launcher_id or payload.get("core_instance_id") != core_instance_id:
        raise ValueError("instance markers do not match this launcher's core")
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
        services[key] = DesiredEntry(key, kind, enabled, nonce, dict(env))
    missing = set(catalogue) - set(services)
    if missing:
        raise ValueError(f"snapshot lacks static services: {sorted(missing)}")
    return Snapshot(core_pid=core_pid, core_restart_nonce=core_nonce, services=services)


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

    def status(self) -> dict[str, Any]:
        rec: dict[str, Any] = {
            "state": self.state, "pid": self.pid if self.state in ("running", "stopping") else None,
            "since": self.since, "last_exit": self.last_exit, "message": self.message,
        }
        if self.state == "backoff" and self.next_retry is not None:
            rec["next_retry"] = self.next_retry
        if self.credential_source:
            rec["credential_source"] = self.credential_source
        return rec


class Launcher:
    def __init__(
        self, *, state_dir: Path, core_only: bool = False,
        catalogue: dict[str, ServiceKind] | None = None,
        source_dir: Path = SOURCE_DIR, core_addr: tuple[str, int] = CORE_ADDR,
        core_argv: list[str] | None = None, spawn_core: bool = True,
        base_env: dict[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.state_dir = Path(state_dir).resolve()
        self.core_only = core_only
        self.catalogue = dict(STATIC_SERVICES if catalogue is None else catalogue)
        self.source_dir = Path(source_dir).resolve()
        self.core_addr = core_addr
        self.core_argv = core_argv or [sys.executable, str(self.source_dir / "main.py")]
        self.spawn_core = spawn_core
        self.base_env = dict(os.environ if base_env is None else base_env)
        self.clock = clock
        self.launcher_id = uuid.uuid4().hex
        self.core_instance_id = uuid.uuid4().hex
        self.started_at = _utc_now()
        self.sequence = 0
        self.snapshot: Snapshot | None = None
        self.snapshot_valid_once = False
        self.core = Proc(CORE_KEY, None, desired=True)
        self.services: dict[str, Proc] = {
            key: Proc(key, kind) for key, kind in self.catalogue.items()
        }
        self.shutting_down = False
        self.shutdown_phase = 0
        self.phase_deadline: float | None = None
        self.exit_code = 0
        self._next_poll = 0.0
        self._next_heartbeat = 0.0
        self._status_dirty = True
        self._status_retry_at = 0.0
        self._lock_fh = None
        self._signals = 0

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

    def core_port_in_use(self) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            return s.connect_ex(self.core_addr) == 0

    # --- spawning --------------------------------------------------------------

    def _read_credentials(self) -> dict[str, str]:
        path = self.state_dir / "credentials.env"
        if not path.exists():
            return {}
        return parse_credentials(path.read_text())

    def resolve_credential(self, name: str) -> tuple[str, str] | None:
        """Startup environment first, then the credentials file re-read now.
        An explicitly empty value is missing. Returns (source, value)."""
        env_value = self.base_env.get(name)
        if env_value is not None and env_value != "":
            return ("environment", env_value)
        file_value = self._read_credentials().get(name)
        if file_value:
            return ("file", file_value)
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
        return None

    def _spawn(self, rec: Proc, now: float) -> None:
        if rec.kind is None:
            argv = list(self.core_argv)
            cwd = self.source_dir
            env = dict(self.base_env)
            self.core_instance_id = uuid.uuid4().hex
            env[LAUNCHER_ID_ENV] = self.launcher_id
            env[CORE_INSTANCE_ID_ENV] = self.core_instance_id
        else:
            cwd, argv = self._service_paths(rec.kind)
            env = service_environment(self.base_env, rec.env)
        try:
            proc = subprocess.Popen(
                argv, cwd=str(cwd), env=env, start_new_session=True, shell=False,
                stdin=subprocess.DEVNULL, close_fds=True,
            )
        except FileNotFoundError as exc:
            rec.state, rec.message = "not installed", f"missing {exc.filename}"
            self._status_dirty = True
            return
        except OSError as exc:
            rec.state, rec.message = "failed", f"spawn error: {exc}"
            self._status_dirty = True
            return
        rec.proc, rec.pid, rec.pgid = proc, proc.pid, proc.pid
        rec.state, rec.since, rec.message = "running", _utc_now(), None
        rec.running_since = now
        rec.stop_requested, rec.pending_restart = False, False
        rec.stop_deadline, rec.next_retry = None, None
        self._status_dirty = True
        logger.info("spawned %s pid=%d", rec.key, proc.pid)

    # --- stopping ----------------------------------------------------------------

    def _group_alive(self, pgid: int | None) -> bool:
        if pgid is None:
            return False
        try:
            os.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def _signal_group(self, rec: Proc, sig: int) -> None:
        if rec.pgid is None:
            return
        try:
            os.killpg(rec.pgid, sig)
        except ProcessLookupError:
            pass

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
            self._handle_exit(rec, rc, now)

    def _handle_exit(self, rec: Proc, rc: int, now: float) -> None:
        rec.proc = None
        rec.last_exit = rc
        rec.running_since = None
        was_intentional = rec.stop_requested
        rec.stop_requested, rec.stop_deadline = False, None
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
                           "locked by another process (exit 3); see its log for the pid")
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
        for key, entry in snap.services.items():
            rec = self.services[key]
            rec.desired = entry.enabled and not self.core_only
            rec.env = dict(entry.env)
            if not rec.nonce_known:
                rec.nonce, rec.nonce_known = entry.restart_nonce, True
            elif entry.restart_nonce != rec.nonce:
                rec.nonce = entry.restart_nonce
                rec.pending_restart = rec.desired
                rec.crash_times, rec.backoff_exp, rec.next_retry = [], 0, None
                if rec.state in ("failed", "backoff"):
                    rec.state = "stopped"
                    rec.message = None
            if self.core_only and entry.enabled:
                rec.message = "suppressed by --core-only"

    def _poll_desired(self, now: float) -> None:
        self._next_poll = now + POLL_INTERVAL
        try:
            status, data = http_json(self.core_addr, "GET", "/services/api/desired", clock=self.clock)
        except ControlError as exc:
            logger.debug("desired: %s", exc)
            return
        if status != 200:
            logger.debug("desired: HTTP %s", status)
            return
        try:
            snap = validate_desired(data, self.launcher_id, self.core_instance_id, self.catalogue)
        except ValueError as exc:
            logger.warning("desired snapshot rejected: %s", exc)
            return
        self._apply_snapshot(snap, now)

    def _post_status(self, now: float) -> None:
        """Post the full table. A failed post (the core is still starting, or
        restarting) keeps the table dirty and retries after STATUS_RETRY_AFTER,
        so a fresh core learns the picture in seconds, not at the next
        heartbeat; a non-2xx answer (stale sequence, markers of an older core)
        is not retried since resending the same thing cannot help."""
        self._next_heartbeat = now + HEARTBEAT_INTERVAL
        self.sequence += 1
        try:
            status, _ = http_json(self.core_addr, "POST", "/services/api/status",
                                  self.status_payload(), clock=self.clock)
        except ControlError as exc:
            logger.debug("status: %s", exc)
            self._status_dirty = True
            self._status_retry_at = now + STATUS_RETRY_AFTER
            return
        self._status_dirty = False
        if status >= 300:
            logger.debug("status: HTTP %s", status)

    def status_payload(self) -> dict[str, Any]:
        services = {key: rec.status() for key, rec in self.services.items()}
        services[CORE_KEY] = self.core.status()
        return {
            "schema_version": SCHEMA_VERSION,
            "launcher_id": self.launcher_id,
            "core_instance_id": self.core_instance_id,
            "sequence": self.sequence,
            "launcher": {"pid": os.getpid(), "state_dir": str(self.state_dir),
                         "started_at": self.started_at, "core_only": self.core_only},
            "services": services,
        }

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
        for rec in self.services.values():
            self._reconcile_one(rec, now)

    # --- shutdown ----------------------------------------------------------------

    def request_shutdown(self) -> None:
        self._signals += 1
        if self._signals >= 2:
            logger.warning("second signal: killing everything now")
            for rec in (*self.services.values(), self.core):
                if rec.proc is not None:
                    self._signal_group(rec, signal.SIGKILL)
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
            return all(r.proc is None for r in (*self.services.values(), self.core))
        return False

    # --- the loop ------------------------------------------------------------------

    def tick(self, now: float | None = None) -> bool:
        """One pass. Returns False once shutdown has completed."""
        now = self.clock() if now is None else now
        self._reap(now)
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
                self._post_status(now)
                return False
            if self._status_dirty:
                self._post_status(now)
            return True
        self._reconcile(now)
        if now >= self._next_poll:
            self._poll_desired(now)
            self._reconcile(now)
        if (self._status_dirty and now >= self._status_retry_at) or now >= self._next_heartbeat:
            self._post_status(now)
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
        try:
            while True:
                if not self.tick():
                    break
                r, _, _ = select.select([rfd], [], [], 0.25)
                if r:
                    os.read(rfd, 4096)
        finally:
            signal.set_wakeup_fd(-1)
            os.close(rfd)
            os.close(wfd)
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
    if launcher.core_port_in_use():
        logger.error(
            "something already listens on %s:%d — an unmanaged core or another "
            "application. Stop it first; the launcher never adopts or signals a "
            "process it did not start.", *launcher.core_addr)
        return EXIT_CONFIG_REJECTED
    logger.info("launcher %s; state dir %s; core-only=%s", launcher.launcher_id, launcher.state_dir, launcher.core_only)
    return launcher.run()


if __name__ == "__main__":
    sys.exit(main())
