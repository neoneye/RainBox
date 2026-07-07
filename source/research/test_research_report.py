from research.report import Report, Source, SubtaskResult


def _report() -> Report:
    return Report(
        query="how do tides work?\nplease be thorough",
        summary_markdown="Tides are driven by the moon [1].",
        subtask_results=[
            SubtaskResult(
                subtask_id="S1",
                title="Gravitational mechanism",
                findings_markdown="The moon pulls the ocean [1][2].",
            ),
            SubtaskResult(
                subtask_id="S2",
                title="Regional variation",
                findings_markdown="",
                failed=True,
                failure_note="no search results",
            ),
        ],
        open_questions_markdown="- How do tides interact with storms?",
        sources=[
            Source(id=1, url="https://example.org/tides", title="Tides 101"),
            Source(id=2, url="https://example.org/moon", title="Moon facts"),
            Source(id=3, url="https://example.org/unused", title="Never cited"),
        ],
    )


def test_render_headings_and_sections():
    markdown = _report().render_markdown()
    assert markdown.startswith("# how do tides work? please be thorough\n")
    assert "## Summary" in markdown
    assert "## Gravitational mechanism" in markdown
    assert "The moon pulls the ocean [1][2]." in markdown
    assert "## Open questions" in markdown
    assert "## References" in markdown


def test_failed_subtask_has_no_section_but_is_noted():
    markdown = _report().render_markdown()
    assert "## Regional variation" not in markdown
    assert (
        '- Subtask "Regional variation" could not be researched: '
        "no search results" in markdown
    )


def test_references_list_only_cited_sources_in_id_order():
    markdown = _report().render_markdown()
    refs = markdown.split("## References")[1]
    assert "[1] Tides 101 — https://example.org/tides" in refs
    assert "[2] Moon facts — https://example.org/moon" in refs
    assert "Never cited" not in refs
    assert refs.index("[1]") < refs.index("[2]")


def test_citation_regex_ignores_unknown_ids():
    report = _report()
    report.summary_markdown = "See [1] and the bogus [99]."
    refs = report.render_markdown().split("## References")[1]
    assert "[99]" not in refs


def test_scope_section_rendered_when_present():
    report = _report()
    report.scope_markdown = "The display standard.\n\nOut of scope: the connector."
    markdown = report.render_markdown()
    assert "## Scope" in markdown
    assert "Out of scope: the connector." in markdown
    assert markdown.index("## Scope") < markdown.index("## Summary")


def test_no_scope_section_by_default():
    assert "## Scope" not in _report().render_markdown()
