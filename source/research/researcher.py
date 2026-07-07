"""Stage 3: one subtask -> a cited findings section.

Bounded, deterministic loop (no free tool-calling): generate queries ->
search -> select which results to read -> fetch -> per-source notes ->
findings. The model only ever picks list indices; Python maps them back to
URLs, so a hallucinated or injected URL can never reach the fetcher.

Sources get run-wide ids via SourceRegistry so [n] citations are unambiguous
across the whole report, and a URL fetched for an earlier subtask is not
refetched — its notes are reused."""

from __future__ import annotations

import logging
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, Field

from research import prompts
from research.caller import Caller
from research.config import ResearchConfig
from research.report import Source, SubtaskResult
from research.splitter import Subtask
from research.websearch import SearchProvider, SearchResult

logger = logging.getLogger(__name__)

Fetcher = Callable[[str, int], str | None]
Progress = Callable[[str, str], None]


class SearchQueryList(BaseModel):
    queries: list[str] = Field(description="2 to 4 short web search queries.")


class UrlSelection(BaseModel):
    indices: list[int] = Field(
        description="Indices of the search results worth reading, best first."
    )


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    return urlunsplit(
        (parts.scheme.lower(), (parts.netloc or "").lower(), parts.path, parts.query, "")
    )


class SourceRegistry:
    """Run-wide source ids and per-source notes."""

    def __init__(self) -> None:
        self._sources: list[Source] = []
        self._id_by_url: dict[str, int] = {}
        self.notes: dict[int, str] = {}

    def add(self, url: str, title: str) -> Source:
        key = normalize_url(url)
        existing = self._id_by_url.get(key)
        if existing is not None:
            return self._sources[existing - 1]
        source = Source(id=len(self._sources) + 1, url=url, title=title)
        self._sources.append(source)
        self._id_by_url[key] = source.id
        return source

    def id_for(self, url: str) -> int | None:
        return self._id_by_url.get(normalize_url(url))

    def all(self) -> list[Source]:
        return list(self._sources)


def research_subtask(
    caller: Caller,
    provider: SearchProvider,
    fetcher: Fetcher,
    registry: SourceRegistry,
    subtask: Subtask,
    config: ResearchConfig,
    progress: Progress,
) -> SubtaskResult:
    progress("research", f"{subtask.id} {subtask.title}")
    queries = _generate_queries(caller, subtask, config)
    results = _run_searches(provider, queries, config, progress)
    if not results:
        return SubtaskResult(
            subtask_id=subtask.id,
            title=subtask.title,
            findings_markdown="",
            failed=True,
            failure_note="no search results",
        )

    reused_ids: list[int] = []
    fresh: list[SearchResult] = []
    for result in results:
        source_id = registry.id_for(result.url)
        if source_id is not None and source_id in registry.notes:
            reused_ids.append(source_id)
        else:
            fresh.append(result)

    fetched_ids: list[int] = []
    for result in _select_results(caller, subtask, fresh, config):
        text = fetcher(result.url, config.per_source_char_cap)
        if text is None:
            progress("fetch", f"skipped {result.url}")
            continue
        source = registry.add(result.url, result.title)
        block = prompts.wrap_source_block(source.id, source.url, text)
        notes = caller.plain(
            prompts.NOTES_SYSTEM, f"{_subtask_block(subtask)}\n\n{block}"
        ).strip()
        if not notes or notes == prompts.NO_RELEVANT_CONTENT:
            progress("notes", f"no relevant content in {result.url}")
            continue
        registry.notes[source.id] = notes
        fetched_ids.append(source.id)

    note_ids = list(dict.fromkeys(reused_ids + fetched_ids))
    if not note_ids:
        return SubtaskResult(
            subtask_id=subtask.id,
            title=subtask.title,
            findings_markdown="",
            failed=True,
            failure_note="no source could be fetched or none was relevant",
        )

    notes_blocks = "\n\n".join(
        f"NOTES FOR SOURCE [{source_id}]:\n{registry.notes[source_id]}"
        for source_id in note_ids
    )
    findings = caller.plain(
        prompts.FINDINGS_SYSTEM, f"{_subtask_block(subtask)}\n\n{notes_blocks}"
    ).strip()
    return SubtaskResult(
        subtask_id=subtask.id, title=subtask.title, findings_markdown=findings
    )


def _subtask_block(subtask: Subtask) -> str:
    return f"SUBTASK: {subtask.title}\nINSTRUCTIONS: {subtask.description}"


def _generate_queries(
    caller: Caller, subtask: Subtask, config: ResearchConfig
) -> list[str]:
    result = caller.structured(
        prompts.QUERYGEN_SYSTEM, _subtask_block(subtask), SearchQueryList
    )
    assert isinstance(result, SearchQueryList)
    queries = [query.strip() for query in result.queries if query.strip()]
    return (queries or [subtask.title])[: config.queries_per_subtask]


def _run_searches(
    provider: SearchProvider,
    queries: list[str],
    config: ResearchConfig,
    progress: Progress,
) -> list[SearchResult]:
    results: list[SearchResult] = []
    seen: set[str] = set()
    for query in queries:
        try:
            rows = provider.search(query, config.results_per_query)
        except Exception as exc:
            progress("search", f"query {query!r} failed: {exc}")
            continue
        for row in rows:
            key = normalize_url(row.url)
            if key in seen:
                continue
            seen.add(key)
            results.append(row)
    return results


def _select_results(
    caller: Caller,
    subtask: Subtask,
    fresh: list[SearchResult],
    config: ResearchConfig,
) -> list[SearchResult]:
    if len(fresh) <= config.fetch_per_subtask:
        return list(fresh)
    listing = "\n".join(
        f"[{i}] {result.title}\n    {result.url}\n    {result.snippet}"
        for i, result in enumerate(fresh)
    )
    selection = caller.structured(
        prompts.SELECT_SYSTEM,
        f"{_subtask_block(subtask)}\n\nSEARCH RESULTS:\n{listing}",
        UrlSelection,
    )
    assert isinstance(selection, UrlSelection)
    picked: list[SearchResult] = []
    seen: set[int] = set()
    for index in selection.indices:
        if 0 <= index < len(fresh) and index not in seen:
            seen.add(index)
            picked.append(fresh[index])
        if len(picked) == config.fetch_per_subtask:
            break
    return picked or list(fresh)[: config.fetch_per_subtask]
