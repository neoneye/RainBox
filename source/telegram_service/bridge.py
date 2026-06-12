"""Telegram <-> rainbox chatroom bridge — entrypoint and loop logic.

Run `python bridge.py` from inside telegram_service/ with its venv active and
the core webapp running. See README.md for setup. Two worker threads:
inbound (Telegram getUpdates -> POST chat message) and outbound (SSE ->
sendMessage). All loop logic takes injected client objects so tests use fakes.
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


# --- config -------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    bot_token: str
    allowed_user_ids: frozenset[int]
    rainbox_url: str
    room_name: str
    state_file: Path


def load_config(env: Mapping[str, str] = os.environ) -> Config:
    token = (env.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required (get one from @BotFather)")
    raw_ids = (env.get("TELEGRAM_ALLOWED_USER_IDS") or "").strip()
    ids = frozenset(int(part) for part in raw_ids.split(",") if part.strip())
    if not ids:
        raise SystemExit(
            "TELEGRAM_ALLOWED_USER_IDS is required (comma-separated numeric ids; "
            "message @userinfobot on Telegram to find yours)"
        )
    return Config(
        bot_token=token,
        allowed_user_ids=ids,
        rainbox_url=(env.get("RAINBOX_URL") or "http://127.0.0.1:5000").strip(),
        room_name=(env.get("TELEGRAM_ROOM_NAME") or "telegram").strip(),
        state_file=Path(env.get("TELEGRAM_STATE_FILE") or "state.json"),
    )


# --- state --------------------------------------------------------------


def load_state(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    """Atomic write (temp + rename) so a crash never truncates the state."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, path)


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


# --- inbound: Telegram -> chatroom ---------------------------------------


def process_updates(
    updates: list[dict[str, Any]],
    cfg: Config,
    state: dict[str, Any],
    rainbox: Any,
    room_uuid: str,
    limiter: RateLimitedLogger,
) -> None:
    """Handle one getUpdates batch. The offset advances per update only after
    that update is fully handled — a failed post raises BEFORE the advance, so
    Telegram redelivers it (at-least-once; see README)."""
    for update in updates:
        update_id = update["update_id"]
        msg = update.get("message") or {}
        from_id = (msg.get("from") or {}).get("id")
        text = msg.get("text")
        if from_id not in cfg.allowed_user_ids:
            limiter.warn(
                f"unauthorized:{from_id}",
                "dropping update from unauthorized telegram user %s", from_id,
            )
        elif text is None:
            logger.info("skipping non-text update %s (v1 is text-only)", update_id)
        else:
            rainbox.post_message(room_uuid, text)  # raises -> offset not advanced
            state["operator_chat_id"] = msg["chat"]["id"]
            logger.info("telegram -> room: %d chars", len(text))
        state["telegram_offset"] = update_id
        save_state(cfg.state_file, state)


# --- outbound: chatroom -> Telegram ---------------------------------------


def outbound_catchup(
    cfg: Config,
    state: dict[str, Any],
    rainbox: Any,
    telegram: Any,
    room_uuid: str,
) -> None:
    """Forward unseen finished agent messages to Telegram, advancing the
    cursor row by row. Stops at the first still-streaming row WITHOUT
    advancing past it: streamed rows are updated in place (same id) and the
    finalizing update fires another SSE event that re-runs this catch-up."""
    rows = rainbox.get_messages_after(room_uuid, state.get("room_cursor", 0))
    for row in rows:
        if row.get("streaming"):
            break
        if row.get("kind") == "message" and row.get("sender_type") == "agent":
            chat_id = state.get("operator_chat_id")
            if chat_id is None:
                logger.warning(
                    "agent reply not delivered: no operator chat id yet "
                    "(send any telegram message first); room row id=%s", row["id"],
                )
            else:
                telegram.send_message(chat_id, row.get("text") or "")
                logger.info("room -> telegram: row id=%s", row["id"])
        state["room_cursor"] = row["id"]
        save_state(cfg.state_file, state)


# --- loops ----------------------------------------------------------------

BACKOFF_CAP_SECONDS = 60.0


def _backoff_wait(attempt: int, stop: "threading.Event") -> None:
    stop.wait(min(BACKOFF_CAP_SECONDS, 2.0 ** attempt))


def init_room_cursor(cfg: Config, state: dict[str, Any], rainbox: Any, room_uuid: str) -> None:
    """First run only: start the cursor at the room's latest message so the
    bridge never replays history to Telegram."""
    if "room_cursor" in state:
        return
    rows = rainbox.get_messages_after(room_uuid, 0)
    state["room_cursor"] = max((r["id"] for r in rows), default=0)
    save_state(cfg.state_file, state)


def inbound_loop(
    cfg: Config, state: dict[str, Any], rainbox: Any, telegram: Any,
    room_uuid: str, stop: "threading.Event",
) -> None:
    limiter = RateLimitedLogger(60.0)
    attempt = 0
    while not stop.is_set():
        try:
            updates = telegram.get_updates(offset=state.get("telegram_offset", 0) + 1)
            process_updates(updates, cfg, state, rainbox, room_uuid, limiter)
            attempt = 0
        except Exception:
            attempt += 1
            logger.exception("inbound loop error (attempt %d)", attempt)
            _backoff_wait(attempt, stop)


def outbound_loop(
    cfg: Config, state: dict[str, Any], rainbox: Any, telegram: Any,
    room_uuid: str, stop: "threading.Event",
) -> None:
    attempt = 0
    while not stop.is_set():
        try:
            # catch up first: covers replies that landed while disconnected
            outbound_catchup(cfg, state, rainbox, telegram, room_uuid)
            for event in rainbox.iter_sse_events():
                attempt = 0
                if str(event.get("room_uuid")) == str(room_uuid):
                    outbound_catchup(cfg, state, rainbox, telegram, room_uuid)
                if stop.is_set():
                    break
        except Exception:
            attempt += 1
            logger.exception("outbound loop error (attempt %d)", attempt)
            _backoff_wait(attempt, stop)
        else:
            if not stop.is_set():
                # SSE generator ended without error (server restart): reconnect
                attempt += 1
                _backoff_wait(attempt, stop)


# --- entrypoint -------------------------------------------------------------


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    import requests  # imported here so tests never need it  # noqa: F401

    from rainbox_api import RainboxClient
    from telegram_api import TelegramClient

    cfg = load_config()
    rainbox = RainboxClient(cfg.rainbox_url)
    telegram = TelegramClient(cfg.bot_token)

    room = rainbox.find_room_by_name(cfg.room_name)
    if room is None:
        raise SystemExit(
            f"chatroom {cfg.room_name!r} not found at {cfg.rainbox_url}. Create it "
            f"on /chat in the webapp and add the agents that should answer, then rerun."
        )
    room_uuid = str(room["uuid"])
    logger.info("bridging telegram <-> room %r (%s)", cfg.room_name, room_uuid)

    state = load_state(cfg.state_file)
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
            args=(cfg, state, rainbox, telegram, room_uuid, stop), daemon=True,
        ),
        threading.Thread(
            target=outbound_loop, name="outbound",
            args=(cfg, state, rainbox, telegram, room_uuid, stop), daemon=True,
        ),
    ]
    for t in threads:
        t.start()
    while not stop.is_set():
        stop.wait(1.0)
    logger.info("bye")


if __name__ == "__main__":
    main()
