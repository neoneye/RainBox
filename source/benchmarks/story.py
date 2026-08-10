"""Conversation benchmarks: write a horror story one section at a time, over
ten turns, and see whether the model holds the thread.

Four variants across two axes — free text vs structured output, tools vs no
tools:

  - BenchmarkStoryText:       text,       no tools
  - BenchmarkStoryStruct:     structured, no tools
  - BenchmarkStoryTextTool:   text,       one tool
  - BenchmarkStoryStructTool: structured, one tool

**Why a conversation.** Every other benchmark here is one-shot, which gives a
prompt cache nothing to hold. These resend the system prompt and the whole
history each turn and append one new user message, so turn *n*'s prompt is a
strict prefix extension of turn *n-1*'s. That is the shape a KV cache is
built for, and the shape most of rainbox actually produces — the assistant's
ReAct loop, chat, the kanban workers. Running these is therefore both a
capability test and the workload that puts a real number on /activity's
reusable-prefix metric.

**Why the tool check is a number.** "Did it write a good section" is not
checkable. "Did the integer the tool returned appear in the prose" is, and it
distinguishes a model that consumed the tool result from one that called the
tool and ignored it.

CLI demo:
    python3 -m benchmarks.story <uuid>                  # text
    python3 -m benchmarks.story <uuid> --struct         # structured output
    python3 -m benchmarks.story <uuid> --text-tool      # text + tool
    python3 -m benchmarks.story <uuid> --struct-tool    # structured + tool
"""

import asyncio
import random
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from uuid import UUID

from llama_index.core.agent.workflow import FunctionAgent
from llama_index.core.instrumentation.dispatcher import instrument_tags
from llama_index.core.llms import ChatMessage, MessageRole
from pydantic import BaseModel, Field

from benchmarks.basic import (
    BenchmarkResult,
    TIMEOUT_ABORT_THRESHOLD,
    _resolve_target,
    _target_kind,
)
from llm import prepare_llm

# Ten (user, assistant) rounds per trial. Long enough that the history
# dominates the prompt — which is what makes the cache measurable — and that a
# small model has to work to stay coherent.
STORY_TURNS: int = 10

# "Around 200 words". The band is wide because the benchmark is not a style
# judge: it only fails a one-line dismissal or a runaway wall of text.
TARGET_WORDS: int = 200
MIN_WORDS: int = 100
MAX_WORDS: int = 350

# One turn's budget. A ten-turn trial can therefore take a few minutes on a
# slow local model, which is expected.
TURN_TIMEOUT: float = 180.0

# The assembled story is held in the runner's state dict and shipped to the
# browser on every poll, so a model that ignores the word limit can't grow it
# without bound.
MAX_STORY_CHARS: int = 40_000


SYSTEM_PROMPT_TEXT: str = (
    "You are a horror novelist writing a story one section at a time.\n\n"
    "Each time the user asks for the next section, reply with that section "
    f"and nothing else: about {TARGET_WORDS} words of prose, continuing "
    "directly from what you have already written. Keep the characters, "
    "setting, and dread consistent across sections, and let the story build.\n\n"
    "Do not number the sections, do not add headings, do not summarize what "
    "came before, and do not comment on the writing. Reply with the prose "
    "only."
)

SYSTEM_PROMPT_STRUCT: str = (
    "You are two people at once: a horror novelist writing a story one "
    "section at a time, and a book reviewer who despises the novelist's "
    "work.\n\n"
    "Each time the user asks for the next section, respond with a single "
    "JSON object with exactly these two fields:\n"
    f"  - `section_text` (string): about {TARGET_WORDS} words of prose, "
    "continuing directly from the previous section. Keep characters, setting "
    "and dread consistent, and let the story build. Prose only — no heading, "
    "no numbering, no commentary.\n"
    "  - `section_reviewer` (string): a brutally harsh critique of that exact "
    "section, in the voice of a book reviewer who finds it derivative and "
    "overwrought. Be specific about what fails and be merciless.\n\n"
    "Output the JSON object and nothing else — no prose outside it, no "
    "markdown fences, no extra fields."
)

_TOOL_RULE: str = (
    "\n\nBefore writing each section you MUST call the `omen_number` tool "
    "exactly once. It returns an integer. That integer is an omen, and the "
    "digits you get back MUST appear literally in the section you then write "
    "— as a room number, a year, a count of something, a number carved into "
    "a wall, whatever fits. Write the digits, not words: if the tool returns "
    "4242, the section must contain 4242."
)

SYSTEM_PROMPT_TEXT_TOOL: str = SYSTEM_PROMPT_TEXT + _TOOL_RULE
SYSTEM_PROMPT_STRUCT_TOOL: str = SYSTEM_PROMPT_STRUCT + _TOOL_RULE


