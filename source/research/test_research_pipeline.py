import json

import pytest

from research import pipeline, prompts
from research.config import ResearchConfig
from research.report import SubtaskResult
from research.researcher import SearchQueryList
from research.scope import ScopeModel
from research.splitter import SubtaskListModel, SubtaskModel
from research.synthesizer import SYNTH_INPUT_CHAR_CAP, synthesize
from research.telemetry import Telemetry
from research.test_research_researcher import FakeSearchProvider, _result
from research.test_research_stages import FakeCaller


def _noop_progress(stage, detail):
    pass


def _ok(subtask_id, title, findings):
    return SubtaskResult(subtask_id=subtask_id, title=title, findings_markdown=findings)


def test_synthesize_returns_summary_and_open_questions():
    caller = FakeCaller(
        plain={
            prompts.SYNTH_SUMMARY_SYSTEM: ["the summary [1]"],
            prompts.SYNTH_OPENQ_SYSTEM: ["- open q"],
        }
    )
    summary, open_questions = synthesize(
        caller, "query", [_ok("S1", "T", "findings [1]")], _noop_progress
    )
    assert summary == "the summary [1]"
    assert open_questions == "- open q"
    user_prompt = caller.calls[0][1]
    assert "RESEARCH QUERY:\nquery" in user_prompt
    assert "findings [1]" in user_prompt


def test_synthesize_truncates_oversized_findings():
    huge = "first paragraph.\n\n" + ("x" * SYNTH_INPUT_CHAR_CAP)
    caller = FakeCaller(
        plain={
            prompts.SYNTH_SUMMARY_SYSTEM: ["s"],
            prompts.SYNTH_OPENQ_SYSTEM: ["o"],
        }
    )
    synthesize(caller, "q", [_ok("S1", "T", huge)], _noop_progress)
    user_prompt = caller.calls[0][1]
    assert len(user_prompt) < SYNTH_INPUT_CHAR_CAP + 1000
    assert "first paragraph." in user_prompt


def test_resolve_fetcher_unknown_raises():
    with pytest.raises(RuntimeError, match="unknown fetcher"):
        pipeline._resolve_fetcher("teleport")


def test_resolve_fetcher_firecrawl_needs_key(monkeypatch):
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="FIRECRAWL_API_KEY"):
        pipeline._resolve_fetcher("firecrawl")


def _e2e_env(monkeypatch):
    subtasks = SubtaskListModel(
        subtasks=[
            SubtaskModel(title="Mechanism", description="how"),
            SubtaskModel(title="History", description="when"),
        ]
    )
    caller = FakeCaller(
        structured={
            prompts.SCOPE_SYSTEM: [
                ScopeModel(
                    meanings=["ocean tides"],
                    chosen_scope="Ocean tides on Earth.",
                    excluded=["metaphorical tides"],
                )
            ],
            prompts.SPLITTER_SYSTEM: [subtasks],
            prompts.QUERYGEN_SYSTEM: [
                SearchQueryList(queries=["mech q"]),
                SearchQueryList(queries=["hist q"]),
            ],
        },
        plain={
            prompts.PLANNER_SYSTEM: ["the plan"],
            prompts.NOTES_SYSTEM: ["mech note", "hist note"],
            prompts.FINDINGS_SYSTEM: ["mech findings [1]", "hist findings [2]"],
            prompts.SYNTH_SUMMARY_SYSTEM: ["summary [1][2]"],
            prompts.SYNTH_OPENQ_SYSTEM: ["- what else?"],
        },
    )
    provider = FakeSearchProvider(
        {
            "mech q": [_result("https://example.org/m", "M")],
            "hist q": [_result("https://example.org/h", "H")],
        }
    )
    monkeypatch.setattr(pipeline, "ModelCaller", lambda group, **kwargs: caller)
    monkeypatch.setattr(pipeline.websearch, "resolve", lambda selector: provider)
    monkeypatch.setattr(
        pipeline, "_resolve_fetcher", lambda fetcher_id: (lambda url, cap: "text")
    )


def test_run_deep_research_end_to_end(monkeypatch):
    _e2e_env(monkeypatch)
    events = []
    report = pipeline.run_deep_research(
        "how do tides work?",
        ResearchConfig(),
        progress_cb=lambda stage, detail: events.append(stage),
    )
    markdown = report.render_markdown()
    assert "# how do tides work?" in markdown
    assert "## Scope" in markdown
    assert "Ocean tides on Earth." in markdown
    assert "Out of scope: metaphorical tides." in markdown
    assert "summary [1][2]" in markdown
    assert "mech findings [1]" in markdown
    assert "hist findings [2]" in markdown
    assert "[1] M — https://example.org/m" in markdown
    assert "[2] H — https://example.org/h" in markdown
    assert "plan" in events and "research" in events and "synthesize" in events


def test_run_deep_research_writes_telemetry(monkeypatch, tmp_path):
    _e2e_env(monkeypatch)
    path = tmp_path / "run.events.jsonl"
    pipeline.run_deep_research(
        "how do tides work?",
        ResearchConfig(),
        progress_cb=lambda stage, detail: None,
        telemetry=Telemetry(str(path)),
    )
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows[0]["event"] == "run"
    assert rows[0]["query"] == "how do tides work?"
    assert rows[0]["config"]["max_subtasks"] == 5
    assert rows[0]["models"] == []  # FakeCaller describes no members
    assert rows[-1]["event"] == "summary"
    assert rows[-1]["completed"] is True
    kinds = [row["event"] for row in rows]
    assert kinds.count("scope") == 1
    scope_row = next(row for row in rows if row["event"] == "scope")
    assert scope_row["chosen"] == "Ocean tides on Earth."
    assert kinds.count("search") == 2
    assert kinds.count("fetch") == 2
    assert kinds.count("subtask") == 2
    assert rows[-1]["subtasks"] == {"total": 2, "failed": 0}
    assert rows[-1]["search"]["fake"]["queries"] == 2


def test_telemetry_summary_written_even_on_abort(monkeypatch, tmp_path):
    monkeypatch.setattr(
        pipeline.websearch,
        "resolve",
        lambda selector: (_ for _ in ()).throw(RuntimeError("no provider")),
    )
    path = tmp_path / "run.events.jsonl"
    with pytest.raises(RuntimeError, match="no provider"):
        pipeline.run_deep_research(
            "q", ResearchConfig(), progress_cb=lambda s, d: None,
            telemetry=Telemetry(str(path)),
        )
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows[-1]["event"] == "summary"
    assert rows[-1]["completed"] is False
