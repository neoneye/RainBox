"""Core-side half of service supervision: the control channel to the
launcher, the desired-state snapshot pushed down it, the restart nonces the
launcher consumes, and the status it reports back. Imports `db`; the launcher
never imports this module.

The channel is one end of a `socketpair()` the launcher created before
spawning the core, handed over as an inherited fd (`core.py --control-fd N`),
exactly the way the core hands agents theirs. Both directions are JSON lines
and both are event-driven — nothing on either side polls:

- core -> launcher: `{"type": "desired", ...snapshot}` once at startup and
  again whenever a service setting or restart nonce changes;
- launcher -> core: `{"type": "status", ...table}` whenever a process changes.

EOF is liveness: when the launcher dies the reader sees EOF and the core is
unmanaged from that moment; when the core dies the launcher sees EOF (and
SIGCHLD). A core started without `--control-fd` (by hand, `tools.serve_ui`)
is unmanaged, and /settings says so instead of showing toggles that would do
nothing.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
from datetime import UTC, datetime
from typing import Any

import db
from services.definitions import (
    CORE_KEY,
    SCHEMA_VERSION,
    STATIC_SERVICES,
    enabled_setting_key,
    env_setting_key,
    nonce_setting_key,
)

logger = logging.getLogger(__name__)

STATES = frozenset({
    "starting", "running", "stopping", "stopped", "backoff", "failed",
    "credential missing", "not installed",
})


def new_nonce() -> str:
    return os.urandom(16).hex()


def _known_key(service_key: str) -> bool:
    return service_key == CORE_KEY or service_key in STATIC_SERVICES


def bump_restart_nonce(service_key: str) -> str:
    """Rewrite one service's (or the core's) restart nonce and push the new
    snapshot to the launcher, which restarts the process. Nothing executes
    here."""
    if not _known_key(service_key):
        raise KeyError(service_key)
    nonce = new_nonce()
    db.set_settings({nonce_setting_key(service_key): nonce})
    CHANNEL.push_desired()
    return nonce


def set_service_setting(key: str, value: object) -> bool:
    """Write a `services.<key>.enabled` or `services.<key>.env.<VAR>` setting
    and, if the effective value changed, rewrite that service's nonce in the
    same transaction, then push the snapshot. Returns whether the nonce was
    bumped. KeyError for a key this function does not own."""
    service_key = _service_key_of(key)
    if service_key is None:
        raise KeyError(key)
    # Row lock BEFORE the read: two concurrent writers of the same key must
    # serialize through read/compare/write, or one could read a stale value,
    # write its own, and wrongly call that "unchanged".
    db.lock_setting_row(key)
    before = db.get_setting(key)
    db.stage_setting(key, value)
    db.session.flush()
    after = db.get_setting(key)
    changed = after != before
    if changed:
        db.stage_setting(nonce_setting_key(service_key), new_nonce())
    db.session.commit()
    if changed:
        CHANNEL.push_desired()
    return changed


def _service_key_of(setting_key: str) -> str | None:
    for svc in STATIC_SERVICES.values():
        if setting_key == enabled_setting_key(svc.key):
            return svc.key
        for var in svc.env_keys:
            if setting_key == env_setting_key(svc.key, var):
                return svc.key
    return None


def owns_setting(setting_key: str) -> bool:
    return _service_key_of(setting_key) is not None


def desired_snapshot() -> dict[str, Any]:
    """What the launcher should be running, as one coherent snapshot from one
    statement over the settings table. Includes disabled entries, never a
    credential value, never a path or argv — the launcher resolves kinds
    against its own copy of the catalogue. App context required."""
    values = db.get_settings_snapshot("services.")
    services = []
    for svc in STATIC_SERVICES.values():
        env: dict[str, str] = {}
        for var in svc.env_keys:
            val = values.get(env_setting_key(svc.key, var))
            if val not in (None, ""):
                env[var] = str(val)
        services.append({
            "key": svc.key,
            "kind": svc.kind,
            "enabled": bool(values.get(enabled_setting_key(svc.key))),
            "restart_nonce": values.get(nonce_setting_key(svc.key)),
            "env": env,
        })
    return {
        "type": "desired",
        "schema_version": SCHEMA_VERSION,
        "core_pid": os.getpid(),
        "core_restart_nonce": values.get(nonce_setting_key(CORE_KEY)),
        "services": services,
    }


class LauncherStatus:
    """The last status table the launcher sent, in memory only."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._payload: dict[str, Any] | None = None
        self._received_at: str | None = None
        self._last_sequence: int = -1

    def accept(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise ValueError("status must be a JSON object")
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported schema_version")
        seq = payload.get("sequence")
        if not isinstance(seq, int) or isinstance(seq, bool):
            raise ValueError("sequence must be an integer")
        services = payload.get("services")
        if not isinstance(services, dict):
            raise ValueError("services must be an object")
        for key, rec in services.items():
            if not isinstance(rec, dict) or rec.get("state") not in STATES:
                raise ValueError(f"bad state for {key}")
        with self._lock:
            if seq <= self._last_sequence:
                raise ValueError("stale sequence")
            self._last_sequence = seq
            self._payload = payload
            self._received_at = datetime.now(UTC).isoformat()

    def reset(self) -> None:
        with self._lock:
            self._payload = None
            self._received_at = None
            self._last_sequence = -1

    def view(self, managed: bool) -> dict[str, Any]:
        """What /settings renders: managed?, and per-service observed state —
        `unknown` when unmanaged (no launcher on the channel) or for a service
        the launcher has not reported yet."""
        keys = [CORE_KEY, *STATIC_SERVICES]
        with self._lock:
            payload = self._payload
            received_at = self._received_at
        reported = (payload or {}).get("services", {}) if managed else {}
        services: dict[str, Any] = {}
        for key in keys:
            rec = reported.get(key)
            if rec is None:
                services[key] = {"state": "unknown"}
            else:
                services[key] = {
                    k: rec.get(k) for k in
                    ("state", "pid", "since", "last_exit", "message", "next_retry")
                    if k in rec
                }
        return {
            "managed": managed,
            "received_at": received_at if managed else None,
            "launcher": (payload or {}).get("launcher") if managed else None,
            "services": services,
        }


class ControlChannel:
    """The core's end of the launcher socket. `attach(fd)` adopts the
    inherited descriptor; `start(app)` pushes the initial snapshot and runs
    the reader thread; `push_desired()` sends a fresh snapshot (called from
    request threads after a settings write — one short line under a lock)."""

    def __init__(self) -> None:
        self._sock: socket.socket | None = None
        self._send_lock = threading.Lock()
        self._app: Any = None
        self._reader: threading.Thread | None = None
        self.status = LauncherStatus()

    # --- lifecycle -------------------------------------------------------------

    def attach(self, fd: int) -> None:
        self._sock = socket.socket(fileno=fd)
        self._sock.settimeout(None)

    def attach_socket(self, sock: socket.socket) -> None:
        self._sock = sock

    def start(self, app: Any) -> None:
        """Push the initial snapshot, then read status lines until EOF."""
        if self._sock is None:
            return
        self._app = app
        self.push_desired()
        self._reader = threading.Thread(target=self._read_loop, name="control-reader", daemon=True)
        self._reader.start()

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        self.status.reset()

    @property
    def managed(self) -> bool:
        return self._sock is not None

    # --- traffic ----------------------------------------------------------------

    def push_desired(self) -> None:
        """Send the current snapshot. Needs an app context for the settings
        read; request threads have one, the startup push pushes its own.
        A send failure means the launcher is gone: detach, log once."""
        if self._sock is None:
            return
        try:
            if self._app is not None and not _has_app_context():
                with self._app.app_context():
                    snapshot = desired_snapshot()
            else:
                snapshot = desired_snapshot()
        except Exception:
            logger.exception("control: could not build the desired snapshot")
            return
        self._send(snapshot)

    def _send(self, message: dict[str, Any]) -> None:
        sock = self._sock
        if sock is None:
            return
        line = (json.dumps(message) + "\n").encode()
        with self._send_lock:
            try:
                sock.settimeout(2.0)
                sock.sendall(line)
                sock.settimeout(None)
            except OSError as exc:
                logger.warning("control: launcher channel lost on send (%s); now unmanaged", exc)
                self.close()

    def _read_loop(self) -> None:
        sock = self._sock
        if sock is None:
            return
        buf = b""
        while True:
            try:
                chunk = sock.recv(65536)
            except OSError:
                chunk = b""
            if not chunk:
                logger.warning("control: launcher channel closed; now unmanaged")
                self.close()
                return
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                self._handle_line(raw)

    def _handle_line(self, raw: bytes) -> None:
        try:
            message = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.warning("control: unparseable line from the launcher")
            return
        if not isinstance(message, dict):
            return
        if message.get("type") == "status":
            try:
                self.status.accept(message)
            except ValueError as exc:
                logger.warning("control: status rejected: %s", exc)
        else:
            logger.warning("control: unknown message type %r", message.get("type"))

    def view(self) -> dict[str, Any]:
        return self.status.view(managed=self.managed)


def _has_app_context() -> bool:
    from flask import has_app_context
    return has_app_context()


CHANNEL = ControlChannel()
