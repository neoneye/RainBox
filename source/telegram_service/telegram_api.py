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
