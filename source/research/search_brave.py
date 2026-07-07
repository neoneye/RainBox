"""Brave Search API provider. Needs BRAVE_API_KEY."""

from __future__ import annotations

import os

import requests

from research.websearch import SearchResult

API_URL = "https://api.search.brave.com/res/v1/web/search"


class BraveSearch:
    id = "brave"

    def is_configured(self) -> bool:
        return bool(os.environ.get("BRAVE_API_KEY"))

    def search(self, query: str, count: int) -> list[SearchResult]:
        response = requests.get(
            API_URL,
            params={"q": query, "count": count},
            headers={
                "X-Subscription-Token": os.environ["BRAVE_API_KEY"],
                "Accept": "application/json",
            },
            timeout=20,
        )
        response.raise_for_status()
        return parse_response(response.json())


def parse_response(payload: dict) -> list[SearchResult]:
    rows = (payload.get("web") or {}).get("results") or []
    results: list[SearchResult] = []
    for row in rows:
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


PROVIDER = BraveSearch()
