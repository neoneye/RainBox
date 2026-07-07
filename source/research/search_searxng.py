"""SearXNG provider (self-hosted metasearch). Needs SEARXNG_BASE_URL; the
instance must allow the JSON format (searxng settings: formats: [html, json])."""

from __future__ import annotations

import os

import requests

from research.websearch import SearchResult


class SearxngSearch:
    id = "searxng"

    def is_configured(self) -> bool:
        return bool(os.environ.get("SEARXNG_BASE_URL"))

    def search(self, query: str, count: int) -> list[SearchResult]:
        base = os.environ["SEARXNG_BASE_URL"].rstrip("/")
        response = requests.get(
            f"{base}/search",
            params={"q": query, "format": "json"},
            timeout=20,
        )
        response.raise_for_status()
        return parse_response(response.json())[:count]


def parse_response(payload: dict) -> list[SearchResult]:
    results: list[SearchResult] = []
    for row in payload.get("results") or []:
        url = row.get("url")
        if not url:
            continue
        results.append(
            SearchResult(
                url=url,
                title=row.get("title") or url,
                snippet=row.get("content") or "",
            )
        )
    return results


PROVIDER = SearxngSearch()
