import json
import sys
import types
from pathlib import Path

from research import search_brave, search_ddg, search_firecrawl, search_searxng

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_brave_parse():
    results = search_brave.parse_response(_load("brave_search.json"))
    assert [r.url for r in results] == [
        "https://example.org/alpha",
        "https://example.org/beta",
    ]
    assert results[0].title == "Alpha result"
    assert results[0].snippet == "Alpha description."


def test_brave_parse_skips_rows_without_url():
    payload = {"web": {"results": [{"title": "no url"}]}}
    assert search_brave.parse_response(payload) == []


def test_brave_parse_empty_payload():
    assert search_brave.parse_response({}) == []


def test_searxng_parse():
    results = search_searxng.parse_response(_load("searxng_search.json"))
    assert [r.url for r in results] == [
        "https://example.org/gamma",
        "https://example.org/delta",
    ]
    assert results[1].snippet == "Delta content."


def test_firecrawl_parse_v2_dict_data():
    results = search_firecrawl.parse_response(_load("firecrawl_search.json"))
    assert [r.url for r in results] == ["https://example.org/epsilon"]
    assert results[0].title == "Epsilon"


def test_firecrawl_parse_list_data():
    payload = {"data": [{"url": "https://example.org/z", "title": "Z"}]}
    results = search_firecrawl.parse_response(payload)
    assert [r.url for r in results] == ["https://example.org/z"]


def test_ddg_parse_rows():
    rows = [
        {"href": "https://example.org/a", "title": "A", "body": "aa"},
        {"title": "no url"},
        {"href": "https://example.org/b"},
    ]
    results = search_ddg.parse_rows(rows)
    assert [r.url for r in results] == [
        "https://example.org/a",
        "https://example.org/b",
    ]
    assert results[1].title == "https://example.org/b"


def test_ddg_search_uses_stubbed_client(monkeypatch):
    calls = {}

    class FakeDDGS:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def text(self, query, max_results):
            calls["query"] = query
            calls["max_results"] = max_results
            return [{"href": "https://example.org/a", "title": "A", "body": "aa"}]

    fake_module = types.SimpleNamespace(DDGS=FakeDDGS)
    monkeypatch.setitem(sys.modules, "ddgs", fake_module)
    results = search_ddg.PROVIDER.search("tides", 5)
    assert calls == {"query": "tides", "max_results": 5}
    assert results[0].url == "https://example.org/a"


def test_env_configuration(monkeypatch):
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    monkeypatch.delenv("SEARXNG_BASE_URL", raising=False)
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    assert search_brave.PROVIDER.is_configured() is False
    assert search_searxng.PROVIDER.is_configured() is False
    assert search_firecrawl.PROVIDER.is_configured() is False
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    monkeypatch.setenv("SEARXNG_BASE_URL", "http://searx.local:8080")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "k")
    assert search_brave.PROVIDER.is_configured() is True
    assert search_searxng.PROVIDER.is_configured() is True
    assert search_firecrawl.PROVIDER.is_configured() is True
