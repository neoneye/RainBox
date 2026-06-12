"""Kanban operation benchmarks — the roadmap's "first slice" from
docs/kanban-design.md.

The decision they inform: which board serialization do the target local
models read ids out of reliably (markdown vs JSON), and which invocation
mechanism yields fewer bad operations (structured output vs function
calling)? The result picks DEFAULTS for the first LLM kanban worker; it is
deliberately small and decision-oriented, not the full benchmark suite.

Each trial builds a synthetic board (uuids, columns, tasks, agents),
serializes it with the PRODUCTION renderers (db_kanban.kanban_render_markdown
/ kanban_render_llm_json — the benchmark measures the real contract, not a
copy), and gives the model the board plus one natural-language instruction:
move / claim / complete(ok) / complete(failed) / append a note. Correct iff
the model names the right operation AND copies the right uuids exactly —
wrong-card and malformed-id errors are precisely the blast-radius failures
the board exists to prevent.

Two benchmark classes × two context formats = the 2×2 decision matrix,
registered in benchmark_runner.KANBAN_BENCHMARK_SPECS and runnable from
/benchmark_kanban:

  - BenchmarkKanbanOpStructured(context_format='markdown'|'json') —
    structured output via as_structured_llm(KanbanOpResponse).
  - BenchmarkKanbanOpTools(context_format='markdown'|'json') —
    function calling via a LlamaIndex FunctionAgent with one no-op tool per
    operation; correct iff exactly one call to the right tool with the right
    ids (requires a function-calling target).

CLI demo:
    python3 benchmark_kanban.py <uuid>                    # structured, markdown
    python3 benchmark_kanban.py <uuid> --json             # structured, JSON
    python3 benchmark_kanban.py <uuid> --tools            # tools, markdown
    python3 benchmark_kanban.py <uuid> --tools --json     # tools, JSON
"""

import json
import random
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Literal
from uuid import UUID, uuid4

from llama_index.core.agent.workflow import FunctionAgent
from llama_index.core.llms import ChatMessage, MessageRole
from pydantic import BaseModel, Field

from benchmark import (
    TIMEOUT_ABORT_THRESHOLD,
    TOOL_CALL_TIMEOUT,
    BenchmarkResult,
    _resolve_target,
    _run_agent,
    _target_kind,
)
from db_kanban import kanban_render_llm_json, kanban_render_markdown
from llm import prepare_llm

# ---- synthetic board generation ----

_VERBS = ["Write", "Review", "Deploy", "Refactor", "Test", "Document",
          "Benchmark", "Translate", "Archive", "Merge"]
_NOUNS = ["report", "homepage", "backup", "parser", "newsletter", "schema",
          "dashboard", "pipeline", "changelog", "prototype"]
_AGENT_NAMES = ["agent_red", "agent_blue", "agent_green"]


def make_board(rng: random.Random) -> tuple[dict[str, Any], dict[str, str]]:
    """A synthetic load_board-shaped payload + its agent uuid->name map.
    Six tasks with unique titles across To do / In progress / Done; every
    agent is assigned somewhere, so its agentId is readable off the
    serialization (a claim instruction must be answerable from the board)."""
    columns = [{"uuid": str(uuid4()), "name": name}
               for name in ("To do", "In progress", "Done")]
    agent_names = {str(uuid4()): name for name in _AGENT_NAMES}
    agent_uuids = list(agent_names)
    titles = [f"{v} the {n}" for v, n in
              zip(rng.sample(_VERBS, 6), rng.sample(_NOUNS, 6))]
    # Column spread: 3 in To do, 2 in In progress, 1 in Done.
    spread = [0, 0, 0, 1, 1, 2]
    tasks = []
    for i, title in enumerate(titles):
        # Tasks 0..2 are assigned (one per agent), the rest unassigned.
        agent = agent_uuids[i] if i < len(agent_uuids) else None
        tasks.append({
            "uuid": str(uuid4()),
            "columnUuid": columns[spread[i]]["uuid"],
            "title": title,
            "description": f"Take care of the {title.split()[-1]}." if i % 2 == 0 else "",
            "agentUuid": agent,
            "claimedBy": None,
            "claimExpiresAt": None,
        })
    rng.shuffle(tasks)
    data = {
        "uuid": str(uuid4()),
        "name": "Sprint board",
        "description": "Synthetic benchmark board.",
        "columns": columns,
        "tasks": tasks,
    }
    return data, agent_names


