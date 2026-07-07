"""Search provider protocol + registry.

Mirrors providers/registry.py (id -> instance), but lazily: the concrete
provider modules are imported on first use so `import research.websearch`
stays dependency-free. `resolve("auto")` picks the first configured provider
in AUTO_ORDER — ddg last because it is keyless but rate-limity."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class SearchResult:
    url: str
    title: str
    snippet: str


class SearchProvider(Protocol):
    id: str

    def is_configured(self) -> bool: ...

    def search(self, query: str, count: int) -> list[SearchResult]: ...


AUTO_ORDER = ("brave", "searxng", "firecrawl", "ddg")

_registry: dict[str, SearchProvider] | None = None


def _providers() -> dict[str, SearchProvider]:
    global _registry
    if _registry is None:
        from research import (
            search_brave,
            search_ddg,
            search_firecrawl,
            search_searxng,
        )

        instances: tuple[SearchProvider, ...] = (
            search_brave.PROVIDER,
            search_searxng.PROVIDER,
            search_firecrawl.PROVIDER,
            search_ddg.PROVIDER,
        )
        _registry = {provider.id: provider for provider in instances}
    return _registry


def get(provider_id: str) -> SearchProvider:
    providers = _providers()
    try:
        return providers[provider_id]
    except KeyError:
        raise KeyError(
            f"unknown search provider {provider_id!r}; known: {sorted(providers)}"
        ) from None


def available() -> list[str]:
    return [pid for pid, provider in _providers().items() if provider.is_configured()]


def resolve(selector: str) -> SearchProvider:
    """Turn a --search selector into a configured provider, or raise a
    RuntimeError that tells the operator what to set."""
    if selector != "auto":
        provider = get(selector)
        if not provider.is_configured():
            raise RuntimeError(
                f"search provider {selector!r} is not configured "
                f"(missing env / library); configured providers: {available()}"
            )
        return provider
    providers = _providers()
    for provider_id in AUTO_ORDER:
        provider = providers.get(provider_id)
        if provider is not None and provider.is_configured():
            return provider
    raise RuntimeError(
        "no search provider configured; set BRAVE_API_KEY, SEARXNG_BASE_URL, "
        "or FIRECRAWL_API_KEY, or install the ddgs library for DuckDuckGo"
    )
