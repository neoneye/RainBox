"""Firecrawl search provider (direct REST, not MCP). Needs FIRECRAWL_API_KEY."""

from __future__ import annotations

import os

import requests

from research.websearch import SearchResult

API_URL = "https://api.firecrawl.dev/v2/search"


class FirecrawlSearch:
    id = "firecrawl"

    def is_configured(self) -> bool:
        return bool(os.environ.get("FIRECRAWL_API_KEY"))

    def search(self, query: str, count: int) -> list[SearchResult]:
        response = requests.post(
            API_URL,
            json={"query": query, "limit": count},
            headers={
                "Authorization": f"Bearer {os.environ['FIRECRAWL_API_KEY']}"
            },
            timeout=30,
        )
        response.raise_for_status()
        return parse_response(response.json())


def parse_response(payload: dict) -> list[SearchResult]:
    # v2 returns {"data": {"web": [...]}}; older shapes return {"data": [...]}.
    data = payload.get("data")
    rows = data.get("web") if isinstance(data, dict) else data
    results: list[SearchResult] = []
    for row in rows or []:
        url = row.get("url")
        if not url:
            continue
        results.append(
            SearchResult(
                url=url,
                title=row.get("title") or url,
                snippet=row.get("description") or "",
            )
        )
    return results


PROVIDER = FirecrawlSearch()
