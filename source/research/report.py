"""Report dataclasses and markdown rendering.

Rendering is pure Python: findings sections are included verbatim (synthesis
can't lose detail), failed subtasks become Open-questions bullets, and the
References section lists exactly the sources whose [n] ids appear in the
rendered prose."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_CITATION_RE = re.compile(r"\[(\d+)\]")
_TRAILING_CITES_RE = re.compile(r"(\s*\[\d+\])+\s*$")


def sweep_questions(markdown: str) -> tuple[str, list[str]]:
    """Remove lines that are questions — small models sometimes emit a
    subtask's guiding question as prose ("How many students are enrolled?
    [10]"). A question is never a finding; the pipeline moves swept lines to
    Open questions, where they are honest. Returns (cleaned, questions)."""
    kept: list[str] = []
    questions: list[str] = []
    for line in markdown.splitlines():
        core = line.strip().lstrip("-*").lstrip("#").strip()
        core = _TRAILING_CITES_RE.sub("", core).strip()
        if core.endswith("?"):
            questions.append(core)
        else:
            kept.append(line)
    return "\n".join(kept).strip(), questions


@dataclass
class Source:
    id: int  # global, run-wide citation id (1-based)
    url: str
    title: str
    tier: str = ""  # quality tier from the verifier ("" until classified)


@dataclass
class SubtaskResult:
    subtask_id: str
    title: str
    findings_markdown: str
    failed: bool = False
    failure_note: str = ""


@dataclass
class Report:
    query: str
    summary_markdown: str
    subtask_results: list[SubtaskResult]
    open_questions_markdown: str
    sources: list[Source] = field(default_factory=list)
    # What the query was taken to mean (and what was excluded) — shown to the
    # reader so an ambiguous term can't silently pick its interpretation.
    scope_markdown: str = ""
    # Deterministic caveat when the run's sources skew low-tier — the reader
    # should know they are getting commentary synthesis, not literature.
    quality_note: str = ""

    def render_markdown(self) -> str:
        title = " ".join(self.query.split())
        parts: list[str] = [f"# {title}", ""]
        if self.scope_markdown.strip():
            parts += ["## Scope", "", self.scope_markdown.strip(), ""]
        if self.quality_note.strip():
            parts += [f"*{self.quality_note.strip()}*", ""]
        parts += ["## Summary", "", self.summary_markdown.strip(), ""]
        for result in self.subtask_results:
            if result.failed:
                continue
            parts += [f"## {result.title}", "", result.findings_markdown.strip(), ""]
        parts += ["## Open questions", "", self.open_questions_markdown.strip()]
        failures = [r for r in self.subtask_results if r.failed]
        for result in failures:
            parts.append(
                f'- Subtask "{result.title}" could not be researched: {result.failure_note}'
            )
        parts += ["", "## References", ""]
        prose = "\n".join(parts)
        known = {source.id: source for source in self.sources}
        cited = sorted(
            {int(m) for m in _CITATION_RE.findall(prose)} & set(known)
        )
        for source_id in cited:
            source = known[source_id]
            suffix = f" ({source.tier})" if source.tier else ""
            parts.append(f"[{source_id}] {source.title} — {source.url}{suffix}")
        return "\n".join(parts) + "\n"
