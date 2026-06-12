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
            if not isinstance(raw, str):
                continue
            if not raw or not raw.startswith("data: "):
                continue  # keepalive comments and blank separators
            try:
                yield json.loads(raw[len("data: "):])
            except json.JSONDecodeError:
                logger.warning("unparseable SSE payload: %r", raw[:200])