def _first_user_message() -> str:
    return (
        "Begin the story. Write section 1: introduce the place and the person "
        "who should not have come here."
    )


def _next_user_message(turn: int) -> str:
    return (
        f"Write section {turn + 1}, continuing directly from the last one. "
        "Raise the dread."
    )


class StorySection(BaseModel):
    """One section of the story, plus its own worst review."""

    section_text: str = Field(
        description=f"About {TARGET_WORDS} words of horror prose continuing the story."
    )
    section_reviewer: str = Field(
        description="A brutally harsh book-reviewer critique of this section."
    )


@dataclass
class SectionOutcome:
    """What one turn produced, before any judgement is passed on it."""

    text: str
    reviewer: str | None = None
    tool_number: int | None = None
    tool_called: bool = False


@dataclass
class StoryTrial:
    trial_index: int
    sections: list[SectionOutcome]
    story: str
    turns_completed: int
    word_counts: list[int]
    correct: bool
    reason: str | None  # why it wasn't correct, when it wasn't
    elapsed: float
    error: str | None


def count_words(text: str) -> int:
    return len(text.split())


def tool_number_present(text: str, number: int) -> bool:
    """Whether `number` appears in `text` as a number in its own right.

    Bounded on both sides so 14242 doesn't satisfy a demand for 4242 — without
    that, a model could pass by emitting any long number that happens to
    contain the digits. A thousands separator is accepted because models write
    4,242 as readily as 4242 and it is the same number.
    """
    plain = str(number)
    grouped = f"{number:,}"
    for form in {plain, grouped}:
        if re.search(rf"(?<![\d,]){re.escape(form)}(?![\d,]*\d)", text):
            return True
    return False


def score_sections(
    sections: list[SectionOutcome],
    require_reviewer: bool = False,
    require_tool: bool = False,
) -> str | None:
    """Why this trial is not correct, or None if it is.

    A string rather than a bool so the page can say what went wrong: "section
    4 was 12 words" is actionable where a red cell is not.
    """
    if len(sections) != STORY_TURNS:
        return f"only {len(sections)} of {STORY_TURNS} sections were written"
    for i, s in enumerate(sections, start=1):
        words = count_words(s.text)
        if words < MIN_WORDS or words > MAX_WORDS:
            return (
                f"section {i} was {words} words, outside {MIN_WORDS}–{MAX_WORDS}"
            )
        if require_reviewer and not (s.reviewer or "").strip():
            return f"section {i} had no reviewer critique"
        if require_tool:
            if not s.tool_called or s.tool_number is None:
                return f"section {i} did not call the omen tool"
            if not tool_number_present(s.text, s.tool_number):
                return (
                    f"section {i} omitted its omen number {s.tool_number}"
                )
    return None


def assemble_story(sections: list[SectionOutcome]) -> str:
    """The story as markdown, for the page's copy-to-clipboard button."""
    if not sections:
        return "_(no sections were written)_"
    parts: list[str] = []
    for i, s in enumerate(sections, start=1):
        heading = f"## Section {i}"
        if s.tool_number is not None:
            heading += f"  ·  omen {s.tool_number}"
        parts.append(heading)
        parts.append(s.text.strip())
        if (s.reviewer or "").strip():
            # Blockquoted so the critique reads as commentary on the section
            # above rather than as more of the story.
            quoted = "\n".join(f"> {line}" for line in s.reviewer.strip().splitlines())
            parts.append(quoted)
    out = "\n\n".join(parts)
    if len(out) > MAX_STORY_CHARS:
        out = out[:MAX_STORY_CHARS] + "\n\n_(truncated)_"
    return out


def _omen_tool() -> tuple[Callable[[], int], dict[str, Any]]:
    """A tool returning a random integer, plus the record of what it returned.

    The record is a dict rather than a closure variable so the caller can read
    it after the agent run and see both whether the tool fired and what number
    the model was given.
    """
    seen: dict[str, Any] = {"called": False, "number": None}

    def omen_number() -> int:
        """Returns the omen number that must appear in the next section."""
        seen["called"] = True
        seen["number"] = random.randint(1000, 9999)
        return seen["number"]

    return omen_number, seen


def _run_agent_turn(agent: FunctionAgent, user_msg: str, history: list[ChatMessage]):
    """One agent turn, bounded by TURN_TIMEOUT. Returns the AgentOutput."""

    async def _go():
        return await asyncio.wait_for(
            agent.run(user_msg=user_msg, chat_history=list(history)),
            timeout=TURN_TIMEOUT,
        )

    return asyncio.run(_go())


