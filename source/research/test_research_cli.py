from research import pipeline
from research.__main__ import main
from research.report import Report


def _report():
    return Report(
        query="q",
        summary_markdown="s",
        subtask_results=[],
        open_questions_markdown="o",
        sources=[],
    )


def test_cli_prints_report_to_stdout(monkeypatch, capsys):
    captured = {}

    def fake_run(query, config, progress_cb=None):
        captured["query"] = query
        captured["config"] = config
        return _report()

    monkeypatch.setattr(pipeline, "run_deep_research", fake_run)
    assert main(["how do tides work?", "--search", "ddg", "--max-subtasks", "2"]) == 0
    assert captured["query"] == "how do tides work?"
    assert captured["config"].search_provider == "ddg"
    assert captured["config"].max_subtasks == 2
    assert "## Summary" in capsys.readouterr().out


def test_cli_writes_out_file(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        pipeline, "run_deep_research", lambda q, c, progress_cb=None: _report()
    )
    out = tmp_path / "report.md"
    assert main(["q", "--out", str(out)]) == 0
    assert "## Summary" in out.read_text()
    assert "report written to" in capsys.readouterr().err


def test_cli_runtime_error_exits_1(monkeypatch, capsys):
    def boom(q, c, progress_cb=None):
        raise RuntimeError("no search provider configured")

    monkeypatch.setattr(pipeline, "run_deep_research", boom)
    assert main(["q"]) == 1
    assert "no search provider configured" in capsys.readouterr().err
