"""DuckDuckGo provider via the ddgs library. Keyless; configured iff the
library is importable."""

from __future__ import annotations

from research.websearch import SearchResult


class DdgSearch:
    id = "ddg"

    def is_configured(self) -> bool:
        try:
            import ddgs  # noqa: F401
        except ImportError:
            return False
        return True

    def search(self, query: str, count: int) -> list[SearchResult]:
        from ddgs import DDGS

        with DDGS() as client:
            rows = list(client.text(query, max_results=count) or [])
        return parse_rows(rows)


def parse_rows(rows: list[dict]) -> list[SearchResult]:
    results: list[SearchResult] = []
    for row in rows:
        url = row.get("href") or row.get("url")
        if not url:
            continue
        results.append(
            SearchResult(
                url=url,
                title=row.get("title") or url,
                snippet=row.get("body") or "",
            )
        )
    return results


PROVIDER = DdgSearch()