class _StoryBenchmarkBase:
    """Shared ten-turn conversation driver.

    Subclasses say how one turn is taken and what the trial must satisfy;
    everything else — history threading, timing, scoring, the abort-on-repeated-
    timeouts rule — lives here.
    """

    name: str = "story"
    require_reviewer: bool = False
    require_tool: bool = False

    def __init__(self, target_uuid: UUID, num_trials: int = 3):
        self.target_uuid = target_uuid
        self.num_trials = num_trials

    # --- subclass hooks ---

    def _system_prompt(self) -> str:
        raise NotImplementedError

    def _take_turn(
        self, ctx: Any, history: list[ChatMessage], user_msg: str
    ) -> SectionOutcome:
        """Produce one section, given the conversation so far."""
        raise NotImplementedError

    def _make_context(self, provider_id: str, model_name: str, args: dict) -> Any:
        """Whatever the turns need — an LLM, or an agent factory."""
        raise NotImplementedError

    # --- the driver ---

    def run(
        self,
        on_trial: Callable[[StoryTrial], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> BenchmarkResult:
        provider_id, model_name, args = _resolve_target(self.target_uuid)

        trials: list[StoryTrial] = []
        timeouts = 0
        aborted = False
        abort_reason: str | None = None

        for i in range(self.num_trials):
            if should_stop is not None and should_stop():
                aborted = True
                abort_reason = "stopped by user"
                break

            t0 = time.monotonic()
            sections: list[SectionOutcome] = []
            error: str | None = None
            timed_out = False
            try:
                ctx = self._make_context(provider_id, model_name, args)
                # The history the model sees. Each turn appends the user ask
                # and the assistant's reply, so the next turn's prompt extends
                # this one rather than replacing it — the property the cache
                # depends on, and the reason these benchmarks exist.
                history: list[ChatMessage] = []
                for turn in range(STORY_TURNS):
                    if should_stop is not None and should_stop():
                        break
                    user_msg = (
                        _first_user_message() if turn == 0 else _next_user_message(turn)
                    )
                    # Attribute the call on /activity. Benchmarks build their
                    # LLM directly rather than through the agent base class, so
                    # without this every one of them lands as "unknown" —
                    # visible as volume, indistinguishable from anything else
                    # the box was doing at the time.
                    with instrument_tags({"caller": f"benchmark.{self.name}"}):
                        outcome = self._take_turn(ctx, history, user_msg)
                    sections.append(outcome)
                    history.append(
                        ChatMessage(role=MessageRole.USER, content=user_msg)
                    )
                    history.append(
                        ChatMessage(
                            role=MessageRole.ASSISTANT, content=outcome.text
                        )
                    )
            except (asyncio.TimeoutError, TimeoutError):
                timed_out = True
                error = f"turn timed out after {TURN_TIMEOUT:g}s"
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
            elapsed = time.monotonic() - t0

            reason = (
                None
                if error is not None
                else score_sections(
                    sections,
                    require_reviewer=self.require_reviewer,
                    require_tool=self.require_tool,
                )
            )
            trial = StoryTrial(
                trial_index=i,
                sections=sections,
                story=assemble_story(sections),
                turns_completed=len(sections),
                word_counts=[count_words(s.text) for s in sections],
                correct=error is None and reason is None,
                reason=reason,
                elapsed=elapsed,
                error=error,
            )
            trials.append(trial)
            if on_trial is not None:
                on_trial(trial)

            if timed_out:
                timeouts += 1
                if timeouts >= TIMEOUT_ABORT_THRESHOLD:
                    aborted = True
                    abort_reason = (
                        f"{timeouts} trials timed out after {TURN_TIMEOUT:g}s; "
                        f"aborted with {self.num_trials - len(trials)} trial(s) unrun"
                    )
                    break

        return BenchmarkResult(
            target_kind=_target_kind(self.target_uuid),
            target_uuid=self.target_uuid,
            model_name=model_name,
            total=len(trials),
            correct=sum(1 for t in trials if t.correct),
            mistakes=sum(1 for t in trials if t.error is None and not t.correct),
            failures=sum(1 for t in trials if t.error is not None),
            trials=list(trials),
            aborted=aborted,
            abort_reason=abort_reason,
        )


class BenchmarkStoryText(_StoryBenchmarkBase):
    """Ten turns of free-text horror. The baseline: no schema, no tools, just
    whether the model can hold a story together across a growing history."""

    name = "story_text"

    def _system_prompt(self) -> str:
        return SYSTEM_PROMPT_TEXT

    def _make_context(self, provider_id: str, model_name: str, args: dict) -> Any:
        return prepare_llm(provider_id, model_name, args)

    def _take_turn(self, ctx, history, user_msg) -> SectionOutcome:
        messages = [
            ChatMessage(role=MessageRole.SYSTEM, content=self._system_prompt()),
            *history,
            ChatMessage(role=MessageRole.USER, content=user_msg),
        ]
        response = ctx.chat(messages)
        return SectionOutcome(text=(response.message.content or "").strip())


class BenchmarkStoryStruct(_StoryBenchmarkBase):
    """Ten turns of structured output: prose plus a hostile review of it.

    The two fields are deliberately different registers, so a model can't
    satisfy the schema by putting the same paragraph in both."""

    name = "story_struct"
    require_reviewer = True

    def _system_prompt(self) -> str:
        return SYSTEM_PROMPT_STRUCT

    def _make_context(self, provider_id: str, model_name: str, args: dict) -> Any:
        return prepare_llm(provider_id, model_name, args).as_structured_llm(
            StorySection
        )

    def _take_turn(self, ctx, history, user_msg) -> SectionOutcome:
        messages = [
            ChatMessage(role=MessageRole.SYSTEM, content=self._system_prompt()),
            *history,
            ChatMessage(role=MessageRole.USER, content=user_msg),
        ]
        response = ctx.chat(messages)
        parsed = response.raw
        if not isinstance(parsed, StorySection):
            parsed = StorySection.model_validate_json(response.message.content or "{}")
        return SectionOutcome(
            text=parsed.section_text.strip(),
            reviewer=parsed.section_reviewer.strip(),
        )


class BenchmarkStoryTextTool(_StoryBenchmarkBase):
    """Ten turns of free text, each gated on a tool call.

    Correct only if the integer `omen_number` returned appears in the section
    the model then wrote — which a model that calls the tool and ignores the
    result cannot fake. Requires a function-calling target."""

    name = "story_text_tool"
    require_tool = True

    def _system_prompt(self) -> str:
        return SYSTEM_PROMPT_TEXT_TOOL

    def _make_context(self, provider_id: str, model_name: str, args: dict) -> Any:
        return (provider_id, model_name, args)

    def _build_agent(self, ctx, tool, output_cls=None) -> FunctionAgent:
        provider_id, model_name, args = ctx
        kwargs: dict[str, Any] = {
            "tools": [tool],
            "llm": prepare_llm(provider_id, model_name, args),
            "system_prompt": self._system_prompt(),
        }
        if output_cls is not None:
            kwargs["output_cls"] = output_cls
        return FunctionAgent(**kwargs)

    def _take_turn(self, ctx, history, user_msg) -> SectionOutcome:
        tool, seen = _omen_tool()
        agent = self._build_agent(ctx, tool)
        result = _run_agent_turn(agent, user_msg, history)
        return SectionOutcome(
            text=str(result).strip(),
            tool_number=seen["number"],
            tool_called=bool(seen["called"]),
        )


class BenchmarkStoryStructTool(BenchmarkStoryTextTool):
    """The crossover: structured output AND function calling in one turn.

    The hardest of the four for a local model — it must route a tool call and
    come back with a valid two-field object that also carries the tool's
    number."""

    name = "story_struct_tool"
    require_reviewer = True
    require_tool = True

    def _system_prompt(self) -> str:
        return SYSTEM_PROMPT_STRUCT_TOOL

    def _take_turn(self, ctx, history, user_msg) -> SectionOutcome:
        tool, seen = _omen_tool()
        agent = self._build_agent(ctx, tool, output_cls=StorySection)
        result = _run_agent_turn(agent, user_msg, history)
        parsed = getattr(result, "structured_response", None)
        if isinstance(parsed, dict):
            section = StorySection.model_validate(parsed)
        elif isinstance(parsed, StorySection):
            section = parsed
        else:
            section = StorySection.model_validate_json(str(result) or "{}")
        return SectionOutcome(
            text=section.section_text.strip(),
            reviewer=section.section_reviewer.strip(),
            tool_number=seen["number"],
            tool_called=bool(seen["called"]),
        )


_CLI_BENCHMARKS: dict[str, type[_StoryBenchmarkBase]] = {
    "--text": BenchmarkStoryText,
    "--struct": BenchmarkStoryStruct,
    "--text-tool": BenchmarkStoryTextTool,
    "--struct-tool": BenchmarkStoryStructTool,
}


def main() -> None:
    import db

    app = db.make_app()
    with app.app_context():
        args = sys.argv[1:]
        target = UUID(args[0])
        flag = next((a for a in args[1:] if a in _CLI_BENCHMARKS), "--text")
        bench = _CLI_BENCHMARKS[flag](target, num_trials=1)

        def on_trial(t: StoryTrial) -> None:
            verdict = "ok" if t.correct else (t.error or t.reason)
            print(f"trial {t.trial_index}: {verdict}  ({t.elapsed:.1f}s)")
            print(t.story)

        result = bench.run(on_trial=on_trial)
        print(
            f"\n{result.model_name}: {result.correct} correct, "
            f"{result.mistakes} mistakes, {result.failures} failures"
        )


if __name__ == "__main__":
    main()
