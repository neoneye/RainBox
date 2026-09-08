"""Thin Discord REST client — only the calls the bridge needs.

Raw HTTP via `requests` (no discord.py, no gateway WebSocket): read a
channel's messages, post/edit/delete messages, identify the bot. See
https://discord.com/developers/docs/resources/message.
"""
import logging
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

API_BASE = "https://discord.com/api/v10"
DISCORD_MAX_LEN = 2000  # message content limit
RETRY_AFTER_CAP_SECONDS = 30.0

# Model output can contain "@everyone" or a role mention; never let it ping.
_NO_MENTIONS: dict[str, Any] = {"parse": []}


class DiscordAPIError(Exception):
    """A non-2xx Discord response, carrying the body's `message` and `code`
    (https://discord.com/developers/docs/topics/opcodes-and-status-codes#json).
    A bare HTTP status hides the one thing an operator needs: 403 "Missing
    Access" (50001, the bot is not in that server / can't see the channel)
    reads the same as 403 "Missing Permissions" (50013, a missing permission
    on a channel it can see)."""

    def __init__(self, status: int, method: str, path: str,
                 message: str | None, code: int | None) -> None:
        self.status = status
        self.method = method
        self.path = path
        self.message = message
        self.code = code
        detail = message or "no error body"
        if code is not None:
            detail += f" (code {code})"
        super().__init__(f"{status} on {method} {path}: {detail}")


def chunk_text(text: str, limit: int = DISCORD_MAX_LEN) -> list[str]:
    """Split text into <=limit chunks; empty text yields no chunks."""
    if not text:
        return []
    return [text[i : i + limit] for i in range(0, len(text), limit)]


class DiscordClient:
    def __init__(
        self, token: str, session: Any | None = None, sleep: Any = time.sleep
    ) -> None:
        self._headers = {
            "Authorization": f"Bot {token}",
            "User-Agent": "DiscordBot (rainbox discord_service, 1.0)",
        }
        self._session = session or requests.Session()
        self._sleep = sleep

    def _request(
        self, method: str, path: str, *, ok_404: bool = False, **kwargs: Any
    ) -> Any:
        """One call. A 429 is honored once (sleep the body's retry_after,
        capped, then retry); a second 429 or any other error status raises
        DiscordAPIError with the body's message and code."""
        url = f"{API_BASE}{path}"
        for attempt in (1, 2):
            resp = self._session.request(
                method, url, headers=self._headers, timeout=30, **kwargs
            )
            if resp.status_code == 429 and attempt == 1:
                try:
                    retry_after = float((resp.json() or {}).get("retry_after", 1.0))
                except Exception:
                    retry_after = 1.0
                wait = min(RETRY_AFTER_CAP_SECONDS, max(0.0, retry_after))
                logger.warning("discord rate limited; sleeping %.1fs", wait)
                self._sleep(wait)
                continue
            if ok_404 and resp.status_code == 404:
                return resp
            if resp.status_code >= 400:
                message = code = None
                try:
                    body = resp.json() or {}
                    message = body.get("message")
                    code = body.get("code")
                except Exception:
                    pass
                raise DiscordAPIError(resp.status_code, method, path, message, code)
            return resp
        raise RuntimeError("unreachable")

    def get_me(self) -> dict[str, Any]:
        return self._request("GET", "/users/@me").json()

    def get_messages(
        self, channel_id: str, after: str | None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Messages in the channel, OLDEST first (Discord returns newest
        first). `after` is a snowflake string; None = the newest page."""
        params: dict[str, Any] = {"limit": limit}
        if after is not None:
            params["after"] = after
        rows = self._request(
            "GET", f"/channels/{channel_id}/messages", params=params
        ).json()
        return sorted(rows, key=lambda m: int(m["id"]))

    def send_message(self, channel_id: str, text: str) -> list[str]:
        """Post text as one or more messages (chunked at the limit); returns
        the Discord message ids in order. Empty text posts nothing."""
        ids: list[str] = []
        for chunk in chunk_text(text):
            resp = self._request(
                "POST", f"/channels/{channel_id}/messages",
                json={"content": chunk, "allowed_mentions": _NO_MENTIONS},
            )
            ids.append(str(resp.json()["id"]))
        return ids

    def edit_message(self, channel_id: str, message_id: str, text: str) -> None:
        """Replace one message's content (caller keeps it within the limit)."""
        self._request(
            "PATCH", f"/channels/{channel_id}/messages/{message_id}",
            json={"content": text, "allowed_mentions": _NO_MENTIONS},
        )

    def delete_message(self, channel_id: str, message_id: str) -> None:
        """Delete one message; already gone (404) is fine."""
        self._request(
            "DELETE", f"/channels/{channel_id}/messages/{message_id}", ok_404=True
        )
