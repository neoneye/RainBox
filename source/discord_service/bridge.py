"""Discord <-> rainbox chatroom bridge — entrypoint and loop logic.

Run `python bridge.py` from inside discord_service/ with its venv active and
the core webapp running. See README.md for setup. Two worker threads:
inbound (poll the channel -> POST chat message) and outbound (SSE -> Discord
messages: replies and notices as new messages, progress bubbles edited in
place and deleted when the core reaps them). All loop logic takes injected
client objects so tests use fakes.
"""
import json
import logging
import os
import signal
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

# Both worker threads persist the shared state dict; the lock makes each
# snapshot+write atomic so one thread's update can't be lost to the other's
# stale json.dumps or a clobbered temp file.
_state_lock = threading.Lock()

DISCORD_MAX_LEN = 2000
# What an empty progress row (the room's own "working" bubble) looks like on
# Discord, where an empty message is not allowed.
PROGRESS_PLACEHOLDER = "⏳ working…"
FORWARDED_KINDS = frozenset({"message", "notice"})
BACKOFF_CAP_SECONDS = 60.0


def redact(text: str, token: str) -> str:
    """The bot token travels in a header, not the URL, so requests' error
    text normally never carries it — but never trust that; scrub it anyway."""
    return text.replace(token, "<redacted>") if token else text


def truncate_text(text: str, limit: int = DISCORD_MAX_LEN) -> str:
    """A row edited in place (progress) stays ONE message: cut with an ellipsis."""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


# --- config -------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    bot_token: str
    channel_id: str
    allowed_user_ids: frozenset[str]
    rainbox_url: str
    room_name: str
    state_file: Path
    poll_seconds: float


def _snowflake(value: str, what: str, hint: str) -> str:
    value = value.strip()
    if not value.isdigit():
        raise SystemExit(f"{what} must be a numeric Discord id (got {value!r}). {hint}")
    return value


def load_config(env: Mapping[str, str] = os.environ) -> Config:
    token = (env.get("DISCORD_BOT_TOKEN") or "").strip()
    if not token:
        raise SystemExit(
            "DISCORD_BOT_TOKEN is required (Developer Portal -> your app -> Bot -> Reset Token)"
        )
    hint = "Enable Developer Mode in Discord (Settings -> Advanced), then right-click -> Copy ID."
    raw_channel = (env.get("DISCORD_CHANNEL_ID") or "").strip()
    if not raw_channel:
        raise SystemExit(f"DISCORD_CHANNEL_ID is required (the one text channel to bridge). {hint}")
    channel_id = _snowflake(raw_channel, "DISCORD_CHANNEL_ID", hint)
    raw_ids = (env.get("DISCORD_ALLOWED_USER_IDS") or "").strip()
    ids = frozenset(
        _snowflake(part, "DISCORD_ALLOWED_USER_IDS", hint)
        for part in raw_ids.split(",") if part.strip()
    )
    if not ids:
        raise SystemExit(
            f"DISCORD_ALLOWED_USER_IDS is required (comma-separated numeric user ids). {hint}"
        )
    try:
        poll = float((env.get("DISCORD_POLL_SECONDS") or "2").strip())
    except ValueError:
        raise SystemExit("DISCORD_POLL_SECONDS must be a number of seconds") from None
    return Config(
        bot_token=token,
        channel_id=channel_id,
        allowed_user_ids=ids,
        rainbox_url=(env.get("RAINBOX_URL") or "http://127.0.0.1:5000").strip(),
        room_name=(env.get("DISCORD_ROOM_NAME") or "discord").strip(),
        state_file=Path(env.get("DISCORD_STATE_FILE") or "state.json"),
        poll_seconds=poll,
    )


# --- state --------------------------------------------------------------


