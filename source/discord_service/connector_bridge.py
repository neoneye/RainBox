"""Discord bridge, connector mode: one process per `bridge_connector` row,
configured from the core instead of the environment.

Selected by `BRIDGE_CONNECTOR=<connector-uuid>` (bridge.py dispatches here).
Deployment facts still come from the environment (`RAINBOX_URL`, the state
file path in `DISCORD_STATE_FILE`, and the bot token under the variable the
connector row NAMES as `token_env`); everything the operator edits on
`/bridges` — bindings, allowlists, forwarding policy, enabled flags — comes
from `GET /bridge/api/connectors/<uuid>/config`, fetched exactly when it can
have changed: once per SSE (re)connect and on every `bridge_config` event
naming this connector. Nothing polls for configuration.

Design: docs/superpowers/specs/2026-09-09-bridge-settings-design.md
("Live configuration contract", "Delivery state and ownership", "Cleanup
when a binding is removed"). Standard library only, like bridge.py: every
network call goes through injected client objects so tests use fakes.

Threads:
- config:   waits for a refetch request, fetches + validates the snapshot,
            publishes it (fresh only if nothing invalidated it meanwhile).
- applier:  applies each published snapshot: activates new bindings at the
            current high-water marks, retires removed ones (bounded cleanup
            of their mirrored progress bubbles), then catches up outbound.
- inbound:  polls every active binding's channel and posts allowed messages.
- outbound: holds /chat/stream open; routes room events to bindings and
            turns `bridge_config` events / stream drops into refetches.

Freshness is a fact, not a clock: a snapshot is fresh while it was fetched on
the currently open stream and no `bridge_config` event for this connector
arrived since. Workers check that, plus the binding's effective enablement
and direction, before every remote request (each chunk, and each 429 retry
inside the Discord client via the injected sleep).
"""
from __future__ import annotations

import json
import logging
import os
import re
import signal
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

logger = logging.getLogger(__name__)

EXIT_CONFIG_REJECTED = 2   # local validation failure or a confirmed credential rejection
EXIT_LOCK_HELD = 3         # another process owns this connector's state file

CONFIG_SCHEMA_VERSION = 1
STATE_SCHEMA_VERSION = 2
CONFIG_TIMEOUT_SECONDS = 5.0
CLEANUP_DEADLINE_SECONDS = 30.0
CLEANUP_REQUEST_TIMEOUT_SECONDS = 5.0
BACKOFF_CAP_SECONDS = 60.0
DISCORD_MAX_LEN = 2000
PROGRESS_PLACEHOLDER = "⏳ working…"
ROW_KINDS = ("message", "notice", "progress")
DIRECTIONS = ("both", "in", "out")

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SNOWFLAKE_RE = re.compile(r"^[0-9]{1,20}$")


class ConfigError(ValueError):
    """A config response the bridge refuses (schema, shape, wrong connector)."""


class Paused(RuntimeError):
    """A remote request was refused locally: the snapshot went stale, or the
    binding is no longer enabled for this direction. Not an error to back
    off from — the work resumes from cursors once the config is fresh."""


def redact(text: str, token: str) -> str:
    return text.replace(token, "<redacted>") if token else text


def truncate_text(text: str, limit: int = DISCORD_MAX_LEN) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def chunk_text(text: str, limit: int = DISCORD_MAX_LEN) -> list[str]:
    return [text[i:i + limit] for i in range(0, len(text), limit)] if text else []


def _backoff(attempt: int) -> float:
    return min(BACKOFF_CAP_SECONDS, 2.0 ** max(1, attempt))


# --- the config snapshot ---------------------------------------------------------


@dataclass(frozen=True)
class Policy:
    allowed_senders: frozenset[str]
    forward_kinds: frozenset[str]
    poll_seconds: float
    mirror_progress: bool
    direction: str

    @property
    def inbound(self) -> bool:
        return self.direction in ("both", "in")

    @property
    def outbound(self) -> bool:
        return self.direction in ("both", "out")


@dataclass(frozen=True)
class Binding:
    uuid: str
    room_uuid: str
    channel_id: str
    address: dict[str, str]
    address_key: str
    enabled: bool             # the row's own flag
    effective_enabled: bool   # connector AND folders AND row
    policy: Policy


@dataclass(frozen=True)
class Snapshot:
    revision: str
    connector_uuid: str
    name: str
    enabled: bool
    token_env: str
    bindings: dict[str, Binding]

    def active(self) -> list[Binding]:
        return [b for b in self.bindings.values() if self.enabled and b.effective_enabled]

    def for_room(self, room_uuid: str) -> list[Binding]:
        return [b for b in self.active() if b.room_uuid == room_uuid]


