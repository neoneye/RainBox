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