_OPS = ("move", "claim", "complete_ok", "complete_failed", "append_event")


def make_instruction(
    rng: random.Random, data: dict[str, Any], agent_names: dict[str, str],
    kind: str,
) -> tuple[str, dict[str, Any]]:
    """One natural-language instruction + the expected operation dict. The
    expected dict's keys are exactly what the grader checks per kind."""
    done_col = data["columns"][-1]["uuid"]
    candidates = [t for t in data["tasks"] if t["columnUuid"] != done_col]
    task = rng.choice(candidates)
    title = task["title"]
    if kind == "move":
        target = rng.choice([c for c in data["columns"]
                             if c["uuid"] != task["columnUuid"]])
        return (f'Move the task titled "{title}" to the "{target["name"]}" column.',
                {"op": "move", "taskId": task["uuid"], "columnId": target["uuid"]})
    if kind == "claim":
        agent_uuid = rng.choice(list(agent_names))
        return (f'Claim the task titled "{title}" for the agent '
                f'@{agent_names[agent_uuid]}.',
                {"op": "claim", "taskId": task["uuid"], "agentId": agent_uuid})
    if kind == "complete_ok":
        return (f'Mark the task titled "{title}" as done.',
                {"op": "complete", "taskId": task["uuid"], "ok": True})
    if kind == "complete_failed":
        return (f'Report the task titled "{title}" as failed.',
                {"op": "complete", "taskId": task["uuid"], "ok": False})
    if kind == "append_event":
        return (f'Add a progress note to the task titled "{title}".',
                {"op": "append_event", "taskId": task["uuid"]})
    raise ValueError(f"unknown instruction kind: {kind}")


def serialize_board(
    data: dict[str, Any], agent_names: dict[str, str], context_format: str
) -> str:
    if context_format == "markdown":
        return kanban_render_markdown(data, agent_names)
    if context_format == "json":
        return json.dumps(kanban_render_llm_json(data, agent_names),
                          indent=2, ensure_ascii=False)
    raise ValueError(f"unknown context_format: {context_format}")


def grade(expected: dict[str, Any], got: dict[str, Any] | None) -> bool:
    """Correct iff the operation and every expected field match exactly.
    Fields outside the expected dict (e.g. a null columnId on a claim) are
    ignored — the decision question is op choice + id fidelity, not whether
    a small model zeroes the irrelevant fields."""
    if got is None:
        return False
    return all(got.get(key) == value for key, value in expected.items())


@dataclass
class KanbanOpTrial:
    trial_index: int
    context_format: str
    instruction: str
    expected: dict[str, Any]
    got: dict[str, Any] | None
    correct: bool
    elapsed: float
    error: str | None


# ---- structured-output variant ----

class KanbanOpResponse(BaseModel):
    op: Literal["move", "claim", "complete", "append_event"] = Field(
        description="The operation that carries out the instruction.")
    taskId: str = Field(
        description="The uuid of the task the instruction refers to, copied "
                    "exactly from the board.")
    columnId: str | None = Field(
        default=None,
        description='For "move": the uuid of the destination column. '
                    "Otherwise null.")
    agentId: str | None = Field(
        default=None,
        description='For "claim": the uuid of the claiming agent. '
                    "Otherwise null.")
    ok: bool | None = Field(
        default=None,
        description='For "complete": true when the task is done, false when '
                    "it failed. Otherwise null.")


KANBAN_STRUCTURED_SYSTEM_PROMPT: str = (
    "You operate a kanban board. The user message contains the current board, "
    "followed by one instruction. Carry out the instruction by responding "
    "with a single JSON object that strictly adheres to the "
    "`KanbanOpResponse` schema:\n"
    '  - `op` (string): one of "move", "claim", "complete", "append_event".\n'
    "  - `taskId` (string): the uuid of the task the instruction refers to.\n"
    '  - `columnId` (string or null): for "move", the destination column '
    "uuid; otherwise null.\n"
    '  - `agentId` (string or null): for "claim", the claiming agent uuid; '
    "otherwise null.\n"
    '  - `ok` (boolean or null): for "complete", true = done, false = '
    "failed; otherwise null.\n\n"
    "Rules:\n"
    "  - Copy every uuid EXACTLY as it appears in the board.\n"
    "  - Output the JSON object and nothing else — no prose, no markdown "
    "fences, no explanation."
)


