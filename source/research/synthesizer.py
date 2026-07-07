"""Stage 4: findings sections -> executive summary + open questions.

Two plain calls instead of one (small local models do better on one job at
a time). The findings themselves are never re-generated — pipeline assembly
includes them verbatim, so synthesis can't lose or distort detail."""

from __future__ import annotations

from typing import Callable

from research import prompts
from research.caller import Caller
from research.report import SubtaskResult

SYNTH_INPUT_CHAR_CAP = 24000


def synthesize(
    caller: Caller,
    query: str,
    subtask_results: list[SubtaskResult],
    progress: Callable[[str, str], None],
) -> tuple[str, str]:
    body = _findings_body(subtask_results)
    if len(body) > SYNTH_INPUT_CHAR_CAP:
        progress("synthesize", "findings exceed budget; using first paragraphs only")
        body = _findings_body(subtask_results, first_paragraph_only=True)
        body = body[:SYNTH_INPUT_CHAR_CAP]
    user_prompt = f"RESEARCH QUERY:\n{query}\n\nFINDINGS:\n{body}"
    progress("synthesize", "writing executive summary")
    summary = caller.plain(prompts.SYNTH_SUMMARY_SYSTEM, user_prompt).strip()
    progress("synthesize", "listing open questions")
    open_questions = caller.plain(prompts.SYNTH_OPENQ_SYSTEM, user_prompt).strip()
    return summary, open_questions


def _findings_body(
    subtask_results: list[SubtaskResult], first_paragraph_only: bool = False
) -> str:
    sections: list[str] = []
    for result in subtask_results:
        if result.failed:
            continue
        text = result.findings_markdown
        if first_paragraph_only:
            text = text.split("\n\n", 1)[0]
        sections.append(f"## {result.title}\n{text}")
    return "\n\n".join(sections)
