"""Knobs for a research run. Defaults are sized for small local models —
tight source caps keep every LLM call inside a modest context window."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ResearchConfig:
    model_group: str = "research"
    search_provider: str = "auto"  # "auto" | "brave" | "ddg" | "searxng" | "firecrawl"
    fetcher: str = "plain"  # "plain" | "firecrawl"
    max_subtasks: int = 5
    queries_per_subtask: int = 3
    results_per_query: int = 5
    fetch_per_subtask: int = 4
    per_source_char_cap: int = 8000
    # Per-model timeout floor in seconds: a member's configured timeout below
    # this is raised to it (research calls run longer than chat calls).
    llm_timeout_s: float = 120.0