def _parse_policy(raw: Any) -> Policy:
    if not isinstance(raw, dict):
        raise ConfigError("binding policy must be an object")
    senders = raw.get("allowed_senders", [])
    kinds = raw.get("forward_kinds", [])
    if not isinstance(senders, list) or not all(isinstance(s, str) for s in senders):
        raise ConfigError("allowed_senders must be a list of strings")
    if not isinstance(kinds, list) or not all(k in ROW_KINDS for k in kinds):
        raise ConfigError("forward_kinds must list supported row kinds")
    poll = raw.get("poll_seconds", 2)
    if isinstance(poll, bool) or not isinstance(poll, (int, float)) or not (0.5 <= float(poll) <= 300):
        raise ConfigError("poll_seconds must be a number in [0.5, 300]")
    mirror = raw.get("mirror_progress", True)
    if not isinstance(mirror, bool):
        raise ConfigError("mirror_progress must be a boolean")
    direction = raw.get("direction", "both")
    if direction not in DIRECTIONS:
        raise ConfigError("direction must be both, in, or out")
    return Policy(frozenset(senders), frozenset(kinds), float(poll), mirror, direction)


def address_key(address: Mapping[str, Any]) -> str:
    """Canonical routing key for a Discord address (channel only; guild_id
    is informational)."""
    return f"channel_id={address['channel_id']}"


def parse_snapshot(payload: Any, expected_connector: str) -> Snapshot:
    """Validate one config response completely before anything uses it.
    Raises ConfigError for anything off; never trusts partial data."""
    if not isinstance(payload, dict):
        raise ConfigError("config response must be an object")
    if payload.get("schema_version") != CONFIG_SCHEMA_VERSION:
        raise ConfigError(f"unsupported config schema_version {payload.get('schema_version')!r}")
    revision = payload.get("revision")
    if not isinstance(revision, str) or not revision:
        raise ConfigError("config revision missing")
    conn = payload.get("connector")
    if not isinstance(conn, dict):
        raise ConfigError("connector missing")
    if conn.get("uuid") != expected_connector:
        raise ConfigError(f"config is for connector {conn.get('uuid')!r}, not {expected_connector}")
    if conn.get("platform") != "discord":
        raise ConfigError(f"connector platform is {conn.get('platform')!r}, not discord")
    token_env = conn.get("token_env")
    if not isinstance(token_env, str) or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", token_env):
        raise ConfigError("connector token_env must be an environment variable name")
    if not isinstance(conn.get("enabled"), bool):
        raise ConfigError("connector enabled must be a boolean")
    rows = payload.get("bindings")
    if not isinstance(rows, list):
        raise ConfigError("bindings must be a list")
    bindings: dict[str, Binding] = {}
    keys: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ConfigError("binding must be an object")
        uuid = row.get("uuid")
        room = row.get("room_uuid")
        if not isinstance(uuid, str) or not _UUID_RE.match(uuid) or uuid in bindings:
            raise ConfigError(f"binding uuid invalid or repeated: {uuid!r}")
        if not isinstance(room, str) or not _UUID_RE.match(room):
            raise ConfigError(f"binding {uuid}: room_uuid invalid")
        address = row.get("address")
        if not isinstance(address, dict) or not isinstance(address.get("channel_id"), str) \
                or not _SNOWFLAKE_RE.match(address["channel_id"]):
            raise ConfigError(f"binding {uuid}: address.channel_id must be a numeric id")
        if not isinstance(row.get("enabled"), bool) or not isinstance(row.get("effective_enabled"), bool):
            raise ConfigError(f"binding {uuid}: enabled flags must be booleans")
        key = address_key(address)
        if key in keys:
            raise ConfigError(f"binding {uuid}: address {key} repeated in one connector")
        keys.add(key)
        bindings[uuid] = Binding(
            uuid=uuid, room_uuid=room, channel_id=address["channel_id"],
            address={k: str(v) for k, v in address.items()}, address_key=key,
            enabled=row["enabled"], effective_enabled=row["effective_enabled"],
            policy=_parse_policy(row.get("policy", {})),
        )
    return Snapshot(revision=revision, connector_uuid=expected_connector,
                    name=str(conn.get("name") or ""), enabled=conn["enabled"],
                    token_env=token_env, bindings=bindings)


