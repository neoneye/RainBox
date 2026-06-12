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
