"""The deep-research pipeline. `run_deep_research` is the public seam —
the CLI calls it today; chat/kanban/cron integrations call it later with a
custom progress_cb.

Setup failures (no search provider, unknown model group, missing fetcher
key) raise before any LLM or network work, so a misconfigured run dies in
milliseconds with an actionable message.

Pass a `telemetry` sink to get a JSONL KPI stream of the run — resolved
model configs, every LLM/search/fetch event, and a final summary row
(written even when the run aborts). See research/telemetry.py."""

from __future__ import annotations

import os
import sys
from dataclasses import asdict
from typing import Callable

from research import fetch, websearch
from research.caller import ModelCaller
from research.config import ResearchConfig
from research.planner import generate_plan
from research.report import Report, sweep_questions
from research.researcher import (
    Fetcher,
    SourceRegistry,
    recover_subtask_from_corpus,
    research_subtask,
)
from research.scope import resolve_scope, scope_block, scope_markdown
from research.splitter import split_plan
from research.synthesizer import synthesize
from research.telemetry import Telemetry, TelemetrySearchProvider, telemetry_fetcher

ProgressCb = Callable[[str, str], None]


def _default_progress(stage: str, detail: str) -> None:
    print(f"[{stage}] {detail}", file=sys.stderr)


def _resolve_fetcher(fetcher_id: str) -> Fetcher:
    if fetcher_id == "plain":
        return fetch.fetch_extract
    if fetcher_id == "firecrawl":
        if not os.environ.get("FIRECRAWL_API_KEY"):
            raise RuntimeError("fetcher 'firecrawl' needs FIRECRAWL_API_KEY")
        return fetch.fetch_extract_firecrawl
    raise RuntimeError(f"unknown fetcher {fetcher_id!r}; known: plain, firecrawl")


def run_deep_research(
    query: str,
    config: ResearchConfig | None = None,
    progress_cb: ProgressCb | None = None,
    telemetry: Telemetry | None = None,
) -> Report:
    cfg = config or ResearchConfig()
    progress = progress_cb or _default_progress
    tel = telemetry or Telemetry()

    completed = False
    try:
        provider = websearch.resolve(cfg.search_provider)
        fetcher = _resolve_fetcher(cfg.fetcher)
        caller = ModelCaller(
            cfg.model_group, timeout_s=cfg.llm_timeout_s, telemetry=tel
        )
        tel.record(
            {
                "event": "run",
                "query": query,
                "config": asdict(cfg),
                "search_provider": provider.id,
                "models": caller.describe_models(),
            }
        )
        provider = TelemetrySearchProvider(provider, tel)
        fetcher = telemetry_fetcher(fetcher, tel)
        progress(
            "setup",
            f"search={provider.id} fetcher={cfg.fetcher} model_group={cfg.model_group}",
        )

        progress("scope", "disambiguating the query")
        scope = resolve_scope(caller, query)
        block = scope_block(scope)
        progress("scope", scope.chosen_scope)
        tel.record(
            {
                "event": "scope",
                "chosen": scope.chosen_scope,
                "meanings": scope.meanings,
                "excluded": scope.excluded,
            }
        )

        progress("plan", "generating research plan")
        plan = generate_plan(caller, query, block)
        progress("split", "splitting plan into subtasks")
        subtasks = split_plan(caller, plan, cfg.max_subtasks)
        progress("split", f"{len(subtasks)} subtasks")

        registry = SourceRegistry()
        results = []
        for subtask in subtasks:
            results.append(
                research_subtask(
                    caller, provider, fetcher, registry, subtask, cfg, progress,
                    scope_block=block,
                )
            )

        # Failed subtasks get a second chance against the run's own corpus:
        # their answer may sit in a page another subtask fetched.
        recovered_ids: set[str] = set()
        for index, result in enumerate(results):
            if not result.failed:
                continue
            retry = recover_subtask_from_corpus(
                caller, registry, subtasks[index], cfg, progress,
                scope_block=block,
            )
            if retry is not None:
                results[index] = retry
                recovered_ids.add(retry.subtask_id)
        for subtask, result in zip(subtasks, results):
            tel.record(
                {
                    "event": "subtask",
                    "id": subtask.id,
                    "title": subtask.title,
                    "failed": result.failed,
                    "recovered": subtask.id in recovered_ids,
                }
            )

        summary, open_questions = synthesize(
            caller, query, results, progress, scope_block=block
        )

        # A question is never a finding — move stray interrogative lines
        # (a small model echoing its instructions as prose) to Open questions.
        swept: list[str] = []
        for result in results:
            if result.failed:
                continue
            cleaned, questions = sweep_questions(result.findings_markdown)
            result.findings_markdown = cleaned
            swept += questions
        summary, questions = sweep_questions(summary)
        swept += questions
        if swept:
            progress("sweep", f"moved {len(swept)} stray question(s) to Open questions")
            bullets = "\n".join(f"- {q}" for q in swept)
            open_questions = f"{open_questions}\n{bullets}".strip()
        report = Report(
            query=query,
            summary_markdown=summary,
            subtask_results=results,
            open_questions_markdown=open_questions,
            sources=registry.all(),
            scope_markdown=scope_markdown(scope),
        )
        completed = True
        return report
    finally:
        tel.finish(completed=completed)
