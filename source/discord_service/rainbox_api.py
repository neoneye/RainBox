"""Thin client for the rainbox chat JSON API + SSE stream.

The bridge is a pure consumer of the core's existing HTTP surface
(webapp/chat_api.py); the core never imports this service.
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
# Bound on one config snapshot fetch (design: "Live configuration contract").
CONFIG_TIMEOUT = 5.0


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
        triggers the room's responder, like typing in the web UI."""
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

    def get_message(self, room_uuid: str, message_id: int) -> dict[str, Any] | None:
        """One row by id, or None once it is gone (a reaped progress row)."""
        resp = self._session.get(
            f"{self._base}/chat/api/rooms/{room_uuid}/messages/{message_id}",
            timeout=30,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def get_connector_config(self, connector_uuid: str) -> dict[str, Any] | None:
        """The connector's resolved config snapshot (connector mode), or
        None when the core says the connector does not exist (404). Bounded
        to CONFIG_TIMEOUT so a wedged fetch cannot hold traffic on a stale
        snapshot; any other failure raises."""
        resp = self._session.get(
            f"{self._base}/bridge/api/connectors/{connector_uuid}/config",
            timeout=CONFIG_TIMEOUT,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def iter_sse_events(self) -> Iterator[dict[str, Any]]:
        """Yield parsed JSON payloads from /chat/stream, preceded by one
        synthetic `{"event": "stream_open"}` once the connection is up (a
        connector-mode bridge refetches its config at that point). Blocks
        while streaming; raises (requests exceptions) on disconnect/timeout
        — the caller reconnects with backoff."""
        resp = self._session.get(
            f"{self._base}/chat/stream",
            stream=True,
            timeout=(10, SSE_READ_TIMEOUT),
        )
        resp.raise_for_status()
        yield {"event": "stream_open"}
        for raw in resp.iter_lines(decode_unicode=True):
            if not isinstance(raw, str):
                continue
            if not raw or not raw.startswith("data: "):
                continue  # keepalive comments and blank separators
            try:
                yield json.loads(raw[len("data: "):])
            except json.JSONDecodeError:
                logger.warning("unparseable SSE payload: %r", raw[:200])