class BenchmarkKanbanOpStructured:
    """Board context (markdown or JSON) + one instruction → one structured
    KanbanOpResponse. Correct iff op and the expected ids match exactly."""

    def __init__(
        self, target_uuid: UUID, num_trials: int = 5,
        context_format: str = "markdown",
    ):
        self.target_uuid = target_uuid
        self.num_trials = num_trials
        self.context_format = context_format

    def run(
        self,
        on_trial: Callable[[KanbanOpTrial], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> BenchmarkResult:
        _provider_id, model_name, args = _resolve_target(self.target_uuid)
        the_llm = prepare_llm(_provider_id, model_name, args)
        sllm = the_llm.as_structured_llm(KanbanOpResponse)
        rng = random.Random()

        trials: list[KanbanOpTrial] = []
        aborted = False
        abort_reason: str | None = None
        for i in range(self.num_trials):
            if should_stop is not None and should_stop():
                aborted = True
                abort_reason = "stopped by user"
                break
            data, agent_names = make_board(rng)
            kind = _OPS[i % len(_OPS)]
            instruction, expected = make_instruction(rng, data, agent_names, kind)
            board_text = serialize_board(data, agent_names, self.context_format)
            messages = [
                ChatMessage(role=MessageRole.SYSTEM,
                            content=KANBAN_STRUCTURED_SYSTEM_PROMPT),
                ChatMessage(role=MessageRole.USER,
                            content=f"{board_text}\nInstruction: {instruction}"),
            ]
            t0 = time.monotonic()
            got: dict[str, Any] | None = None
            error: str | None = None
            try:
                response = sllm.chat(messages)
                got = response.raw.model_dump()
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
            elapsed = time.monotonic() - t0

            trial = KanbanOpTrial(
                trial_index=i,
                context_format=self.context_format,
                instruction=instruction,
                expected=expected,
                got=got,
                correct=error is None and grade(expected, got),
                elapsed=elapsed,
                error=error,
            )
            trials.append(trial)
            if on_trial is not None:
                on_trial(trial)

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


# ---- function-calling variant ----

KANBAN_TOOLS_SYSTEM_PROMPT: str = (
    "You operate a kanban board via tools. The user message contains the "
    "current board, followed by one instruction. Carry out the instruction "
    "by making exactly ONE tool call:\n"
    "  - move_task(taskId, columnId) — move a task to another column.\n"
    "  - claim_task(taskId, agentId) — claim a task for an agent.\n"
    "  - complete_task(taskId, ok) — report a task done (ok=true) or failed "
    "(ok=false).\n"
    "  - append_event(taskId, note) — add a progress note to a task.\n\n"
    "Copy every uuid EXACTLY as it appears in the board. After the tool call "
    "completes, reply with a short confirmation."
)

# Which tool + recorded params the grader checks per instruction kind.
_KIND_TO_TOOL = {
    "move": ("move_task", ("taskId", "columnId")),
    "claim": ("claim_task", ("taskId", "agentId")),
    "complete_ok": ("complete_task", ("taskId", "ok")),
    "complete_failed": ("complete_task", ("taskId", "ok")),
    "append_event": ("append_event", ("taskId",)),
}


class BenchmarkKanbanOpTools:
    """Board context (markdown or JSON) + one instruction → exactly one call
    to the right tool with the right ids. Requires a function-calling target
    (FunctionAgent rejects models that don't advertise tool use — captured as
    a per-trial failure, same as the other tool benchmarks)."""

    def __init__(
        self, target_uuid: UUID, num_trials: int = 5,
        context_format: str = "markdown",
    ):
        self.target_uuid = target_uuid
        self.num_trials = num_trials
        self.context_format = context_format

    def run(
        self,
        on_trial: Callable[[KanbanOpTrial], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> BenchmarkResult:
        _provider_id, model_name, args = _resolve_target(self.target_uuid)
        rng = random.Random()

        trials: list[KanbanOpTrial] = []
        timeouts = 0
        aborted = False
        abort_reason: str | None = None
        for i in range(self.num_trials):
            if should_stop is not None and should_stop():
                aborted = True
                abort_reason = "stopped by user"
                break
            data, agent_names = make_board(rng)
            kind = _OPS[i % len(_OPS)]
            instruction, expected = make_instruction(rng, data, agent_names, kind)
            board_text = serialize_board(data, agent_names, self.context_format)
            calls: list[tuple[str, dict[str, Any]]] = []

            def move_task(taskId: str, columnId: str) -> str:
                """Move the task to another column."""
                calls.append(("move_task", {"taskId": taskId, "columnId": columnId}))
                return "moved"

            def claim_task(taskId: str, agentId: str) -> str:
                """Claim the task for an agent."""
                calls.append(("claim_task", {"taskId": taskId, "agentId": agentId}))
                return "claimed"

            def complete_task(taskId: str, ok: bool) -> str:
                """Report the task done (ok=true) or failed (ok=false)."""
                calls.append(("complete_task", {"taskId": taskId, "ok": ok}))
                return "completed"

            def append_event(taskId: str, note: str) -> str:
                """Add a progress note to the task."""
                calls.append(("append_event", {"taskId": taskId, "note": note}))
                return "noted"

            t0 = time.monotonic()
            error: str | None = None
            timed_out = False
            try:
                the_llm = prepare_llm(_provider_id, model_name, args)
                agent = FunctionAgent(
                    tools=[move_task, claim_task, complete_task, append_event],
                    llm=the_llm,
                    system_prompt=KANBAN_TOOLS_SYSTEM_PROMPT,
                )
                _run_agent(agent, f"{board_text}\nInstruction: {instruction}")
            except (TimeoutError,):
                timed_out = True
                error = f"timed out after {TOOL_CALL_TIMEOUT:g}s"
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
            elapsed = time.monotonic() - t0

            tool_name, check_keys = _KIND_TO_TOOL[kind]
            got: dict[str, Any] | None = None
            if len(calls) == 1 and calls[0][0] == tool_name:
                got = {"op": expected["op"], **calls[0][1]}
            elif calls:
                got = {"op": calls[0][0], **calls[0][1]}  # wrong tool / extra calls
            correct = (error is None and len(calls) == 1
                       and calls[0][0] == tool_name
                       and all(calls[0][1].get(k) == expected[k]
                               for k in check_keys if k in expected))
            trial = KanbanOpTrial(
                trial_index=i,
                context_format=self.context_format,
                instruction=instruction,
                expected=expected,
                got=got,
                correct=correct,
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
                        f"{timeouts} trials timed out after {TOOL_CALL_TIMEOUT:g}s; "
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


if __name__ == "__main__":
    import db

    argv = [a for a in sys.argv[1:] if a]
    use_tools = "--tools" in argv
    use_json = "--json" in argv
    argv = [a for a in argv if a not in ("--tools", "--json")]
    if not argv:
        print("usage: python3 benchmark_kanban.py <target-uuid> [--tools] [--json]")
        sys.exit(1)
    app = db.make_app()
    with app.app_context():
        fmt = "json" if use_json else "markdown"
        cls = BenchmarkKanbanOpTools if use_tools else BenchmarkKanbanOpStructured
        bench = cls(UUID(argv[0]), num_trials=5, context_format=fmt)

        def show(t: KanbanOpTrial) -> None:
            mark = "✓" if t.correct else "✗"
            print(f"  {mark} [{t.elapsed:5.1f}s] {t.instruction}")
            if not t.correct:
                print(f"      expected {t.expected}")
                print(f"      got      {t.got}  error={t.error}")

        result = bench.run(on_trial=show)
        print(f"{cls.__name__} format={fmt}: {result.correct}/{result.total} correct, "
              f"{result.mistakes} mistakes, {result.failures} failures")
