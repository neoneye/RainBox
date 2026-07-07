import json

import pytest

import llm
from research import prompts
from research.caller import ModelCaller
from research.telemetry import (
    Telemetry,
    TelemetrySearchProvider,
    label_for,
    telemetry_fetcher,
)
from research.test_research_caller import MODEL_A, MODEL_B, FakeLLM, _patch_group
from research.websearch import SearchResult


def test_label_for_maps_prompts_and_defaults():
    assert label_for(prompts.PLANNER_SYSTEM) == "plan"
    assert label_for(prompts.NOTES_SYSTEM) == "notes"
    assert label_for("something else") == "other"


def test_telemetry_writes_jsonl_incrementally(tmp_path):
    path = tmp_path / "run.events.jsonl"
    telemetry = Telemetry(str(path))
    telemetry.record({"event": "run", "query": "q"})
    # visible before finish — a crashed run keeps its events
    assert len(path.read_text().splitlines()) == 1
    telemetry.finish(completed=True)
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert [row["event"] for row in lines] == ["run", "summary"]
    assert all("ts" in row for row in lines)
    assert lines[-1]["completed"] is True


def test_telemetry_without_path_is_in_memory_only():
    telemetry = Telemetry()
    telemetry.record({"event": "run"})
    telemetry.finish(completed=False)
    assert [row["event"] for row in telemetry.events] == ["run", "summary"]


def test_caller_records_fallback_attempts(monkeypatch):
    _patch_group(monkeypatch, [MODEL_A, MODEL_B])
    llms = {
        f"model-{MODEL_A}": FakeLLM(fail=True),
        f"model-{MODEL_B}": FakeLLM(reply="served"),
    }
    monkeypatch.setattr(llm, "prepare_llm", lambda p, m, a: llms[m])
    telemetry = Telemetry()
    caller = ModelCaller("research", telemetry=telemetry)
    assert caller.plain(prompts.PLANNER_SYSTEM, "user") == "served"
    (event,) = telemetry.events
    assert event["event"] == "llm_call"
    assert event["label"] == "plan"
    assert event["kind"] == "plain"
    assert event["served_by"] == str(MODEL_B)
    assert event["served_by_model"] == f"model-{MODEL_B}"
    assert [a["member"] for a in event["attempts"]] == [str(MODEL_A), str(MODEL_B)]
    assert [a["model"] for a in event["attempts"]] == [
        f"model-{MODEL_A}",
        f"model-{MODEL_B}",
    ]
    assert event["attempts"][0]["error"] == "model down"
    assert event["attempts"][1]["error"] is None


def test_caller_records_total_failure(monkeypatch):
    _patch_group(monkeypatch, [MODEL_A])
    monkeypatch.setattr(llm, "prepare_llm", lambda p, m, a: FakeLLM(fail=True))
    telemetry = Telemetry()
    caller = ModelCaller("research", telemetry=telemetry)
    with pytest.raises(RuntimeError):
        caller.plain(prompts.PLANNER_SYSTEM, "user")
    (event,) = telemetry.events
    assert event["served_by"] is None
    assert event["served_by_model"] is None
    assert event["attempts"][0]["member"] == str(MODEL_A)
    assert event["attempts"][0]["error"] == "model down"


class _OkProvider:
    id = "fake"

    def is_configured(self) -> bool:
        return True

    def search(self, query, count):
        return [SearchResult(url="https://x.example", title="t", snippet="s")]


class _BoomProvider:
    id = "boom"

    def is_configured(self) -> bool:
        return True

    def search(self, query, count):
        raise RuntimeError("rate limited")


def test_search_wrapper_records_success():
    telemetry = Telemetry()
    provider = TelemetrySearchProvider(_OkProvider(), telemetry)
    results = provider.search("tides", 5)
    assert len(results) == 1
    (event,) = telemetry.events
    assert event["event"] == "search"
    assert event["provider"] == "fake"
    assert event["results"] == 1
    assert event["error"] is None


def test_search_wrapper_records_error_and_reraises():
    telemetry = Telemetry()
    provider = TelemetrySearchProvider(_BoomProvider(), telemetry)
    with pytest.raises(RuntimeError, match="rate limited"):
        provider.search("tides", 5)
    (event,) = telemetry.events
    assert event["error"] == "rate limited"
    assert event["results"] == 0


def test_fetcher_wrapper_records_outcomes():
    telemetry = Telemetry()
    ok = telemetry_fetcher(lambda url, cap: "some text", telemetry)
    bad = telemetry_fetcher(lambda url, cap: None, telemetry)
    assert ok("https://a.example", 100) == "some text"
    assert bad("https://b.example", 100) is None
    first, second = telemetry.events
    assert first["ok"] is True and first["chars"] == 9
    assert second["ok"] is False and second["chars"] == 0


def test_summary_aggregates_per_model_label_provider():
    telemetry = Telemetry()
    telemetry.record(
        {
            "event": "llm_call",
            "label": "notes",
            "kind": "plain",
            "served_by": "fast",
            "ms": 500,
            "attempts": [
                {"member": "m-slow", "model": "slow", "ms": 400, "error": "timed out"},
                {"member": "m-fast", "model": "fast", "ms": 100, "error": None},
            ],
        }
    )
    telemetry.record(
        {
            "event": "llm_call",
            "label": "notes",
            "kind": "plain",
            "served_by": "fast",
            "ms": 90,
            "attempts": [
                {"member": "m-fast", "model": "fast", "ms": 90, "error": None}
            ],
        }
    )
    telemetry.record(
        {
            "event": "search",
            "provider": "ddg",
            "query": "q",
            "ms": 30,
            "results": 5,
            "error": None,
        }
    )
    telemetry.record(
        {
            "event": "search",
            "provider": "ddg",
            "query": "q2",
            "ms": 40,
            "results": 0,
            "error": "rate limited",
        }
    )
    telemetry.record({"event": "fetch", "url": "u", "ms": 10, "ok": True, "chars": 5})
    telemetry.record({"event": "subtask", "id": "S1", "title": "t", "failed": True})
    telemetry.finish(completed=True)
    summary = telemetry.events[-1]
    assert summary["llm"]["calls"] == 2
    assert summary["llm"]["failed_calls"] == 0
    assert summary["llm"]["models"]["m-slow"] == {
        "model": "slow",
        "attempts": 1,
        "served": 0,
        "errors": 1,
        "total_ms": 400,
        "last_error": "timed out",
    }
    assert summary["llm"]["models"]["m-fast"]["served"] == 2
    assert summary["llm"]["models"]["m-fast"]["model"] == "fast"
    assert summary["llm"]["labels"]["notes"] == {"calls": 2, "total_ms": 590}
    assert summary["search"]["ddg"] == {
        "queries": 2,
        "errors": 1,
        "results": 5,
        "total_ms": 70,
    }
    assert summary["fetch"] == {"attempted": 1, "ok": 1, "total_ms": 10}
    assert summary["subtasks"] == {"total": 1, "failed": 1}
