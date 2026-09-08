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


def test_find_room_by_name():
    c = _client([FakeResponse([{"uuid": "u1", "name": "discord"}, {"uuid": "u2", "name": "x"}])])
    assert c.find_room_by_name("discord") == {"uuid": "u1", "name": "discord"}
    c = _client([FakeResponse([])])
    assert c.find_room_by_name("discord") is None


def test_post_message_posts_as_human():
    c = _client([FakeResponse({"id": 7, "uuid": "m7"})])
    assert c.post_message("u1", "hi") == {"id": 7, "uuid": "m7"}
    _m, url, kwargs = c._session.calls[0]
    assert url == "http://127.0.0.1:5000/chat/api/rooms/u1/messages"
    assert kwargs["json"] == {"text": "hi"}


def test_get_messages_after_passes_cursor():
    c = _client([FakeResponse([{"id": 8}])])
    assert c.get_messages_after("u1", 7) == [{"id": 8}]
    assert c._session.calls[0][2]["params"] == {"after": 7}


def test_get_message_200_and_404():
    c = _client([FakeResponse({"id": 8, "text": "t"}), FakeResponse({"error": "gone"}, status=404)])
    assert c.get_message("u1", 8) == {"id": 8, "text": "t"}
    assert c.get_message("u1", 9) is None
    assert c._session.calls[0][1] == "http://127.0.0.1:5000/chat/api/rooms/u1/messages/8"


def test_iter_sse_events_parses_data_lines_only():
    c = _client([FakeResponse(lines=[
        ": connected", "", 'data: {"room_uuid": "u1", "message_id": 3}', "",
        ": keepalive", "data: not json",
    ])])
    assert list(c.iter_sse_events()) == [{"room_uuid": "u1", "message_id": 3}]
