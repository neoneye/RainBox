import socket

import pytest
import requests

from research import fetch


def _fake_getaddrinfo(ip: str):
    def fake(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    return fake


def test_url_allowed_public(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34"))
    assert fetch.url_allowed("https://example.org/page") is True


@pytest.mark.parametrize(
    "ip", ["127.0.0.1", "10.0.0.5", "192.168.1.10", "169.254.1.1", "0.0.0.0"]
)
def test_url_allowed_refuses_non_public_ips(monkeypatch, ip):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo(ip))
    assert fetch.url_allowed("http://internal.example/admin") is False


def test_url_allowed_refuses_non_http_schemes():
    assert fetch.url_allowed("file:///etc/passwd") is False
    assert fetch.url_allowed("ftp://example.org/x") is False


def test_url_allowed_refuses_unresolvable(monkeypatch):
    def boom(host, port, *args, **kwargs):
        raise socket.gaierror("nope")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    assert fetch.url_allowed("https://doesnotexist.example") is False


def test_extract_text_strips_boilerplate():
    html = (
        "<html><head><style>body{color:red}</style>"
        "<script>alert(1)</script></head>"
        "<body><nav>menu</nav><p>Hello research world.</p></body></html>"
    )
    text = fetch.extract_text(html)
    assert "Hello research world." in text
    assert "alert(1)" not in text
    assert "color:red" not in text


class FakeResponse:
    def __init__(self, body: bytes):
        self._body = body
        self.encoding = "utf-8"

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i : i + chunk_size]


def test_fetch_extract_happy_path_and_char_cap(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34"))
    html = "<html><body><p>" + ("word " * 500) + "</p></body></html>"
    monkeypatch.setattr(
        fetch.requests, "get", lambda *a, **k: FakeResponse(html.encode())
    )
    text = fetch.fetch_extract("https://example.org/x", char_cap=50)
    assert text is not None
    assert len(text) <= 50


def test_fetch_extract_refuses_private_without_network(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("127.0.0.1"))

    def no_network(*a, **k):
        raise AssertionError("must not issue a request for a refused url")

    monkeypatch.setattr(fetch.requests, "get", no_network)
    assert fetch.fetch_extract("http://localhost/admin", char_cap=100) is None


def test_fetch_extract_returns_none_on_request_error(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34"))

    def boom(*a, **k):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(fetch.requests, "get", boom)
    assert fetch.fetch_extract("https://example.org/x", char_cap=100) is None


class FakeJsonResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_fetch_extract_firecrawl_happy_path_and_char_cap(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34"))
    monkeypatch.setenv("FIRECRAWL_API_KEY", "k")
    payload = {"data": {"markdown": "word " * 500}}
    monkeypatch.setattr(fetch.requests, "post", lambda *a, **k: FakeJsonResponse(payload))
    text = fetch.fetch_extract_firecrawl("https://example.org/x", char_cap=50)
    assert text is not None
    assert len(text) <= 50


def test_fetch_extract_firecrawl_refuses_private_without_network(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("10.0.0.5"))

    def no_network(*a, **k):
        raise AssertionError("must not issue a request for a refused url")

    monkeypatch.setattr(fetch.requests, "post", no_network)
    assert fetch.fetch_extract_firecrawl("http://internal.example/x", char_cap=100) is None


def test_fetch_extract_firecrawl_returns_none_on_request_error(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34"))
    monkeypatch.setenv("FIRECRAWL_API_KEY", "k")

    def boom(*a, **k):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(fetch.requests, "post", boom)
    assert fetch.fetch_extract_firecrawl("https://example.org/x", char_cap=100) is None


def test_fetch_extract_firecrawl_empty_markdown_returns_none(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34"))
    monkeypatch.setenv("FIRECRAWL_API_KEY", "k")
    monkeypatch.setattr(
        fetch.requests, "post", lambda *a, **k: FakeJsonResponse({"data": {}})
    )
    assert fetch.fetch_extract_firecrawl("https://example.org/x", char_cap=100) is None
