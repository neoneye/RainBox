"""Core-side half of service supervision: the desired-state snapshot the
launcher polls, the restart nonces it consumes, and the status it reports.
Imports `db`; the launcher never imports this module.

The core knows it is launcher-managed by two environment markers the
launcher sets on the core it spawns, `RAINBOX_LAUNCHER_ID` and
`RAINBOX_CORE_INSTANCE_ID`. A core started any other way (by hand,
`tools.serve_ui`) has neither, refuses desired/status traffic as
"unmanaged", and the /settings page says so instead of showing toggles
that would do nothing.
"""
from __future__ import annotations

import os
import threading
import time
import uuid
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

LAUNCHER_ID_ENV = "RAINBOX_LAUNCHER_ID"
CORE_INSTANCE_ID_ENV = "RAINBOX_CORE_INSTANCE_ID"

# Observed state older than this is shown as `unknown`: the launcher heartbeats
# every 30 s, so three missed beats means no launcher, or a dead one.
STATUS_STALE_AFTER: float = 90.0

STATES = frozenset({
    "starting", "running", "stopping", "stopped", "backoff", "failed",
    "credential missing", "not installed",
})


def instance_markers() -> dict[str, str] | None:
    """{launcher_id, core_instance_id} when this core was spawned by a
    launcher, else None."""
    lid = os.environ.get(LAUNCHER_ID_ENV, "").strip()
    cid = os.environ.get(CORE_INSTANCE_ID_ENV, "").strip()
    if not lid or not cid:
        return None
    return {"launcher_id": lid, "core_instance_id": cid}


def new_nonce() -> str:
    return uuid.uuid4().hex


def _known_key(service_key: str) -> bool:
    return service_key == CORE_KEY or service_key in STATIC_SERVICES


def bump_restart_nonce(service_key: str) -> str:
    """Rewrite one service's (or the core's) restart nonce. The launcher
    restarts the process when it sees the change; nothing executes here."""
    if not _known_key(service_key):
        raise KeyError(service_key)
    nonce = new_nonce()
    db.set_settings({nonce_setting_key(service_key): nonce})
    return nonce


def set_service_setting(key: str, value: object) -> bool:
    """Write a `services.<key>.enabled` or `services.<key>.env.<VAR>` setting
    and, if the effective value changed, rewrite that service's nonce in the
    same transaction — an enable or an environment edit is a restart-requiring
    change, and a quick off/on between two launcher polls must still reset a
    failed service. Returns whether the nonce was bumped. KeyError for a key
    this function does not own."""
    service_key = _service_key_of(key)
    if service_key is None:
        raise KeyError(key)
    # Row lock BEFORE the read: two concurrent writers of the same key must
    # serialize through read/compare/write, or one could read a stale value,
    # write its own, and wrongly call that "unchanged" — leaving the launcher
    # holding the other writer's nonce and never applying this value.
    db.lock_setting_row(key)
    before = db.get_setting(key)
    # Stage the write, compare the coerced effective value (the caller may
    # send "true" or True), stage the nonce if it changed, commit ONCE: a
    # crash between the two cannot leave a changed environment without the
    # nonce that restarts its service, and a concurrent desired-state read
    # sees both or neither.
    db.stage_setting(key, value)
    db.session.flush()
    after = db.get_setting(key)
    changed = after != before
    if changed:
        db.stage_setting(nonce_setting_key(service_key), new_nonce())
    db.session.commit()
    return changed


def _service_key_of(setting_key: str) -> str | None:
    """The service a public `services.*` setting belongs to, or None."""
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
    """What the launcher should be running, as one coherent snapshot. Includes
    disabled entries (so a missing key can later mean removal), never a
    credential value, never a path or argv — the launcher resolves kinds
    against its own copy of the catalogue."""
    markers = instance_markers()
    if markers is None:
        raise RuntimeError("unmanaged core: no launcher instance markers")
    # One statement for every services.* key, so an edit committed while we
    # assemble the response cannot yield an old env with a new nonce (which
    # the launcher would apply once and then never revisit).
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
        "schema_version": SCHEMA_VERSION,
        **markers,
        "core_pid": os.getpid(),
        "core_restart_nonce": values.get(nonce_setting_key(CORE_KEY)),
        "services": services,
    }


class LauncherStatus:
    """The last status table the launcher posted, in memory only. Accepts a
    report only from the launcher that spawned this core (matching markers)
    and only if its sequence number increases; receipt time is ours."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._payload: dict[str, Any] | None = None
        self._received_mono: float | None = None
        self._received_at: str | None = None
        self._last_sequence: int = -1

    def accept(self, payload: Any) -> None:
        markers = instance_markers()
        if markers is None:
            raise PermissionError("unmanaged core")
        if not isinstance(payload, dict):
            raise ValueError("status must be a JSON object")
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported schema_version")
        if (payload.get("launcher_id") != markers["launcher_id"]
                or payload.get("core_instance_id") != markers["core_instance_id"]):
            raise ValueError("instance markers do not match this core")
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
            self._received_mono = time.monotonic()
            self._received_at = datetime.now(UTC).isoformat()

    def reset(self) -> None:
        with self._lock:
            self._payload = None
            self._received_mono = None
            self._received_at = None
            self._last_sequence = -1

    def view(self, now: float | None = None) -> dict[str, Any]:
        """What /settings renders: managed?, stale?, and per-service observed
        state — `unknown` when unmanaged or when no report has arrived within
        STATUS_STALE_AFTER."""
        managed = instance_markers() is not None
        keys = [CORE_KEY, *STATIC_SERVICES]
        with self._lock:
            payload = self._payload
            received_mono = self._received_mono
            received_at = self._received_at
        now = time.monotonic() if now is None else now
        stale = (payload is None or received_mono is None
                 or now - received_mono > STATUS_STALE_AFTER)
        services: dict[str, Any] = {}
        reported = (payload or {}).get("services", {}) if not stale else {}
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
        launcher = (payload or {}).get("launcher") if not stale else None
        return {
            "managed": managed,
            "stale": stale,
            "received_at": received_at if not stale else None,
            "launcher": launcher,
            "services": services,
        }


STATUS = LauncherStatus()
