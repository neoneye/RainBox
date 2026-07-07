import pytest

from research import websearch
from research.websearch import SearchResult


class FakeProvider:
    def __init__(self, provider_id: str, configured: bool):
        self.id = provider_id
        self._configured = configured

    def is_configured(self) -> bool:
        return self._configured

    def search(self, query: str, count: int) -> list[SearchResult]:
        return [SearchResult(url="https://x.example", title="t", snippet="s")]


def _patch_registry(monkeypatch, providers):
    monkeypatch.setattr(websearch, "_registry", {p.id: p for p in providers})


def test_get_unknown_provider_raises_keyerror(monkeypatch):
    _patch_registry(monkeypatch, [FakeProvider("brave", True)])
    with pytest.raises(KeyError):
        websearch.get("bing")


def test_available_lists_only_configured(monkeypatch):
    _patch_registry(
        monkeypatch,
        [FakeProvider("brave", False), FakeProvider("ddg", True)],
    )
    assert websearch.available() == ["ddg"]


def test_resolve_named_provider(monkeypatch):
    _patch_registry(monkeypatch, [FakeProvider("searxng", True)])
    assert websearch.resolve("searxng").id == "searxng"


def test_resolve_named_but_unconfigured_raises(monkeypatch):
    _patch_registry(monkeypatch, [FakeProvider("brave", False)])
    with pytest.raises(RuntimeError, match="not configured"):
        websearch.resolve("brave")


def test_resolve_auto_prefers_brave_over_ddg(monkeypatch):
    _patch_registry(
        monkeypatch,
        [FakeProvider("ddg", True), FakeProvider("brave", True)],
    )
    assert websearch.resolve("auto").id == "brave"


def test_resolve_auto_falls_back_to_ddg(monkeypatch):
    _patch_registry(
        monkeypatch,
        [FakeProvider("brave", False), FakeProvider("ddg", True)],
    )
    assert websearch.resolve("auto").id == "ddg"


def test_resolve_auto_none_configured_raises(monkeypatch):
    _patch_registry(monkeypatch, [FakeProvider("brave", False)])
    with pytest.raises(RuntimeError, match="no search provider configured"):
        websearch.resolve("auto")
