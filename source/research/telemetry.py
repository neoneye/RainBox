"""Run telemetry: a JSONL event stream for assessing model/provider KPIs.

One JSON object per line, written incrementally (a crashed run keeps every
event up to the crash): the first row describes the run — config plus every
group member's resolved model settings — then `llm_call` / `search` /
`fetch` / `subtask` events as they happen, and a final `summary` row with
per-model, per-label, and per-provider aggregates. That layout answers the
operator questions directly: which member timed out (llm_call.attempts),
which search API is flaky (search.error), and whether a faster model serves
as well as a slower one (summary.llm.models).

A `Telemetry()` with no path is an in-memory no-op sink, so the pipeline
never branches on "is telemetry enabled"."""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any, Callable

from research import prompts
from research.websearch import SearchProvider, SearchResult

# llm_call events are labeled by which system prompt drove the call, so the
# Caller protocol needs no label plumbing through the stages.
PROMPT_LABELS = {
    prompts.PLANNER_SYSTEM: "plan",
    prompts.SPLITTER_SYSTEM: "split",
    prompts.QUERYGEN_SYSTEM: "queries",
    prompts.SELECT_SYSTEM: "select",
    prompts.NOTES_SYSTEM: "notes",
    prompts.FINDINGS_SYSTEM: "findings",
    prompts.SYNTH_SUMMARY_SYSTEM: "summary",
    prompts.SYNTH_OPENQ_SYSTEM: "open_questions",
}


def label_for(system_prompt: str) -> str:
    return PROMPT_LABELS.get(system_prompt, "other")


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


class Telemetry:
    """Collects events; with a path, also appends each one as a JSON line
    and flushes immediately."""

    def __init__(self, path: str | None = None) -> None:
        self.path = path
        self.events: list[dict[str, Any]] = []
        self._handle = open(path, "w", encoding="utf-8") if path else None
        self._t0 = time.monotonic()

    def record(self, event: dict[str, Any]) -> None:
        event = {"ts": datetime.now().isoformat(timespec="seconds"), **event}
        self.events.append(event)
        if self._handle is not None:
            self._handle.write(
                json.dumps(event, ensure_ascii=False, default=str) + "\n"
            )
            self._handle.flush()

    def finish(self, completed: bool) -> None:
        """Append the summary row and close the file. Idempotent-safe only
        in the sense that a second call appends a second summary — the
        pipeline calls it exactly once, in its finally block."""
        self.record({"event": "summary", "completed": completed, **self._summary()})
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def _summary(self) -> dict[str, Any]:
        models: dict[str, dict[str, Any]] = {}
        labels: dict[str, dict[str, Any]] = {}
        llm_calls = 0
        llm_failed = 0
        search: dict[str, dict[str, Any]] = {}
        fetch: dict[str, Any] = {"attempted": 0, "ok": 0, "total_ms": 0}
        subtasks = {"total": 0, "failed": 0}
        for event in self.events:
            kind = event.get("event")
            if kind == "llm_call":
                llm_calls += 1
                if event["served_by"] is None:
                    llm_failed += 1
                label_row = labels.setdefault(
                    event["label"], {"calls": 0, "total_ms": 0}
                )
                label_row["calls"] += 1
                label_row["total_ms"] += event["ms"]
                for attempt in event["attempts"]:
                    # Keyed by group-member uuid, not model name — the same
                    # model can be in the group twice with different
                    # overrides. Join against the run row for the settings.
                    model_row = models.setdefault(
                        attempt["member"],
                        {
                            "model": attempt["model"],
                            "attempts": 0,
                            "served": 0,
                            "errors": 0,
                            "total_ms": 0,
                            "last_error": None,
                        },
                    )
                    model_row["attempts"] += 1
                    model_row["total_ms"] += attempt["ms"]
                    if attempt["error"] is None:
                        model_row["served"] += 1
                    else:
                        model_row["errors"] += 1
                        model_row["last_error"] = attempt["error"]
            elif kind == "search":
                provider_row = search.setdefault(
                    event["provider"],
                    {"queries": 0, "errors": 0, "results": 0, "total_ms": 0},
                )
                provider_row["queries"] += 1
                provider_row["total_ms"] += event["ms"]
                if event["error"] is None:
                    provider_row["results"] += event["results"]
                else:
                    provider_row["errors"] += 1
            elif kind == "fetch":
                fetch["attempted"] += 1
                fetch["total_ms"] += event["ms"]
                if event["ok"]:
                    fetch["ok"] += 1
            elif kind == "subtask":
                subtasks["total"] += 1
                if event["failed"]:
                    subtasks["failed"] += 1
        return {
            "wall_ms": _ms(self._t0),
            "llm": {
                "calls": llm_calls,
                "failed_calls": llm_failed,
                "models": models,
                "labels": labels,
            },
            "search": search,
            "fetch": fetch,
            "subtasks": subtasks,
        }


class TelemetrySearchProvider:
    """Wraps a SearchProvider: one `search` event per query — provider id,
    latency, result count or error — then re-raises errors unchanged so the
    researcher's degradation logic is untouched."""

    def __init__(self, inner: SearchProvider, telemetry: Telemetry) -> None:
        self._inner = inner
        self._telemetry = telemetry
        self.id = inner.id

    def is_configured(self) -> bool:
        return self._inner.is_configured()

    def search(self, query: str, count: int) -> list[SearchResult]:
        t0 = time.monotonic()
        try:
            results = self._inner.search(query, count)
        except Exception as exc:
            self._telemetry.record(
                {
                    "event": "search",
                    "provider": self.id,
                    "query": query,
                    "ms": _ms(t0),
                    "results": 0,
                    "error": str(exc),
                }
            )
            raise
        self._telemetry.record(
            {
                "event": "search",
                "provider": self.id,
                "query": query,
                "ms": _ms(t0),
                "results": len(results),
                "error": None,
            }
        )
        return results


def telemetry_fetcher(
    inner: Callable[[str, int], str | None], telemetry: Telemetry
) -> Callable[[str, int], str | None]:
    """Wrap a fetcher: one `fetch` event per URL with latency and outcome."""

    def fetch(url: str, char_cap: int) -> str | None:
        t0 = time.monotonic()
        text = inner(url, char_cap)
        telemetry.record(
            {
                "event": "fetch",
                "url": url,
                "ms": _ms(t0),
                "ok": text is not None,
                "chars": len(text) if text else 0,
            }
        )
        return text

    return fetch
