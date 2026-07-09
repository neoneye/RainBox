"""Stage: findings sections -> executive summary + open questions.

Two plain calls instead of one (small local models do better on one job at
a time). The findings themselves are never re-generated — pipeline assembly
includes them verbatim, so synthesis can't lose or distort detail.

Synthesis input degrades instead of aborting: a findings body that fits the
char cap can still overflow a small model's context window (empty replies,
timeouts), and one oversized prompt must not kill a whole run. Each rung of
SYNTH_BODY_STEPS shrinks the body — full text first, then first paragraphs
only at decreasing caps — and only when every rung fails does the model
group's error propagate."""

from __future__ import annotations

from typing import Callable

from research import prompts
from research.caller import Caller
from research.report import SubtaskResult

SYNTH_INPUT_CHAR_CAP = 24000
# (first_paragraph_only, char cap) — tried in order until the model answers.
SYNTH_BODY_STEPS = (
    (False, SYNTH_INPUT_CHAR_CAP),
    (True, 12000),
    (True, 6000),
)


def synthesize(
    caller: Caller,
    query: str,
    subtask_results: list[SubtaskResult],
    progress: Callable[[str, str], None],
    scope_block: str = "",
) -> tuple[str, str]:
    scope_part = f"{scope_block}\n\n" if scope_block else ""
    last_error: Exception | None = None
    for step, (first_paragraph_only, cap) in enumerate(SYNTH_BODY_STEPS):
        body = _findings_body(
            subtask_results, first_paragraph_only=first_paragraph_only
        )[:cap]
        user_prompt = f"RESEARCH QUERY:\n{query}\n\n{scope_part}FINDINGS:\n{body}"
        if step > 0:
            progress(
                "synthesize",
                f"retrying with smaller findings input ({cap} chars, "
                "first paragraphs only)",
            )
        try:
            progress("synthesize", "writing executive summary")
            summary = caller.plain(prompts.SYNTH_SUMMARY_SYSTEM, user_prompt).strip()
            progress("synthesize", "listing open questions")
            open_questions = caller.plain(
                prompts.SYNTH_OPENQ_SYSTEM, user_prompt
            ).strip()
            return summary, open_questions
        except RuntimeError as exc:
            last_error = exc
    raise last_error if last_error else RuntimeError("synthesis produced nothing")


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