class ConfigState:
    """The published snapshot plus its freshness, shared by every thread.

    `invalidate` (stream connect, or a bridge_config event) bumps the
    epoch, clears freshness, and requests a fetch. `publish` keeps a
    snapshot fetched under a superseded epoch as the latest known data but
    leaves it stale, so a change that raced the fetch is fetched again.
    Freshness also requires the stream to be open: a snapshot fetched while
    disconnected is stored but stays stale, because the next change event
    could not reach us; `stream_opened` is what restores it (with a fetch)."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self.snapshot: Snapshot | None = None
        self.fresh = False
        self.stale_reason = "startup"
        self.epoch = 0
        self.stream_open = False
        self._wanted = False
        self.changes = 0   # bumps on every publish; workers wait on it

    def invalidate(self, reason: str) -> None:
        """A change may have happened: nothing is fresh until a fetch made
        on the open stream lands. No fetch is requested while the stream is
        closed — it could not be fresh anyway; the reconnect requests one."""
        with self._cond:
            self.epoch += 1
            self.fresh = False
            self.stale_reason = reason
            self._wanted = self.stream_open
            self.changes += 1
            self._cond.notify_all()

    def stream_opened(self) -> None:
        with self._cond:
            self.stream_open = True
        self.invalidate("stream connected")

    def stream_closed(self, reason: str) -> None:
        with self._cond:
            self.stream_open = False
        self.invalidate(reason)

    def request_fetch(self) -> None:
        with self._cond:
            self._wanted = True
            self._cond.notify_all()

    def wait_for_request(self, stop: threading.Event, timeout: float = 0.5) -> int | None:
        """Block until a fetch is wanted (returns the epoch it serves) or
        `stop` is set (returns None)."""
        with self._cond:
            while not self._wanted:
                if stop.is_set():
                    return None
                self._cond.wait(timeout)
            self._wanted = False
            return self.epoch

    def publish(self, snapshot: Snapshot, epoch: int) -> bool:
        with self._cond:
            self.snapshot = snapshot
            self.fresh = (epoch == self.epoch) and self.stream_open
            if not self.fresh:
                self.stale_reason = "changed during fetch" if self.stream_open else "stream not open"
                self._wanted = self.stream_open
            self.changes += 1
            self._cond.notify_all()
            return self.fresh

    def current(self) -> tuple[Snapshot | None, bool]:
        with self._cond:
            return self.snapshot, self.fresh

    def wait_change(self, seen: int, stop: threading.Event, timeout: float) -> int:
        """Sleep until the state changes past `seen` (or timeout/stop);
        returns the change counter to pass next time."""
        with self._cond:
            deadline = time.monotonic() + timeout
            while self.changes == seen and not stop.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(min(remaining, 0.5))
            return self.changes


# --- the state file --------------------------------------------------------------


class StateStore:
    """The connector's private state file (schema 2): connector identity plus
    per-binding cursors and progress maps. Atomic replace on every save;
    one process owns it through the sibling lock file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self.data: dict[str, Any] = {}

    def load(self, connector_uuid: str) -> str:
        """Returns 'missing' or 'ok'; raises ConfigError for a corrupt,
        incompatible, or foreign state file (never silently reset)."""
        try:
            raw = self.path.read_text()
        except FileNotFoundError:
            self.data = {"schema_version": STATE_SCHEMA_VERSION, "connector_uuid": connector_uuid,
                         "platform": "discord", "remote_identity": None, "bindings": {}}
            return "missing"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"state file {self.path} is not valid JSON ({exc}); move it aside to start fresh "
                              "(delivery positions will reset to newest)") from None
        if not isinstance(data, dict) or data.get("schema_version") != STATE_SCHEMA_VERSION:
            raise ConfigError(f"state file {self.path} has schema {data.get('schema_version') if isinstance(data, dict) else '?'}, "
                              f"expected {STATE_SCHEMA_VERSION}; run import_legacy.py for a legacy state.json, "
                              "or move the file aside")
        if data.get("connector_uuid") != connector_uuid:
            raise ConfigError(f"state file {self.path} belongs to connector {data.get('connector_uuid')}, "
                              f"not {connector_uuid}; each connector needs its own state file")
        if not isinstance(data.get("bindings"), dict):
            raise ConfigError(f"state file {self.path}: bindings must be an object")
        self.data = data
        return "ok"

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self.data))
            os.replace(tmp, self.path)

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def binding(self, uuid: str) -> dict[str, Any] | None:
        return self.data["bindings"].get(uuid)

    def binding_ids(self) -> list[str]:
        return list(self.data["bindings"])

    def set_binding(self, uuid: str, rec: dict[str, Any]) -> None:
        with self._lock:
            self.data["bindings"][uuid] = rec
            self.save()

    def remove_binding(self, uuid: str) -> None:
        with self._lock:
            self.data["bindings"].pop(uuid, None)
            self.save()


