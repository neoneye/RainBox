# Telegram Bridge Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A standalone `telegram_service/` process (own venv, `requests`-only) that bridges Telegram ↔ one rainbox chatroom two-way, with zero core changes, per `docs/superpowers/specs/2026-06-12-telegram-service-design.md`.

**Architecture:** Two worker threads in one process. Inbound: long-poll Telegram `getUpdates` → `POST /chat/api/rooms/<uuid>/messages` (posts as the seeded human, which auto-triggers responder agents). Outbound: SSE `/chat/stream` → fetch new rows → `sendMessage` only agent `kind="message"` rows. State (`telegram_offset`, `operator_chat_id`, `room_cursor`) persists in a JSON file with atomic writes. All loop logic takes injected client objects so tests run with in-memory fakes, no network.

**Tech Stack:** Python 3 (stdlib + `requests`), pytest. No Flask, no python-telegram-bot, no core-venv changes.

---

## Context for an engineer with zero rainbox knowledge

- Work from `/Users/neoneye/git/rainbox/source`. Run tests with the ROOT venv: `venv/bin/python -m pytest -q telegram_service/` (the root venv has `requests`; the service's own venv is only for production runs).
- The core chat API (already exists, do NOT modify; read `webapp/chat_api.py` if curious):
  - `GET /chat/api/rooms` → JSON list of rooms, each with at least `{"uuid": str, "name": str}`.
  - `POST /chat/api/rooms/<room_uuid>/messages` body `{"text": "..."}` (no `sender_uuid`) → 201 `{"id": int, "uuid": str}`; posts as the human operator and triggers the room's responder agents.
  - `GET /chat/api/rooms/<room_uuid>/messages?after=<id>` → JSON list, oldest first, each row: `{"id": int, "uuid": str, "sender_uuid": str, "sender_name": str, "sender_type": "human"|"agent", "text": str, "content_type": str, "kind": str, "streaming": bool, "timestamp": str, "feedback": str|null}`.
  - `GET /chat/stream` → SSE; lines `data: {"room_uuid": ..., "message_id": ...,...}` per new/updated message plus `: keepalive` comment lines. Check `SSE_HEARTBEAT_SECONDS` in `webapp/chat_api.py` and use a read timeout of at least 3× that value in the SSE client.
- **Streaming wrinkle:** agent replies can stream — the same message row (same `id`) is updated repeatedly with `streaming: true`, then finalized with `streaming: false` (each update fires another SSE event). The outbound cursor must process rows in id order and STOP at the first `streaming: true` row without advancing past it; the finalizing event re-triggers the catch-up.
- The two pre-existing failing-suite quirks: run the full suite with `--ignore=whisper_service --ignore=kokoro_service`. Expected full-suite result: 995 passed, 10 skipped (+ your new tests).
- NEVER touch the `rainbox_production` Postgres DB. These tasks need no DB at all.
- All test file basenames must be unique repo-wide (`test_bridge.py`, `test_telegram_api.py`, `test_rainbox_api.py` are safe).

---

### Task 1: Scaffold (requirements + README)

**Files:**
- Create: `telegram_service/requirements.txt`
- Create: `telegram_service/README.md`

- [ ] **Step 1: Write `telegram_service/requirements.txt`**

```
# Telegram bridge service — isolated from the main project's deps.
# Fully pinned (direct + transitive) for supply-chain safety, following the
# kokoro_service convention. Regenerate with `pip freeze` after changes.

# Direct dependency
requests==2.34.2

# Transitive (pins copied from the kokoro_service lock, same platform)
certifi==2026.5.20
charset-normalizer==3.4.7
idna==3.18
urllib3==2.7.0
```

- [ ] **Step 2: Write `telegram_service/README.md`**

```markdown
# Telegram bridge service

A standalone process that bridges Telegram and one rainbox chatroom, two-way.
Kept separate from the main project (own venv) so no Telegram-related
dependency enters the main venv. Talks to the core over HTTP only (the chat
JSON API + SSE stream); the core never imports this code and was not changed
to support it.

- **Inbound:** messages you send to your Telegram bot are posted into the
  configured chatroom as you (the human operator) — the room's responder
  agents reply exactly as if you had typed in the web UI.
- **Outbound:** agent replies (`kind="message"` rows from agent senders) in
  that room are delivered back to your Telegram chat. Debug rows and your own
  messages are not forwarded.

## Setup

1. Create a bot: talk to @BotFather on Telegram → `/newbot` → copy the token.
2. Find your numeric Telegram user id (e.g. message @userinfobot).
3. In the rainbox webapp, create the room (default name `telegram`) on
   `/chat` and add the agents that should answer. The bridge never creates
   rooms.
4. Create the venv:

   ```bash
   cd telegram_service
   python3 -m venv venv
   venv/bin/pip install -r requirements.txt
   ```

## Run

With `main.py` (the core) already running:

```bash
cd telegram_service
TELEGRAM_BOT_TOKEN=123:abc \
TELEGRAM_ALLOWED_USER_IDS=987654321 \
venv/bin/python bridge.py
```

| Env var | Required | Default | Meaning |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | — | BotFather token |
| `TELEGRAM_ALLOWED_USER_IDS` | yes | — | comma-separated numeric user ids; everyone else is dropped |
| `RAINBOX_URL` | no | `http://127.0.0.1:5000` | core webapp base URL |
| `TELEGRAM_ROOM_NAME` | no | `telegram` | chatroom the bridge binds to |
| `TELEGRAM_STATE_FILE` | no | `./state.json` | offset/cursor persistence |

Stop with Ctrl-C.

## Behavior notes

- **At-least-once inbound:** the Telegram offset advances only after a message
  is successfully posted into the room; a crash at the wrong instant can
  duplicate one message after restart.
- **Replies need a first message:** outbound delivery starts after your first
  Telegram message (that's how the bridge learns your chat id).
- **No history replay:** on first run the room cursor starts at the room's
  latest message; old history is never sent to Telegram.
- **Text only (v1):** photos/voice/stickers are logged and skipped; replies
  are sent as plain text, chunked at Telegram's 4096-char limit.

## Tests

From the repo's source root: `venv/bin/python -m pytest -q telegram_service/`
(the bridge logic is tested with in-memory fakes; no network, no bot token).
```

- [ ] **Step 3: Commit**

```bash
git add telegram_service/requirements.txt telegram_service/README.md
git commit -m "feat(telegram_service): scaffold — pinned requirements and operator README"
```

---

### Task 2: Telegram API client

**Files:**
- Create: `telegram_service/telegram_api.py`
- Test: `telegram_service/test_telegram_api.py`

- [ ] **Step 1: Write the failing tests**

`telegram_service/test_telegram_api.py`:

```python
"""TelegramClient against a fake requests session — no network."""
import pytest

from telegram_api import TELEGRAM_MAX_LEN, TelegramClient, chunk_text


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.responses.pop(0)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.responses.pop(0)


def test_chunk_text_empty_returns_no_chunks():
    assert chunk_text("") == []


def test_chunk_text_short_is_single_chunk():
    assert chunk_text("hello") == ["hello"]


def test_chunk_text_splits_at_limit():
    text = "x" * (TELEGRAM_MAX_LEN + 1)
    chunks = chunk_text(text)
    assert len(chunks) == 2
    assert chunks[0] == "x" * TELEGRAM_MAX_LEN
    assert chunks[1] == "x"


def test_get_updates_returns_result_list():
    session = FakeSession([FakeResponse({"ok": True, "result": [{"update_id": 7}]})])
    client = TelegramClient("tok", session=session)
    updates = client.get_updates(offset=5)
    assert updates == [{"update_id": 7}]
    method, url, kwargs = session.calls[0]
    assert method == "GET"
    assert url.endswith("/bottok/getUpdates")
    assert kwargs["params"]["offset"] == 5


def test_get_updates_raises_when_not_ok():
    session = FakeSession([FakeResponse({"ok": False, "description": "nope"})])
    client = TelegramClient("tok", session=session)
    with pytest.raises(RuntimeError, match="getUpdates"):
        client.get_updates(offset=0)


def test_send_message_posts_each_chunk():
    long_text = "y" * (TELEGRAM_MAX_LEN + 10)
    session = FakeSession([FakeResponse({"ok": True}), FakeResponse({"ok": True})])
    client = TelegramClient("tok", session=session)
    client.send_message(chat_id=42, text=long_text)
    assert len(session.calls) == 2
    method, url, kwargs = session.calls[0]
    assert method == "POST"
    assert url.endswith("/bottok/sendMessage")
    assert kwargs["json"]["chat_id"] == 42
    assert kwargs["json"]["text"] == "y" * TELEGRAM_MAX_LEN
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest -q telegram_service/test_telegram_api.py`
Expected: FAIL (ModuleNotFoundError: telegram_api) — note pytest's rootdir
import: run from `source/`; the test imports `telegram_api` bare because the
test lives next to it (pytest inserts the test file's dir; there is NO
`__init__.py` in telegram_service — same as kokoro_service).

- [ ] **Step 3: Write `telegram_service/telegram_api.py`**

```python
"""Thin Telegram Bot API client — only the two calls the bridge needs.

Raw HTTP via `requests` (no python-telegram-bot): long-poll getUpdates and
sendMessage. See https://core.telegram.org/bots/api.
"""
import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)

TELEGRAM_MAX_LEN = 4096  # sendMessage text limit


def chunk_text(text: str, limit: int = TELEGRAM_MAX_LEN) -> list[str]:
    """Split text into <=limit chunks; empty text yields no chunks."""
    if not text:
        return []
    return [text[i : i + limit] for i in range(0, len(text), limit)]


class TelegramClient:
    def __init__(self, token: str, session: Any | None = None) -> None:
        self._base = f"https://api.telegram.org/bot{token}"
        self._session = session or requests.Session()

    def get_updates(self, offset: int, timeout: int = 50) -> list[dict[str, Any]]:
        """Long-poll for updates with update_id >= offset."""
        resp = self._session.get(
            f"{self._base}/getUpdates",
            params={"timeout": timeout, "offset": offset},
            timeout=timeout + 10,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram getUpdates not ok: {data}")
        return data.get("result", [])

    def send_message(self, chat_id: int, text: str) -> None:
        """Send text as plain-text message(s), chunked at the API limit."""
        for chunk in chunk_text(text):
            resp = self._session.post(
                f"{self._base}/sendMessage",
                json={"chat_id": chat_id, "text": chunk},
                timeout=30,
            )
            resp.raise_for_status()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest -q telegram_service/test_telegram_api.py`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add telegram_service/telegram_api.py telegram_service/test_telegram_api.py
git commit -m "feat(telegram_service): Telegram Bot API client (getUpdates, sendMessage, chunking)"
```

---

### Task 3: Rainbox API client

**Files:**
- Create: `telegram_service/rainbox_api.py`
- Test: `telegram_service/test_rainbox_api.py`

- [ ] **Step 1: Check the SSE heartbeat constant**

Run: `grep -n "SSE_HEARTBEAT_SECONDS" webapp/chat_api.py`
Note the value (call it H). The SSE read timeout below must be > 3×H; the code
uses 90 s which is correct if H ≤ 30 — adjust if H is larger.

- [ ] **Step 2: Write the failing tests**

`telegram_service/test_rainbox_api.py`:

```python
"""RainboxClient against a fake requests session — no network."""
from rainbox_api import RainboxClient


class FakeResponse:
    def __init__(self, payload=None, lines=None, status=200):
        self._payload = payload
        self._lines = lines or []
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload

    def iter_lines(self, decode_unicode=False):
        return iter(self._lines)


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.responses.pop(0)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.responses.pop(0)


def _client(responses):
    return RainboxClient("http://127.0.0.1:5000/", session=FakeSession(responses))


def test_find_room_by_name_returns_matching_room():
    rooms = [{"uuid": "u1", "name": "general"}, {"uuid": "u2", "name": "telegram"}]
    client = _client([FakeResponse(rooms)])
    assert client.find_room_by_name("telegram") == {"uuid": "u2", "name": "telegram"}


def test_find_room_by_name_returns_none_when_missing():
    client = _client([FakeResponse([{"uuid": "u1", "name": "general"}])])
    assert client.find_room_by_name("telegram") is None


def test_post_message_returns_created_row():
    client = _client([FakeResponse({"id": 9, "uuid": "m9"}, status=201)])
    out = client.post_message("u2", "hi")
    assert out == {"id": 9, "uuid": "m9"}
    method, url, kwargs = client._session.calls[0]
    assert url.endswith("/chat/api/rooms/u2/messages")
    assert kwargs["json"] == {"text": "hi"}


def test_get_messages_after_passes_cursor():
    client = _client([FakeResponse([{"id": 10}])])
    out = client.get_messages_after("u2", 9)
    assert out == [{"id": 10}]
    _, _, kwargs = client._session.calls[0]
    assert kwargs["params"] == {"after": 9}


def test_iter_sse_events_parses_data_lines_and_skips_comments():
    lines = [
        ": connected",
        'data: {"room_uuid": "u2", "message_id": 3}',
        "",
        ": keepalive",
        "data: not-json",
        'data: {"room_uuid": "u1", "message_id": 4}',
    ]
    client = _client([FakeResponse(lines=lines)])
    events = list(client.iter_sse_events())
    assert events == [
        {"room_uuid": "u2", "message_id": 3},
        {"room_uuid": "u1", "message_id": 4},
    ]
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `venv/bin/python -m pytest -q telegram_service/test_rainbox_api.py`
Expected: FAIL (ModuleNotFoundError: rainbox_api)

- [ ] **Step 4: Write `telegram_service/rainbox_api.py`**

```python
"""Thin client for the rainbox chat JSON API + SSE stream.

The bridge is a pure consumer of the core's existing HTTP surface
(webapp/chat_api.py); the core was not modified for this service.
"""
import json
import logging
from typing import Any, Iterator

import requests

logger = logging.getLogger(__name__)

# Read timeout for the SSE stream. The server emits a `: keepalive` comment
# every SSE_HEARTBEAT_SECONDS (webapp/chat_api.py), so a healthy stream never
# goes quiet this long; if it does, the connection is dead and we reconnect.
SSE_READ_TIMEOUT = 90.0


class RainboxClient:
    def __init__(self, base_url: str, session: Any | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._session = session or requests.Session()

    def find_room_by_name(self, name: str) -> dict[str, Any] | None:
        resp = self._session.get(f"{self._base}/chat/api/rooms", timeout=10)
        resp.raise_for_status()
        for room in resp.json():
            if room.get("name") == name:
                return room
        return None

    def post_message(self, room_uuid: str, text: str) -> dict[str, Any]:
        """Post as the seeded human operator (no sender_uuid) — this also
        triggers the room's responder agents, like typing in the web UI."""
        resp = self._session.post(
            f"{self._base}/chat/api/rooms/{room_uuid}/messages",
            json={"text": text},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def get_messages_after(self, room_uuid: str, after_id: int) -> list[dict[str, Any]]:
        resp = self._session.get(
            f"{self._base}/chat/api/rooms/{room_uuid}/messages",
            params={"after": after_id},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def iter_sse_events(self) -> Iterator[dict[str, Any]]:
        """Yield parsed JSON payloads from /chat/stream. Blocks while
        streaming; raises (requests exceptions) on disconnect/timeout —
        the caller reconnects with backoff."""
        resp = self._session.get(
            f"{self._base}/chat/stream",
            stream=True,
            timeout=(10, SSE_READ_TIMEOUT),
        )
        resp.raise_for_status()
        for raw in resp.iter_lines(decode_unicode=True):
            if not raw or not raw.startswith("data: "):
                continue  # keepalive comments and blank separators
            try:
                yield json.loads(raw[len("data: "):])
            except json.JSONDecodeError:
                logger.warning("unparseable SSE payload: %r", raw[:200])
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `venv/bin/python -m pytest -q telegram_service/test_rainbox_api.py`
Expected: 5 passed

- [ ] **Step 6: Commit**

```bash
git add telegram_service/rainbox_api.py telegram_service/test_rainbox_api.py
git commit -m "feat(telegram_service): rainbox chat-API client (rooms, messages, SSE)"
```

---

### Task 4: Bridge core — config, state, inbound logic

**Files:**
- Create: `telegram_service/bridge.py`
- Test: `telegram_service/test_bridge.py`

- [ ] **Step 1: Write the failing tests (config, state, inbound)**

`telegram_service/test_bridge.py`:

```python
"""Bridge logic with in-memory fakes — no network, no threads."""
import json

import pytest

from bridge import (
    Config,
    RateLimitedLogger,
    load_config,
    load_state,
    outbound_catchup,
    process_updates,
    save_state,
)


# --- fakes --------------------------------------------------------------


class FakeTelegram:
    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


class FakeRainbox:
    def __init__(self, messages=None, post_fails=False):
        self.posted = []
        self.messages = messages or []
        self.post_fails = post_fails
        self._next_id = 100

    def post_message(self, room_uuid, text):
        if self.post_fails:
            raise RuntimeError("rainbox down")
        self.posted.append((room_uuid, text))
        self._next_id += 1
        return {"id": self._next_id, "uuid": f"m{self._next_id}"}

    def get_messages_after(self, room_uuid, after_id):
        return [m for m in self.messages if m["id"] > after_id]


def _cfg(tmp_path, **overrides):
    base = dict(
        bot_token="tok",
        allowed_user_ids=frozenset({111}),
        rainbox_url="http://127.0.0.1:5000",
        room_name="telegram",
        state_file=tmp_path / "state.json",
    )
    base.update(overrides)
    return Config(**base)


def _update(update_id, from_id=111, chat_id=222, text="hello"):
    msg = {"from": {"id": from_id}, "chat": {"id": chat_id}}
    if text is not None:
        msg["text"] = text
    return {"update_id": update_id, "message": msg}


# --- config -------------------------------------------------------------


def test_load_config_reads_env(tmp_path):
    env = {
        "TELEGRAM_BOT_TOKEN": "tok",
        "TELEGRAM_ALLOWED_USER_IDS": "111, 222",
        "TELEGRAM_STATE_FILE": str(tmp_path / "s.json"),
    }
    cfg = load_config(env)
    assert cfg.bot_token == "tok"
    assert cfg.allowed_user_ids == frozenset({111, 222})
    assert cfg.rainbox_url == "http://127.0.0.1:5000"
    assert cfg.room_name == "telegram"


def test_load_config_missing_token_exits():
    with pytest.raises(SystemExit):
        load_config({"TELEGRAM_ALLOWED_USER_IDS": "111"})


def test_load_config_empty_allowlist_exits():
    with pytest.raises(SystemExit):
        load_config({"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_ALLOWED_USER_IDS": " "})


# --- state --------------------------------------------------------------


def test_state_round_trip_and_missing_file(tmp_path):
    path = tmp_path / "state.json"
    assert load_state(path) == {}
    save_state(path, {"telegram_offset": 5})
    assert load_state(path) == {"telegram_offset": 5}
    assert json.loads(path.read_text())["telegram_offset"] == 5


# --- inbound ------------------------------------------------------------


def test_inbound_posts_text_and_advances_offset(tmp_path):
    cfg = _cfg(tmp_path)
    state, rainbox = {}, FakeRainbox()
    process_updates([_update(7)], cfg, state, rainbox, "room-1", RateLimitedLogger(60))
    assert rainbox.posted == [("room-1", "hello")]
    assert state["telegram_offset"] == 7
    assert state["operator_chat_id"] == 222
    assert load_state(cfg.state_file)["telegram_offset"] == 7


def test_inbound_drops_unauthorized_but_advances_offset(tmp_path):
    cfg = _cfg(tmp_path)
    state, rainbox = {}, FakeRainbox()
    process_updates(
        [_update(8, from_id=999)], cfg, state, rainbox, "room-1", RateLimitedLogger(60)
    )
    assert rainbox.posted == []
    assert state["telegram_offset"] == 8
    assert "operator_chat_id" not in state


def test_inbound_skips_non_text_but_advances_offset(tmp_path):
    cfg = _cfg(tmp_path)
    state, rainbox = {}, FakeRainbox()
    process_updates([_update(9, text=None)], cfg, state, rainbox, "room-1", RateLimitedLogger(60))
    assert rainbox.posted == []
    assert state["telegram_offset"] == 9


def test_inbound_post_failure_does_not_advance_offset(tmp_path):
    cfg = _cfg(tmp_path)
    state, rainbox = {"telegram_offset": 6}, FakeRainbox(post_fails=True)
    with pytest.raises(RuntimeError, match="rainbox down"):
        process_updates([_update(7)], cfg, state, rainbox, "room-1", RateLimitedLogger(60))
    assert state["telegram_offset"] == 6  # redelivered on next poll
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest -q telegram_service/test_bridge.py`
Expected: FAIL (ModuleNotFoundError: bridge)

- [ ] **Step 3: Write `telegram_service/bridge.py` (config, state, rate-limited log, inbound; outbound comes in Task 5)**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest -q telegram_service/test_bridge.py`
Expected: 8 passed (the `outbound_catchup` import will fail until Task 5 —
if it does, temporarily remove `outbound_catchup` from the test imports in
this task and re-add it in Task 5 Step 1; keep the test file exactly as
written otherwise)

- [ ] **Step 5: Commit**

```bash
git add telegram_service/bridge.py telegram_service/test_bridge.py
git commit -m "feat(telegram_service): config, atomic state, inbound update processing"
```

---

### Task 5: Outbound logic (chatroom → Telegram)

**Files:**
- Modify: `telegram_service/bridge.py` (append)
- Test: `telegram_service/test_bridge.py` (append)

- [ ] **Step 1: Append the failing tests to `telegram_service/test_bridge.py`**

```python
# --- outbound -----------------------------------------------------------


def _agent_msg(mid, text="reply", kind="message", sender_type="agent", streaming=False):
    return {
        "id": mid, "uuid": f"m{mid}", "text": text, "kind": kind,
        "sender_type": sender_type, "streaming": streaming,
    }


def test_outbound_forwards_agent_messages_only(tmp_path):
    cfg = _cfg(tmp_path)
    state = {"operator_chat_id": 222, "room_cursor": 0}
    rainbox = FakeRainbox(messages=[
        _agent_msg(1, text="from human", sender_type="human"),
        _agent_msg(2, text="debug row", kind="debug-router"),
        _agent_msg(3, text="real reply"),
    ])
    telegram = FakeTelegram()
    outbound_catchup(cfg, state, rainbox, telegram, "room-1")
    assert telegram.sent == [(222, "real reply")]
    assert state["room_cursor"] == 3


def test_outbound_stops_at_streaming_row(tmp_path):
    cfg = _cfg(tmp_path)
    state = {"operator_chat_id": 222, "room_cursor": 0}
    rainbox = FakeRainbox(messages=[
        _agent_msg(1, text="done reply"),
        _agent_msg(2, text="half a repl", streaming=True),
        _agent_msg(3, text="later reply"),
    ])
    telegram = FakeTelegram()
    outbound_catchup(cfg, state, rainbox, telegram, "room-1")
    # forwarded the finished row, then STOPPED at the streaming row without
    # advancing past it — its finalizing SSE event re-triggers catch-up
    assert telegram.sent == [(222, "done reply")]
    assert state["room_cursor"] == 1


def test_outbound_without_chat_id_advances_cursor_without_sending(tmp_path):
    cfg = _cfg(tmp_path)
    state = {"room_cursor": 0}
    rainbox = FakeRainbox(messages=[_agent_msg(1)])
    telegram = FakeTelegram()
    outbound_catchup(cfg, state, rainbox, telegram, "room-1")
    assert telegram.sent == []
    assert state["room_cursor"] == 1
```

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `venv/bin/python -m pytest -q telegram_service/test_bridge.py`
Expected: 3 failures (NameError/ImportError on outbound_catchup), 8 pass

- [ ] **Step 3: Append to `telegram_service/bridge.py`**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest -q telegram_service/test_bridge.py`
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
git add telegram_service/bridge.py telegram_service/test_bridge.py
git commit -m "feat(telegram_service): outbound catch-up with streaming-aware cursor"
```

---

### Task 6: Loops, threads, and main()

**Files:**
- Modify: `telegram_service/bridge.py` (append)
- Test: `telegram_service/test_bridge.py` (append)

- [ ] **Step 1: Append the failing tests**

```python
# --- loops / main wiring --------------------------------------------------

import threading

from bridge import inbound_loop, init_room_cursor, outbound_loop


class FakeTelegramPolling(FakeTelegram):
    """get_updates returns one batch, then sets stop so loops exit in tests."""

    def __init__(self, batches, stop):
        super().__init__()
        self.batches = list(batches)
        self.stop = stop

    def get_updates(self, offset, timeout=50):
        if self.batches:
            return self.batches.pop(0)
        self.stop.set()
        return []


class FakeRainboxSSE(FakeRainbox):
    def __init__(self, events, stop, **kwargs):
        super().__init__(**kwargs)
        self.events = list(events)
        self.stop = stop

    def iter_sse_events(self):
        yield from self.events
        self.stop.set()


def test_init_room_cursor_uses_latest_message(tmp_path):
    cfg = _cfg(tmp_path)
    rainbox = FakeRainbox(messages=[_agent_msg(4), _agent_msg(9)])
    state = {}
    init_room_cursor(cfg, state, rainbox, "room-1")
    assert state["room_cursor"] == 9  # never replay history


def test_init_room_cursor_keeps_existing(tmp_path):
    cfg = _cfg(tmp_path)
    state = {"room_cursor": 5}
    init_room_cursor(cfg, state, FakeRainbox(messages=[_agent_msg(9)]), "room-1")
    assert state["room_cursor"] == 5


def test_inbound_loop_processes_then_stops(tmp_path):
    cfg = _cfg(tmp_path)
    stop = threading.Event()
    telegram = FakeTelegramPolling([[_update(7)]], stop)
    rainbox = FakeRainbox()
    state = {}
    inbound_loop(cfg, state, rainbox, telegram, "room-1", stop)
    assert rainbox.posted == [("room-1", "hello")]
    assert state["telegram_offset"] == 7


def test_outbound_loop_catches_up_on_matching_room_event(tmp_path):
    cfg = _cfg(tmp_path)
    stop = threading.Event()
    rainbox = FakeRainboxSSE(
        events=[{"room_uuid": "other", "message_id": 1},
                {"room_uuid": "room-1", "message_id": 2}],
        stop=stop,
        messages=[_agent_msg(2)],
    )
    telegram = FakeTelegram()
    state = {"operator_chat_id": 222, "room_cursor": 0}
    outbound_loop(cfg, state, rainbox, telegram, "room-1", stop)
    assert telegram.sent == [(222, "reply")]
```

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `venv/bin/python -m pytest -q telegram_service/test_bridge.py`
Expected: 4 failures (ImportError on inbound_loop etc.), 11 pass

- [ ] **Step 3: Append to `telegram_service/bridge.py`**

```python
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
    import requests  # imported here so tests never need it

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
```

Note: `import requests` inside `main()` plus the lazy client imports keep
`import bridge` dependency-free for tests; module level needs only stdlib.

- [ ] **Step 4: Run all service tests**

Run: `venv/bin/python -m pytest -q telegram_service/`
Expected: 26 passed (6 telegram_api + 5 rainbox_api + 15 bridge)

- [ ] **Step 5: Commit**

```bash
git add telegram_service/bridge.py telegram_service/test_bridge.py
git commit -m "feat(telegram_service): loops, threads, signal handling, main entrypoint"
```

---

### Task 7: Repo integration + live smoke test

**Files:**
- Modify: `README.md` (Layout table)
- Modify: `telegram_service/.gitignore` (create)

- [ ] **Step 1: Add a services row to the root Layout table in `README.md`**

After the `data/` row in the `## Layout` table, add:

```markdown
| `kokoro_service/`, `whisper_service/`, `telegram_service/` | standalone processes with their own venvs (TTS, STT, Telegram bridge) — the core talks to/with them over HTTP only |
```

- [ ] **Step 2: Create `telegram_service/.gitignore`**

```
venv/
state.json
state.tmp
```

- [ ] **Step 3: Full suite**

Run: `venv/bin/python -m pytest -q --ignore=whisper_service --ignore=kokoro_service`
Expected: 1021 passed, 10 skipped (995 baseline + 26 new), 0 failed

- [ ] **Step 4: Live smoke test (manual, operator-assisted; skip cleanly if no bot token is available)**

This step needs a real bot token and is expected to be done BY OR WITH the
operator; an agentic worker should do the no-token parts and report the rest
as pending:

No-token parts (do these):
```bash
# config validation fails fast with clear messages:
cd telegram_service && python3 ../venv/bin/python bridge.py 2>&1 | head -2   # expect TELEGRAM_BOT_TOKEN error, exit 1
# room-missing path (claude DB, core running):
# DATABASE_URL=postgresql+psycopg://localhost/rainbox_claude venv/bin/python main.py  (in another shell, from source/)
# TELEGRAM_BOT_TOKEN=fake TELEGRAM_ALLOWED_USER_IDS=1 TELEGRAM_ROOM_NAME=definitely-missing ../venv/bin/python bridge.py
#   -> expect "chatroom 'definitely-missing' not found" exit
```

With-token parts (operator): follow README Run section end-to-end — message
the bot, see it in the room, see the agent reply arrive on Telegram.

- [ ] **Step 5: Commit**

```bash
git add README.md telegram_service/.gitignore
git commit -m "feat(telegram_service): repo integration (README layout row, service gitignore)"
```

---

## Self-review notes (already applied)

- Spec coverage: layout/deps (Task 1-2), inbound incl. allowlist + at-least-once (Task 4), outbound incl. agent-only filter + chunking + no-chat-id rule (Tasks 2, 5), SSE + reconnect + catch-up (Tasks 3, 6), state file + atomic writes + cursor init (Tasks 4, 6), env config + fail-fast validation (Task 4), room discovery fail-fast (Task 6), README runbook (Task 1), tests-with-fakes (every task). Streaming-row handling (Task 5) is an addition the spec missed; it preserves the spec's "forward finished agent replies" intent.
- The Task 4 test file imports `outbound_catchup` before Task 5 defines it — Step 4 of Task 4 calls this out explicitly with the temporary-removal instruction.
- Type consistency: `process_updates(updates, cfg, state, rainbox, room_uuid, limiter)`, `outbound_catchup(cfg, state, rainbox, telegram, room_uuid)`, loops `(cfg, state, rainbox, telegram, room_uuid, stop)` — checked against all call sites in tests and main().
