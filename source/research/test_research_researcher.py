from research import prompts
from research.config import ResearchConfig
from research.report import SubtaskResult
from research.researcher import (
    SearchQueryList,
    SourceRegistry,
    UrlSelection,
    normalize_url,
    research_subtask,
)
from research.splitter import Subtask
from research.test_research_stages import FakeCaller
from research.websearch import SearchResult


def _noop_progress(stage: str, detail: str) -> None:
    pass


SUBTASK = Subtask(id="S1", title="Tidal mechanism", description="how the moon drives tides")


class FakeSearchProvider:
    id = "fake"

    def __init__(self, results_by_query):
        self.results_by_query = results_by_query
        self.queries = []

    def is_configured(self) -> bool:
        return True

    def search(self, query, count):
        self.queries.append(query)
        outcome = self.results_by_query.get(query, [])
        if isinstance(outcome, Exception):
            raise outcome
        return outcome[:count]


def _result(url, title="t"):
    return SearchResult(url=url, title=title, snippet="snippet")


def test_normalize_url():
    assert normalize_url("HTTPS://Example.ORG/Path?q=1#frag") == (
        "https://example.org/Path?q=1"
    )


def test_source_registry_dedupes_by_normalized_url():
    registry = SourceRegistry()
    first = registry.add("https://example.org/a#x", "A")
    second = registry.add("HTTPS://EXAMPLE.ORG/a", "A again")
    assert first.id == 1 and second.id == 1
    assert registry.id_for("https://example.org/a") == 1
    assert len(registry.all()) == 1


def _caller(queries, notes_and_findings):
    return FakeCaller(
        structured={prompts.QUERYGEN_SYSTEM: [SearchQueryList(queries=queries)]},
        plain=notes_and_findings,
    )


def test_happy_path_produces_findings_with_notes():
    caller = _caller(
        ["moon tides"],
        {
            prompts.NOTES_SYSTEM: ["note about gravity"],
            prompts.FINDINGS_SYSTEM: ["Gravity drives tides [1]."],
        },
    )
    provider = FakeSearchProvider({"moon tides": [_result("https://example.org/a", "A")]})
    registry = SourceRegistry()
    result = research_subtask(
        caller,
        provider,
        lambda url, cap: "page text",
        registry,
        SUBTASK,
        ResearchConfig(),
        _noop_progress,
    )
    assert result == SubtaskResult(
        subtask_id="S1", title="Tidal mechanism", findings_markdown="Gravity drives tides [1]."
    )
    assert registry.notes == {1: "note about gravity"}
    notes_call = next(c for c in caller.calls if c[0] == prompts.NOTES_SYSTEM)
    assert "BEGIN UNTRUSTED SOURCE [1] https://example.org/a" in notes_call[1]
    assert "SUBTASK: Tidal mechanism" in notes_call[1]


def test_no_search_results_marks_subtask_failed():
    caller = _caller(["moon tides"], {})
    provider = FakeSearchProvider({"moon tides": []})
    result = research_subtask(
        caller, provider, lambda u, c: "x", SourceRegistry(), SUBTASK,
        ResearchConfig(), _noop_progress,
    )
    assert result.failed and "no search results" in result.failure_note


def test_provider_error_on_one_query_continues():
    caller = _caller(
        ["boom", "ok"],
        {
            prompts.NOTES_SYSTEM: ["note"],
            prompts.FINDINGS_SYSTEM: ["findings [1]."],
        },
    )
    provider = FakeSearchProvider(
        {"boom": RuntimeError("rate limited"), "ok": [_result("https://example.org/a")]}
    )
    result = research_subtask(
        caller, provider, lambda u, c: "text", SourceRegistry(), SUBTASK,
        ResearchConfig(), _noop_progress,
    )
    assert not result.failed


def test_all_fetches_fail_marks_subtask_failed():
    caller = _caller(["q"], {})
    provider = FakeSearchProvider({"q": [_result("https://example.org/a")]})
    result = research_subtask(
        caller, provider, lambda u, c: None, SourceRegistry(), SUBTASK,
        ResearchConfig(), _noop_progress,
    )
    assert result.failed and "fetched" in result.failure_note


def test_no_relevant_content_notes_are_dropped():
    caller = _caller(
        ["q"],
        {prompts.NOTES_SYSTEM: [prompts.NO_RELEVANT_CONTENT]},
    )
    provider = FakeSearchProvider({"q": [_result("https://example.org/a")]})
    registry = SourceRegistry()
    result = research_subtask(
        caller, provider, lambda u, c: "text", registry, SUBTASK,
        ResearchConfig(), _noop_progress,
    )
    assert result.failed
    assert registry.notes == {}


def test_selection_used_when_more_results_than_fetch_budget():
    config = ResearchConfig(fetch_per_subtask=2)
    results = [_result(f"https://example.org/{i}", f"T{i}") for i in range(5)]
    caller = FakeCaller(
        structured={
            prompts.QUERYGEN_SYSTEM: [SearchQueryList(queries=["q"])],
            # indices out of range (7) and duplicated (1) must be ignored
            prompts.SELECT_SYSTEM: [UrlSelection(indices=[3, 7, 1, 1, 0])],
        },
        plain={
            prompts.NOTES_SYSTEM: ["n3", "n1"],
            prompts.FINDINGS_SYSTEM: ["f [1][2]."],
        },
    )
    provider = FakeSearchProvider({"q": results})
    fetched = []

    def fetcher(url, cap):
        fetched.append(url)
        return "text"

    research_subtask(
        caller, provider, fetcher, SourceRegistry(), SUBTASK, config, _noop_progress
    )
    assert fetched == ["https://example.org/3", "https://example.org/1"]


def test_already_noted_url_is_reused_not_refetched():
    registry = SourceRegistry()
    source = registry.add("https://example.org/a", "A")
    registry.notes[source.id] = "existing note"
    caller = FakeCaller(
        structured={prompts.QUERYGEN_SYSTEM: [SearchQueryList(queries=["q"])]},
        plain={prompts.FINDINGS_SYSTEM: ["reused [1]."]},
    )
    provider = FakeSearchProvider({"q": [_result("https://example.org/a")]})

    def fetcher(url, cap):
        raise AssertionError("must not refetch a noted url")

    result = research_subtask(
        caller, provider, fetcher, registry, SUBTASK, ResearchConfig(), _noop_progress
    )
    assert not result.failed
    findings_call = next(c for c in caller.calls if c[0] == prompts.FINDINGS_SYSTEM)
    assert "NOTES FOR SOURCE [1]" in findings_call[1]
    assert "existing note" in findings_call[1]


def test_empty_generated_queries_fall_back_to_title():
    caller = FakeCaller(
        structured={prompts.QUERYGEN_SYSTEM: [SearchQueryList(queries=["  "])]},
        plain={
            prompts.NOTES_SYSTEM: ["note"],
            prompts.FINDINGS_SYSTEM: ["f [1]."],
        },
    )
    provider = FakeSearchProvider(
        {"Tidal mechanism": [_result("https://example.org/a")]}
    )
    result = research_subtask(
        caller, provider, lambda u, c: "text", SourceRegistry(), SUBTASK,
        ResearchConfig(), _noop_progress,
    )
    assert provider.queries == ["Tidal mechanism"]
    assert not result.failed