def acquire_state_lock(state_file: Path) -> Any:
    """Exclusive OS lock on `<state-file>.lock`, held for the process
    lifetime. Never unlink it. Returns the open file (keep a reference);
    raises SystemExit(3) when another process holds it."""
    import fcntl
    lock_path = state_file.with_name(state_file.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        logger.error("state lock %s is held by another process: this connector already runs "
                     "(or a manual instance does); not starting a duplicate", lock_path)
        raise SystemExit(EXIT_LOCK_HELD) from None
    return handle


# --- the runtime context ---------------------------------------------------------


class RateLimitedLogger:
    def __init__(self, interval_seconds: float) -> None:
        self._interval = interval_seconds
        self._last: dict[str, float] = {}

    def warn(self, key: str, msg: str, *args: Any) -> None:
        now = time.monotonic()
        if now - self._last.get(key, float("-inf")) >= self._interval:
            self._last[key] = now
            logger.warning(msg, *args)


class Bridge:
    """Everything the worker loops share. `discord` and `rainbox` are the
    injected clients; `token` is used only to redact log lines."""

    def __init__(self, connector_uuid: str, store: StateStore, config: ConfigState,
                 rainbox: Any, discord: Any, token: str = "") -> None:
        self.connector_uuid = connector_uuid
        self.store = store
        self.config = config
        self.rainbox = rainbox
        self.discord = discord
        self.token = token
        self._binding_locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        self._current = threading.local()   # the binding a worker thread is acting for
        self.limiter = RateLimitedLogger(60.0)
        self.first_applied = False

    # -- guards --

    def binding_lock(self, uuid: str) -> threading.RLock:
        with self._locks_guard:
            lock = self._binding_locks.get(uuid)
            if lock is None:
                lock = self._binding_locks[uuid] = threading.RLock()
            return lock

    def may_act(self, binding_uuid: str, direction: str) -> bool:
        """Fresh snapshot, connector enabled, binding present and effectively
        enabled, and its policy allows this direction ('in' | 'out')."""
        snap, fresh = self.config.current()
        if snap is None or not fresh or not snap.enabled:
            return False
        b = snap.bindings.get(binding_uuid)
        if b is None or not b.effective_enabled:
            return False
        return b.policy.inbound if direction == "in" else b.policy.outbound

    def require(self, binding_uuid: str, direction: str) -> Binding:
        """The binding as the CURRENT snapshot has it (policy included), or
        Paused. Callers read policy from the returned value, never from a
        binding captured before the loop began: a snapshot published mid-poll
        (a revoked sender, a narrowed forward list) applies to the very next
        message."""
        if not self.may_act(binding_uuid, direction):
            raise Paused(f"binding {binding_uuid} paused ({direction}); config {self.config.stale_reason if not self.config.fresh else 'fresh'}")
        snap, _ = self.config.current()
        assert snap is not None
        return snap.bindings[binding_uuid]

    def guarded_sleep(self, seconds: float) -> None:
        """Injected into the Discord client for its 429 retry: sleep, then
        refuse the retry if the acting binding may no longer send."""
        time.sleep(seconds)
        acting = getattr(self._current, "binding", None)
        if acting is not None:
            self.require(acting[0], acting[1])

    def acting_for(self, binding_uuid: str | None, direction: str = "out") -> None:
        self._current.binding = (binding_uuid, direction) if binding_uuid else None

    def log_error(self, prefix: str, exc: BaseException) -> None:
        logger.error("%s: %s: %s", prefix, type(exc).__name__, redact(str(exc), self.token))

    # -- per-binding state records --

    def activate(self, b: Binding) -> bool:
        """First activation: establish both high-water marks so neither
        channel history nor room history is replayed, persist, done."""
        try:
            rows = self.discord.get_messages(b.channel_id, after=None, limit=1)
            discord_after = str(max((int(m["id"]) for m in rows), default=0))
            room_rows = self.rainbox.get_messages_after(b.room_uuid, 0)
            room_cursor = max((r["id"] for r in room_rows), default=0)
        except Exception as exc:
            self.log_error(f"binding {b.uuid} ({b.address_key}) cannot activate", exc)
            return False
        self.store.set_binding(b.uuid, {
            "room_uuid": b.room_uuid, "address": dict(b.address), "address_key": b.address_key,
            "discord_after": discord_after, "room_cursor": room_cursor, "progress_messages": {},
        })
        logger.info("binding %s activated: room %s <-> %s (after %s, cursor %s)",
                    b.uuid, b.room_uuid, b.address_key, discord_after, room_cursor)
        return True

    def record_matches(self, b: Binding, rec: dict[str, Any]) -> bool:
        return rec.get("room_uuid") == b.room_uuid and rec.get("address_key") == b.address_key

    # -- inbound --

    def process_inbound(self, b: Binding, rec: dict[str, Any], messages: list[dict[str, Any]]) -> None:
        """One poll's messages, oldest first. The cursor advances per message
        only after it is fully handled; a failed post raises before the
        advance (at-least-once)."""
        for msg in messages:
            cur = self.require(b.uuid, "in")   # the allowlist as of NOW, not of the poll
            author = msg.get("author") or {}
            author_id = str(author.get("id"))
            content = msg.get("content") or ""
            if author.get("bot"):
                pass  # our own posts, and any other bot's — never echoed
            elif author_id not in cur.policy.allowed_senders:
                self.limiter.warn(f"unauthorized:{b.uuid}:{author_id}",
                                  "binding %s: dropping discord message from user %s (not in allowed_senders)",
                                  b.uuid, author_id)
            elif not content.strip():
                logger.info("binding %s: skipping message %s without text", b.uuid, msg.get("id"))
            else:
                self.require(b.uuid, "in")
                self.rainbox.post_message(b.room_uuid, content)
                logger.info("discord -> room %s: %d chars", b.room_uuid, len(content))
            rec["discord_after"] = str(msg["id"])
            self.store.save()

    # -- outbound --

    def send(self, b: Binding, text: str) -> list[str]:
        """Post text as chunks, re-checking the guard before each chunk."""
        ids: list[str] = []
        for chunk in chunk_text(text):
            self.require(b.uuid, "out")
            ids.extend(self.discord.send_message(b.channel_id, chunk))
        return ids

    def forward_progress(self, b: Binding, rec: dict[str, Any], row_id: int, text: str) -> None:
        shown = truncate_text(text) if text.strip() else PROGRESS_PLACEHOLDER
        mapping = rec.setdefault("progress_messages", {})
        key = str(row_id)
        existing = mapping.get(key)
        if not self.require(b.uuid, "out").policy.mirror_progress:
            self.send(b, shown)  # each update is its own message; nothing to edit later
            return
        if existing:
            self.require(b.uuid, "out")
            self.discord.edit_message(b.channel_id, existing, shown)
        else:
            ids = self.send(b, shown)
            if ids:
                mapping[key] = ids[0]
        self.store.save()

    def drop_progress(self, b: Binding, rec: dict[str, Any], row_ids: list[int]) -> None:
        mapping = rec.setdefault("progress_messages", {})
        for rid in row_ids:
            did = mapping.get(str(rid))
            if did:
                self.require(b.uuid, "out")
                self.discord.delete_message(b.channel_id, did)
                mapping.pop(str(rid), None)
                logger.info("binding %s: progress row %s reaped", b.uuid, rid)
        self.store.save()

    def reconcile_progress(self, b: Binding, rec: dict[str, Any]) -> None:
        gone = [int(k) for k in list(rec.get("progress_messages", {}))
                if self.rainbox.get_message(b.room_uuid, int(k)) is None]
        if gone:
            self.drop_progress(b, rec, gone)

    def outbound_catchup(self, b: Binding, rec: dict[str, Any]) -> None:
        """Forward unseen finished agent rows of the forwarded kinds
        (progress excluded: it is mirrored from events), advancing the
        cursor row by row and stopping at the first still-streaming row."""
        rows = self.rainbox.get_messages_after(b.room_uuid, rec.get("room_cursor", 0))
        for row in rows:
            if row.get("streaming"):
                break
            kinds = self.require(b.uuid, "out").policy.forward_kinds & {"message", "notice"}
            if row.get("kind") in kinds and row.get("sender_type") == "agent":
                self.send(b, row.get("text") or "")
                logger.info("room %s -> discord (%s): %s row id=%s", b.room_uuid, b.address_key, row.get("kind"), row["id"])
            rec["room_cursor"] = row["id"]
            self.store.save()

    def handle_event(self, b: Binding, rec: dict[str, Any], event: dict[str, Any]) -> None:
        deleted = event.get("deleted_progress_ids") or []
        if deleted:
            self.drop_progress(b, rec, [int(i) for i in deleted])
        if event.get("event") == "delete":
            self.reconcile_progress(b, rec)
        if (event.get("kind") == "progress" and event.get("event") in ("insert", "update")
                and "progress" in self.require(b.uuid, "out").policy.forward_kinds):
            row_id = int(event["message_id"])
            text = event.get("text")
            if text is None:
                row = self.rainbox.get_message(b.room_uuid, row_id)
                if row is None:
                    self.drop_progress(b, rec, [row_id])
                    return
                text = row.get("text") or ""
            self.forward_progress(b, rec, row_id, text)
        self.outbound_catchup(b, rec)

    def handle_room_event(self, room_uuid: str, event: dict[str, Any]) -> None:
        """Route one chat event to every active outbound binding of that
        room. Dropped while the snapshot is stale: the cursors catch up once
        it is fresh again."""
        snap, fresh = self.config.current()
        if snap is None or not fresh:
            return
        for b in snap.for_room(room_uuid):
            if not b.policy.outbound:
                continue
            with self.binding_lock(b.uuid):
                rec = self.store.binding(b.uuid)
                if rec is None or not self.record_matches(b, rec):
                    continue
                self.acting_for(b.uuid, "out")
                try:
                    self.handle_event(b, rec, event)
                except Paused as exc:
                    logger.info("%s", exc)
                except Exception as exc:
                    self.log_error(f"binding {b.uuid} outbound", exc)
                finally:
                    self.acting_for(None)

    def catchup_all(self) -> None:
        snap, fresh = self.config.current()
        if snap is None or not fresh:
            return
        for b in snap.active():
            if not b.policy.outbound:
                continue
            with self.binding_lock(b.uuid):
                rec = self.store.binding(b.uuid)
                if rec is None or not self.record_matches(b, rec):
                    continue
                self.acting_for(b.uuid, "out")
                try:
                    self.reconcile_progress(b, rec)
                    self.outbound_catchup(b, rec)
                except Paused as exc:
                    logger.info("%s", exc)
                except Exception as exc:
                    self.log_error(f"binding {b.uuid} catch-up", exc)
                finally:
                    self.acting_for(None)

    # -- removal --

    def cleanup_removed(self, uuid: str, rec: dict[str, Any], clock: Callable[[], float] = time.monotonic) -> None:
        """Bounded best-effort deletion of a removed binding's recorded
        progress bubbles, then prune its state. Each id is attempted once
        with no adapter retry; 2xx/404 complete it. Stops on the deadline,
        connector disablement, or lost freshness; reports the rest."""
        mapping = dict(rec.get("progress_messages") or {})
        channel = str((rec.get("address") or {}).get("channel_id") or "")
        failed: list[str] = []
        unattempted: list[str] = []
        deadline = clock() + CLEANUP_DEADLINE_SECONDS
        items = list(mapping.items())
        for i, (_row_id, message_id) in enumerate(items):
            snap, fresh = self.config.current()
            remaining = deadline - clock()
            if snap is None or not fresh or not snap.enabled or remaining <= 0 or not channel:
                unattempted = [m for _, m in items[i:]]
                break
            try:
                self.discord.delete_message_once(channel, message_id,
                                                 timeout=min(CLEANUP_REQUEST_TIMEOUT_SECONDS, remaining))
            except Exception as exc:
                failed.append(message_id)
                self.log_error(f"binding {uuid}: cleanup of message {message_id} in channel {channel} failed", exc)
        if failed or unattempted:
            logger.warning("binding %s removed: %d bubble(s) not cleaned up in channel %s "
                           "(failed: %s; unattempted: %s) — delete them by hand",
                           uuid, len(failed) + len(unattempted), channel, failed, unattempted)
        else:
            logger.info("binding %s removed: %d bubble(s) cleaned up", uuid, len(items))
        self.store.remove_binding(uuid)

    def report_orphan(self, uuid: str, rec: dict[str, Any]) -> None:
        mapping = rec.get("progress_messages") or {}
        logger.warning("state for binding %s (room %s, %s) is absent from the connector's config: "
                       "pruning it; %d mirrored bubble(s) may remain in channel %s for manual cleanup: %s",
                       uuid, rec.get("room_uuid"), rec.get("address_key"), len(mapping),
                       (rec.get("address") or {}).get("channel_id"), list(mapping.values()))
        self.store.remove_binding(uuid)

    # -- snapshot application --

    def apply_snapshot(self, snap: Snapshot) -> bool:
        """Retire what vanished (cleanup, or an orphan report on the first
        valid snapshot after start), activate what is new and enabled, then
        catch up outbound for everything active. Returns True when an
        activation failed (a transient error) so the applier retries."""
        first = not self.first_applied
        retry = False
        for uuid in self.store.binding_ids():
            if uuid in snap.bindings:
                continue
            with self.binding_lock(uuid):
                rec = self.store.binding(uuid)
                if rec is None:
                    continue
                if first:
                    self.report_orphan(uuid, rec)
                else:
                    self.cleanup_removed(uuid, rec)
        for b in snap.bindings.values():
            with self.binding_lock(b.uuid):
                rec = self.store.binding(b.uuid)
                if rec is not None and not self.record_matches(b, rec):
                    logger.error("binding %s: state file says room %s / %s but config says room %s / %s; "
                                 "not using that state (delete the binding and re-create it, or fix the file)",
                                 b.uuid, rec.get("room_uuid"), rec.get("address_key"), b.room_uuid, b.address_key)
                    continue
                if rec is None and snap.enabled and b.effective_enabled and not self.activate(b):
                    retry = True
        self.first_applied = True
        self.catchup_all()
        return retry


# --- loops -----------------------------------------------------------------------


def config_loop(bridge: Bridge, stop: threading.Event, on_snapshot: Callable[[Snapshot], None]) -> None:
    """Serve refetch requests one at a time: fetch, validate, publish, hand
    to the applier. A failure (timeout, 5xx, bad shape) keeps the config
    stale and retries with capped backoff; a 404 pauses until the next
    request (event or reconnect) without retrying."""
    attempt = 0
    while not stop.is_set():
        epoch = bridge.config.wait_for_request(stop)
        if epoch is None:
            break
        try:
            payload = bridge.rainbox.get_connector_config(bridge.connector_uuid)
        except Exception as exc:
            attempt += 1
            bridge.log_error(f"config fetch failed (attempt {attempt}); traffic paused", exc)
            stop.wait(_backoff(attempt))
            bridge.config.request_fetch()
            continue
        if payload is None:
            attempt = 0
            logger.warning("connector %s not found at the core (404): traffic paused until the next "
                           "bridge_config event or stream reconnect", bridge.connector_uuid)
            continue
        try:
            snap = parse_snapshot(payload, bridge.connector_uuid)
        except ConfigError as exc:
            attempt += 1
            logger.error("config rejected (attempt %d): %s; traffic paused", attempt, exc)
            stop.wait(_backoff(attempt))
            bridge.config.request_fetch()
            continue
        attempt = 0
        if bridge.config.publish(snap, epoch):
            logger.info("config revision %s: connector %s, %d binding(s), %d active",
                        snap.revision, "enabled" if snap.enabled else "disabled", len(snap.bindings), len(snap.active()))
            on_snapshot(snap)
        # else: something changed during the fetch; the request is already re-armed


class Applier:
    """Applies the latest published snapshot on its own thread so cleanup
    (up to 30 s) never blocks config fetches or the stream reader. An
    activation that failed transiently is retried with capped backoff by
    re-applying the current snapshot while it is still fresh."""

    def __init__(self, bridge: Bridge, clock: Callable[[], float] = time.monotonic) -> None:
        self.bridge = bridge
        self._cond = threading.Condition()
        self._pending: Snapshot | None = None
        self._clock = clock
        self._retry_at: float | None = None
        self._attempt = 0

    def submit(self, snap: Snapshot) -> None:
        with self._cond:
            self._pending = snap   # latest wins
            self._cond.notify_all()

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            with self._cond:
                while self._pending is None and not stop.is_set() and not (
                        self._retry_at is not None and self._clock() >= self._retry_at):
                    remaining = 0.5 if self._retry_at is None else max(0.0, min(0.5, self._retry_at - self._clock()))
                    self._cond.wait(remaining)
                snap, self._pending = self._pending, None
            if snap is None:
                if stop.is_set():
                    break
                self._retry_at = None
                snap, fresh = self.bridge.config.current()
                if snap is None or not fresh:
                    continue   # a future publish re-applies anyway
            try:
                retry = self.bridge.apply_snapshot(snap)
            except Exception as exc:
                self.bridge.log_error("applying config", exc)
                retry = True
            if retry:
                self._attempt += 1
                self._retry_at = self._clock() + _backoff(self._attempt)
                logger.info("config apply incomplete; retrying in %.0fs", _backoff(self._attempt))
            else:
                self._attempt, self._retry_at = 0, None


def inbound_loop(bridge: Bridge, stop: threading.Event) -> None:
    """Poll every active inbound binding's channel; sleep on the shortest
    poll interval, or until the config changes when nothing is active."""
    attempt = 0
    seen = 0
    while not stop.is_set():
        snap, fresh = bridge.config.current()
        active = [b for b in (snap.active() if snap and fresh else []) if b.policy.inbound]
        if not active:
            seen = bridge.config.wait_change(seen, stop, 60.0)
            continue
        for b in active:
            with bridge.binding_lock(b.uuid):
                if not bridge.may_act(b.uuid, "in"):
                    continue
                rec = bridge.store.binding(b.uuid)
                if rec is None or not bridge.record_matches(b, rec):
                    continue
                bridge.acting_for(b.uuid, "in")
                try:
                    messages = bridge.discord.get_messages(b.channel_id, after=rec.get("discord_after", "0"))
                    bridge.process_inbound(b, rec, messages)
                    attempt = 0
                except Paused as exc:
                    logger.info("%s", exc)
                except Exception as exc:
                    attempt += 1
                    bridge.log_error(f"binding {b.uuid} inbound error (attempt {attempt})", exc)
                finally:
                    bridge.acting_for(None)
        if attempt:
            stop.wait(_backoff(attempt))
        else:
            stop.wait(min(b.policy.poll_seconds for b in active))


def outbound_loop(bridge: Bridge, stop: threading.Event) -> None:
    """Hold the chat stream open. The stream's own `stream_open` marker and
    every `bridge_config` event for this connector invalidate the snapshot
    (which requests a fetch); room events are routed to bindings; a drop
    invalidates too, so nothing is delivered on a stale snapshot."""
    attempt = 0
    while not stop.is_set():
        try:
            for event in bridge.rainbox.iter_sse_events():
                attempt = 0
                kind = event.get("event")
                if kind == "stream_open":
                    bridge.config.stream_opened()
                elif kind == "bridge_config":
                    if str(event.get("connector_uuid")) == bridge.connector_uuid:
                        bridge.config.invalidate("bridge_config event")
                elif event.get("room_uuid"):
                    bridge.handle_room_event(str(event["room_uuid"]), event)
                if stop.is_set():
                    break
        except Exception as exc:
            attempt += 1
            bridge.config.stream_closed("stream dropped")
            bridge.log_error(f"stream error (attempt {attempt})", exc)
            stop.wait(_backoff(attempt))
        else:
            if not stop.is_set():
                attempt += 1
                bridge.config.stream_closed("stream ended")
                stop.wait(_backoff(attempt))


# --- startup -----------------------------------------------------------------------


def fetch_startup_snapshot(bridge: Bridge, stop: threading.Event) -> Snapshot | None:
    """Before any credential is read: the first valid snapshot, retried
    with capped backoff (including 404: the row may not exist yet). It only
    tells us the credential's variable name and validates the pairing; it
    is not fresh (no stream is open) and is never applied."""
    attempt = 0
    while not stop.is_set():
        try:
            payload = bridge.rainbox.get_connector_config(bridge.connector_uuid)
            if payload is None:
                raise ConfigError("connector not found at the core (404)")
            return parse_snapshot(payload, bridge.connector_uuid)
        except Exception as exc:
            attempt += 1
            bridge.log_error(f"startup config fetch failed (attempt {attempt}); retrying", exc)
            stop.wait(_backoff(attempt))
    return None


def verify_identity(bridge: Bridge, stop: threading.Event) -> dict[str, Any] | None:
    """`GET /users/@me` with the connector's credential: 401/403 is a
    confirmed rejection (exit 2); anything else (network, 429, 5xx) is
    retried, since it proves nothing about the credential. A bot id that
    differs from the one recorded in the state file is a mismatch (exit
    2): another bot must not inherit these checkpoints."""
    attempt = 0
    while not stop.is_set():
        try:
            me = bridge.discord.get_me()
        except Exception as exc:
            status = getattr(exc, "status", None)
            if status in (401, 403):
                logger.error("discord rejected the credential in %s: %s", bridge_token_env(bridge), redact(str(exc), bridge.token))
                raise SystemExit(EXIT_CONFIG_REJECTED) from None
            attempt += 1
            bridge.log_error(f"cannot reach discord (attempt {attempt}); retrying", exc)
            stop.wait(_backoff(attempt))
            continue
        bot_id = str(me.get("id") or "")
        recorded = bridge.store.data.get("remote_identity")
        if recorded and bot_id and recorded != bot_id:
            logger.error("state file %s was written by bot %s but this credential authenticates as bot %s; "
                         "refusing to reuse its checkpoints (use a fresh state file for a different bot)",
                         bridge.store.path, recorded, bot_id)
            raise SystemExit(EXIT_CONFIG_REJECTED)
        if bot_id and recorded != bot_id:
            bridge.store.data["remote_identity"] = bot_id
            bridge.store.save()
        return me
    return None


def bridge_token_env(bridge: Bridge) -> str:
    snap, _ = bridge.config.current()
    return snap.token_env if snap else "?"


def run(env: Mapping[str, str] = os.environ) -> None:
    """Entry point for connector mode (bridge.py calls this when
    BRIDGE_CONNECTOR is set)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from discord_api import DiscordClient          # deferred: keeps `import connector_bridge` stdlib-only
    from rainbox_api import RainboxClient

    connector_uuid = (env.get("BRIDGE_CONNECTOR") or "").strip().lower()
    if not _UUID_RE.match(connector_uuid):
        logger.error("BRIDGE_CONNECTOR must be the connector's uuid (from /bridges); got %r", connector_uuid)
        raise SystemExit(EXIT_CONFIG_REJECTED)
    rainbox_url = (env.get("RAINBOX_URL") or "http://127.0.0.1:5000").strip()
    state_file = Path(env.get("DISCORD_STATE_FILE") or f"bridge-{connector_uuid}.json")

    stop = threading.Event()

    def _shutdown(signum: int, frame: Any) -> None:
        logger.info("signal %d: shutting down", signum)
        stop.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    lock_handle = acquire_state_lock(state_file)   # exit 3 when held
    store = StateStore(state_file)
    try:
        found = store.load(connector_uuid)
    except ConfigError as exc:
        logger.error("%s", exc)
        raise SystemExit(EXIT_CONFIG_REJECTED) from None
    logger.info("connector %s: state file %s (%s)", connector_uuid, state_file, found)

    config = ConfigState()
    rainbox = RainboxClient(rainbox_url)
    bridge = Bridge(connector_uuid, store, config, rainbox, discord=None)
    startup = fetch_startup_snapshot(bridge, stop)
    if startup is None:
        return
    config.snapshot = startup   # known but not fresh: informs the credential name only
    token = (env.get(startup.token_env) or "").strip()
    if not token:
        logger.error("credential variable %s (the connector's token_env) is not set in this process's "
                     "environment; under the launcher put it in <state-dir>/credentials.env, "
                     "for a manual run export it before starting", startup.token_env)
        raise SystemExit(EXIT_CONFIG_REJECTED)
    bridge.token = token
    bridge.discord = DiscordClient(token, sleep=bridge.guarded_sleep)
    me = verify_identity(bridge, stop)
    if me is None:
        return
    logger.info("discord bot %s (%s); connector %r, %d binding(s) configured",
                me.get("username"), me.get("id"), startup.name, len(startup.bindings))

    applier = Applier(bridge)
    threads = [
        threading.Thread(target=config_loop, name="config", args=(bridge, stop, applier.submit), daemon=True),
        threading.Thread(target=applier.run, name="applier", args=(stop,), daemon=True),
        threading.Thread(target=inbound_loop, name="inbound", args=(bridge, stop), daemon=True),
        threading.Thread(target=outbound_loop, name="outbound", args=(bridge, stop), daemon=True),
    ]
    for t in threads:
        t.start()
    while not stop.is_set():
        stop.wait(1.0)
    for t in threads:
        t.join(timeout=3.0)
    del lock_handle   # closes the lock file; the file itself stays
    logger.info("bye")