def load_state(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    """Atomic write (temp + rename) so a crash never truncates the state."""
    with _state_lock:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        os.replace(tmp, path)


def _progress_map(state: dict[str, Any]) -> dict[str, str]:
    """room row id (str) -> Discord message id, for live progress bubbles."""
    return state.setdefault("progress_messages", {})


def init_discord_cursor(cfg: Config, state: dict[str, Any], discord: Any) -> None:
    """First run only: start after the channel's newest message so Discord
    history is never replayed into the room."""
    if "discord_after" in state:
        return
    rows = discord.get_messages(cfg.channel_id, after=None, limit=1)
    state["discord_after"] = str(max((int(m["id"]) for m in rows), default=0))
    save_state(cfg.state_file, state)


def init_room_cursor(cfg: Config, state: dict[str, Any], rainbox: Any, room_uuid: str) -> None:
    """First run only: start the cursor at the room's latest message so the
    bridge never replays room history to Discord."""
    if "room_cursor" in state:
        return
    rows = rainbox.get_messages_after(room_uuid, 0)
    state["room_cursor"] = max((r["id"] for r in rows), default=0)
    save_state(cfg.state_file, state)


# --- logging helper -----------------------------------------------------


class RateLimitedLogger:
    """At most one warning per key per interval — so an unauthorized spammer
    can't flood the log."""

    def __init__(self, interval_seconds: float) -> None:
        self._interval = interval_seconds
        self._last: dict[str, float] = {}

    def warn(self, key: str, msg: str, *args: Any) -> None:
        now = time.monotonic()
        if now - self._last.get(key, float("-inf")) >= self._interval:
            self._last[key] = now
            logger.warning(msg, *args)


# --- inbound: Discord -> chatroom ---------------------------------------


def process_messages(
    messages: list[dict[str, Any]],
    cfg: Config,
    state: dict[str, Any],
    rainbox: Any,
    room_uuid: str,
    limiter: RateLimitedLogger,
) -> None:
    """Handle one poll's messages (oldest first). `discord_after` advances
    per message only after that message is fully handled — a failed post
    raises BEFORE the advance, so the next poll fetches it again
    (at-least-once; see README)."""
    for msg in messages:
        author = msg.get("author") or {}
        author_id = str(author.get("id"))
        content = msg.get("content") or ""
        if author.get("bot"):
            pass  # our own posts, and any other bot's
        elif author_id not in cfg.allowed_user_ids:
            limiter.warn(
                f"unauthorized:{author_id}",
                "dropping discord message from unauthorized user %s", author_id,
            )
        elif not content.strip():
            logger.info(
                "skipping discord message %s without text (attachments/embeds are not bridged)",
                msg.get("id"),
            )
        else:
            rainbox.post_message(room_uuid, content)  # raises -> cursor not advanced
            logger.info("discord -> room: %d chars", len(content))
        state["discord_after"] = str(msg["id"])
        save_state(cfg.state_file, state)


# --- outbound: chatroom -> Discord ----------------------------------------


def outbound_catchup(
    cfg: Config, state: dict[str, Any], rainbox: Any, discord: Any, room_uuid: str,
) -> None:
    """Forward unseen finished agent replies/notices as new Discord messages,
    advancing the cursor row by row. Stops at the first still-streaming row
    WITHOUT advancing past it: streamed rows are updated in place (same id)
    and the finalizing update fires another SSE event that re-runs this
    catch-up. Progress rows are skipped here — they are edited in place, so
    the cursor is the wrong tool; handle_event mirrors them from events."""
    rows = rainbox.get_messages_after(room_uuid, state.get("room_cursor", 0))
    for row in rows:
        if row.get("streaming"):
            break
        if row.get("kind") in FORWARDED_KINDS and row.get("sender_type") == "agent":
            discord.send_message(cfg.channel_id, row.get("text") or "")
            logger.info("room -> discord: %s row id=%s", row.get("kind"), row["id"])
        state["room_cursor"] = row["id"]
        save_state(cfg.state_file, state)


def forward_progress(
    cfg: Config, state: dict[str, Any], discord: Any, row_id: int, text: str,
) -> None:
    """Mirror one progress row: post its Discord message the first time, edit
    it in place after. Empty text (the room's own "working" bubble) shows the
    placeholder; long text is truncated so the bubble stays one message."""
    shown = truncate_text(text) if text.strip() else PROGRESS_PLACEHOLDER
    mapping = _progress_map(state)
    key = str(row_id)
    existing = mapping.get(key)
    if existing:
        discord.edit_message(cfg.channel_id, existing, shown)
    else:
        ids = discord.send_message(cfg.channel_id, shown)
        if ids:
            mapping[key] = ids[0]
    save_state(cfg.state_file, state)


def drop_progress(
    cfg: Config, state: dict[str, Any], discord: Any, row_ids: list[int],
) -> None:
    """The core reaped these progress rows: delete their Discord messages."""
    mapping = _progress_map(state)
    for rid in row_ids:
        did = mapping.pop(str(rid), None)
        if did:
            discord.delete_message(cfg.channel_id, did)
            logger.info("room -> discord: progress row id=%s reaped", rid)
    save_state(cfg.state_file, state)


def reconcile_progress(
    cfg: Config, state: dict[str, Any], rainbox: Any, discord: Any, room_uuid: str,
) -> None:
    """After a (re)connect: any mirrored progress row that no longer exists
    was reaped while we weren't listening — delete its Discord message."""
    gone = [
        int(key) for key in list(_progress_map(state))
        if rainbox.get_message(room_uuid, int(key)) is None
    ]
    if gone:
        drop_progress(cfg, state, discord, gone)


def handle_event(
    cfg: Config, state: dict[str, Any], rainbox: Any, discord: Any,
    room_uuid: str, event: dict[str, Any],
) -> None:
    """One SSE event for the bridge's room (payload shape: db.chat
    _chat_event_payload). Order matters: reaps first, then the progress
    mirror, then the cursor catch-up for finished replies/notices."""
    deleted = event.get("deleted_progress_ids") or []
    if deleted:
        drop_progress(cfg, state, discord, [int(i) for i in deleted])
    if event.get("event") == "delete":
        reconcile_progress(cfg, state, rainbox, discord, room_uuid)
    if event.get("kind") == "progress" and event.get("event") in ("insert", "update"):
        row_id = int(event["message_id"])
        text = event.get("text")
        if text is None:
            row = rainbox.get_message(room_uuid, row_id)
            if row is None:
                drop_progress(cfg, state, discord, [row_id])
                return
            text = row.get("text") or ""
        forward_progress(cfg, state, discord, row_id, text)
    outbound_catchup(cfg, state, rainbox, discord, room_uuid)


# --- loops ----------------------------------------------------------------


def _backoff_wait(attempt: int, stop: "threading.Event") -> None:
    stop.wait(min(BACKOFF_CAP_SECONDS, 2.0 ** attempt))


def inbound_loop(
    cfg: Config, state: dict[str, Any], rainbox: Any, discord: Any,
    room_uuid: str, stop: "threading.Event",
) -> None:
    limiter = RateLimitedLogger(60.0)
    attempt = 0
    while not stop.is_set():
        try:
            messages = discord.get_messages(
                cfg.channel_id, after=state.get("discord_after", "0")
            )
            process_messages(messages, cfg, state, rainbox, room_uuid, limiter)
            attempt = 0
            stop.wait(cfg.poll_seconds)
        except Exception as exc:
            attempt += 1
            logger.error(
                "inbound loop error (attempt %d): %s: %s",
                attempt, type(exc).__name__, redact(str(exc), cfg.bot_token),
            )
            _backoff_wait(attempt, stop)


def outbound_loop(
    cfg: Config, state: dict[str, Any], rainbox: Any, discord: Any,
    room_uuid: str, stop: "threading.Event",
) -> None:
    attempt = 0
    while not stop.is_set():
        try:
            # (Re)connect: bubbles reaped while we weren't listening, then
            # replies that landed meanwhile.
            reconcile_progress(cfg, state, rainbox, discord, room_uuid)
            outbound_catchup(cfg, state, rainbox, discord, room_uuid)
            for event in rainbox.iter_sse_events():
                attempt = 0
                if str(event.get("room_uuid")) == str(room_uuid):
                    handle_event(cfg, state, rainbox, discord, room_uuid, event)
                if stop.is_set():
                    break
        except Exception as exc:
            attempt += 1
            logger.error(
                "outbound loop error (attempt %d): %s: %s",
                attempt, type(exc).__name__, redact(str(exc), cfg.bot_token),
            )
            _backoff_wait(attempt, stop)
        else:
            if not stop.is_set():
                # SSE generator ended without error (server restart): reconnect
                attempt += 1
                _backoff_wait(attempt, stop)


# --- entrypoint -------------------------------------------------------------


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Deferred so `import bridge` stays stdlib-only for tests.
    from discord_api import DiscordClient
    from rainbox_api import RainboxClient

    cfg = load_config()
    rainbox = RainboxClient(cfg.rainbox_url)
    discord = DiscordClient(cfg.bot_token)

    try:
        me = discord.get_me()
    except Exception as exc:
        raise SystemExit(
            f"discord rejected the bot token: {type(exc).__name__}: "
            f"{redact(str(exc), cfg.bot_token)}"
        ) from None
    logger.info("discord bot %s (%s)", me.get("username"), me.get("id"))

    room = rainbox.find_room_by_name(cfg.room_name)
    if room is None:
        raise SystemExit(
            f"chatroom {cfg.room_name!r} not found at {cfg.rainbox_url}. Create it "
            f"on /chat in the webapp (as a direct room, pick its model), then rerun."
        )
    room_uuid = str(room["uuid"])
    logger.info(
        "bridging discord channel %s <-> room %r (%s)",
        cfg.channel_id, cfg.room_name, room_uuid,
    )

    state = load_state(cfg.state_file)
    init_discord_cursor(cfg, state, discord)
    init_room_cursor(cfg, state, rainbox, room_uuid)

    stop = threading.Event()

    def _shutdown(signum: int, frame: Any) -> None:
        logger.info("signal %d: shutting down", signum)
        stop.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    threads = [
        threading.Thread(
            target=inbound_loop, name="inbound",
            args=(cfg, state, rainbox, discord, room_uuid, stop), daemon=True,
        ),
        threading.Thread(
            target=outbound_loop, name="outbound",
            args=(cfg, state, rainbox, discord, room_uuid, stop), daemon=True,
        ),
    ]
    for t in threads:
        t.start()
    while not stop.is_set():
        stop.wait(1.0)
    logger.info("bye")


if __name__ == "__main__":
    main()
