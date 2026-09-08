"""DiscordClient against a fake requests session — no network."""
import pytest

from discord_api import DISCORD_MAX_LEN, DiscordClient, chunk_text


class FakeResponse:
    def __init__(self, payload=None, status=200):
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

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def _client(responses, slept=None):
    session = FakeSession(responses)
    sleep = slept.append if slept is not None else (lambda s: None)
    return DiscordClient("tok", session=session, sleep=sleep), session


def test_chunk_text():
    assert chunk_text("") == []
    assert chunk_text("hi") == ["hi"]
    chunks = chunk_text("x" * (DISCORD_MAX_LEN + 1))
    assert [len(c) for c in chunks] == [DISCORD_MAX_LEN, 1]


def test_headers_carry_bot_token_and_user_agent():
    client, session = _client([FakeResponse({"id": "42", "username": "bot"})])
    assert client.get_me()["id"] == "42"
    method, url, kwargs = session.calls[0]
    assert (method, url) == ("GET", "https://discord.com/api/v10/users/@me")
    assert kwargs["headers"]["Authorization"] == "Bot tok"
    assert kwargs["headers"]["User-Agent"].startswith("DiscordBot")


def test_get_messages_sorted_oldest_first_with_after():
    client, session = _client([FakeResponse([
        {"id": "300", "content": "c"}, {"id": "100", "content": "a"},
        {"id": "200", "content": "b"},
    ])])
    rows = client.get_messages("777", after="50")
    assert [r["id"] for r in rows] == ["100", "200", "300"]
    _m, url, kwargs = session.calls[0]
    assert url == "https://discord.com/api/v10/channels/777/messages"
    assert kwargs["params"] == {"limit": 100, "after": "50"}


def test_get_messages_without_after_omits_param():
    client, session = _client([FakeResponse([])])
    assert client.get_messages("777", after=None, limit=1) == []
    assert session.calls[0][2]["params"] == {"limit": 1}


def test_send_message_chunks_and_suppresses_mentions():
    client, session = _client([FakeResponse({"id": "1"}), FakeResponse({"id": "2"})])
    ids = client.send_message("777", "x" * (DISCORD_MAX_LEN + 5))
    assert ids == ["1", "2"]
    for _m, url, kwargs in session.calls:
        assert url == "https://discord.com/api/v10/channels/777/messages"
        assert kwargs["json"]["allowed_mentions"] == {"parse": []}
    assert len(session.calls[0][2]["json"]["content"]) == DISCORD_MAX_LEN
    assert session.calls[1][2]["json"]["content"] == "xxxxx"


def test_send_message_empty_posts_nothing():
    client, session = _client([])
    assert client.send_message("777", "") == []
    assert session.calls == []


def test_edit_and_delete():
    client, session = _client([FakeResponse({"id": "9"}), FakeResponse(None, status=204)])
    client.edit_message("777", "9", "new text")
    client.delete_message("777", "9")
    assert session.calls[0][:2] == ("PATCH", "https://discord.com/api/v10/channels/777/messages/9")
    assert session.calls[0][2]["json"] == {"content": "new text", "allowed_mentions": {"parse": []}}
    assert session.calls[1][:2] == ("DELETE", "https://discord.com/api/v10/channels/777/messages/9")


def test_delete_swallows_404():
    client, _session = _client([FakeResponse({"message": "Unknown Message"}, status=404)])
    client.delete_message("777", "9")  # no raise


def test_429_sleeps_retry_after_and_retries_once():
    slept = []
    client, session = _client(
        [FakeResponse({"retry_after": 1.5}, status=429), FakeResponse({"id": "1"})],
        slept=slept,
    )
    assert client.send_message("777", "hi") == ["1"]
    assert slept == [1.5]
    assert len(session.calls) == 2


def test_second_429_raises():
    client, _session = _client([
        FakeResponse({"retry_after": 1}, status=429),
        FakeResponse({"retry_after": 1}, status=429),
    ])
    with pytest.raises(RuntimeError):
        client.send_message("777", "hi")
