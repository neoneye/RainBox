"""Benchmark for EditDocumentAgentV1, EditDocumentAgentV2, and later siblings.

Layer 1 of the benchmark stack (mirrors benchmarks/basic.py):
  - EditDocumentTest: data describing one test case (input + expected output).
  - EDIT_DOCUMENT_TESTS: the seeded list of tests.
  - EditDocumentTrial: per-test result record.
  - BenchmarkEditDocumentResult: per-(target, agent) result aggregate.
  - BenchmarkEditDocument: runs every test against one (target, agent).

Layer 2 (background orchestration + state dict the webapp polls) lives in
benchmarks/editdocument_runner.py.

See docs/superpowers/specs/2026-05-30-benchmark-editdocument-design.md.
"""

import time
from dataclasses import dataclass, field
from typing import Any, Callable
from uuid import UUID

import db
import llm
from agents.patch_apply import apply_patches


@dataclass(frozen=True)
class EditDocumentTest:
    """One test case: a document, an instruction, and the expected exact
    post-edit document the scoring oracle checks against."""

    name: str           # short id used as column header (e.g. "append_task")
    description: str    # one-line, shown in cell tooltip / drill-down
    document: str       # initial document
    instructions: str   # what the agent is asked to do
    expected: str       # exact post-edit document for byte-for-byte match


@dataclass
class EditDocumentTrial:
    """One trial's result: per-cell record."""

    test_name: str
    document: str
    instructions: str
    expected: str
    applied: str | None         # post-edit document, or None on failure
    patches: list[dict[str, Any]] | None
    agent_status: str | None    # "done" | "partial" | "unclear" | None (v2 only)
    agent_comment: str | None   # v2 only; None for v1
    thinking_chars: int | None   # chars of the model's native thinking (<think> block); 0 if non-reasoning
    content_chars: int | None    # chars of the model's native response content
    correct: bool
    elapsed: float
    error: str | None           # exception message on failure


@dataclass
class BenchmarkEditDocumentResult:
    """Per-(target_uuid, agent) result aggregate."""

    target_kind: str            # "config" or "override"
    target_uuid: UUID
    model_name: str
    agent_name: str             # "edit_document_v1", "edit_document_v2", …
    total: int
    correct: int
    mistakes: int               # ran cleanly but output != expected
    failures: int               # raised
    trials: list[EditDocumentTrial] = field(default_factory=list)


# ----- Seeded tests -----------------------------------------------------------

TODO_LIST: str = (
    "## todo list\n"
    "\n"
    "- [ ] fix water pipe\n"
    "- [ ] buy shoelaces\n"
    "- [ ] close support ticket\n"
    "- [ ] check insurance rules\n"
    "\n"
    "## status\n"
    "ok\n"
)


TODO_LIST_WITH_MOVE_FURNITURE: str = (
    "## todo list\n"
    "\n"
    "- [ ] fix water pipe\n"
    "- [ ] buy shoelaces\n"
    "- [ ] close support ticket\n"
    "- [ ] check insurance rules\n"
    "- [ ] move furniture\n"
    "\n"
    "## status\n"
    "ok\n"
)


_TODO_LIST_WITHOUT_SHOELACES: str = (
    "## todo list\n"
    "\n"
    "- [ ] fix water pipe\n"
    "- [ ] close support ticket\n"
    "- [ ] check insurance rules\n"
    "\n"
    "## status\n"
    "ok\n"
)


_TODO_LIST_WITH_X_ON_SHOELACES: str = (
    "## todo list\n"
    "\n"
    "- [ ] fix water pipe\n"
    "- [x] buy shoelaces\n"
    "- [ ] close support ticket\n"
    "- [ ] check insurance rules\n"
    "\n"
    "## status\n"
    "ok\n"
)

COMMENT_FOLLOWED_BY_EMPTY_LINES: str = (
    "comment\n"
    "\n"
    "\n"
    "\n"
)



EDIT_DOCUMENT_TESTS: list[EditDocumentTest] = [
    EditDocumentTest(
        name="append_newline",
        description="Append empty row end of the file.",
        document=COMMENT_FOLLOWED_BY_EMPTY_LINES,
        instructions="Append a newline to the file",
        expected=COMMENT_FOLLOWED_BY_EMPTY_LINES + "\n",
    ),
    EditDocumentTest(
        name="append_text",
        description="Append row end of the file.",
        document=COMMENT_FOLLOWED_BY_EMPTY_LINES,
        instructions="Append row that says 'bottom'",
        expected=COMMENT_FOLLOWED_BY_EMPTY_LINES + "bottom\n",
    ),
    EditDocumentTest(
        name="append_task",
        description="Append a new task to the end of the list.",
        document=TODO_LIST,
        instructions="The 'todo list' section has a list of tasks. At the bottom of that list, append this task: '- [ ] move furniture'",
        expected=TODO_LIST_WITH_MOVE_FURNITURE,
    ),
    EditDocumentTest(
        name="remove_task",
        description="Remove a named task line.",
        document=TODO_LIST,
        instructions="Remove 'buy shoelaces'",
        expected=_TODO_LIST_WITHOUT_SHOELACES,
    ),
    EditDocumentTest(
        name="check_task",
        description="Mark a task as done by changing '[ ]' to '[x]'.",
        document=TODO_LIST,
        instructions="Place an 'x' in the 'buy shoelaces' task",
        expected=_TODO_LIST_WITH_X_ON_SHOELACES,
    ),
]


def _instantiate_agent(agent_class: type, agent_uuid: UUID, name: str):
    """Construct the agent with a no-op send callback. Extracted so tests
    can monkeypatch and inject a pre-stubbed agent."""
    return agent_class(agent_uuid=agent_uuid, name=name, send=lambda _m: None)


class BenchmarkEditDocument:
    """Run every EditDocumentTest against one (target_uuid, agent) and
    score by byte-for-byte equality after applying patches.

    The target uuid identifies either a ModelConfig or a ModelConfigOverride;
    it is pinned as the agent's only candidate model so the underlying
    StructuredLLMAgent._structured_call uses that one model (no fallback).
    """

    def __init__(
        self,
        target_uuid: UUID,
        agent_class: type,
        agent_uuid: UUID,
        agent_name: str,
        tests: list[EditDocumentTest] | None = None,
    ) -> None:
        self.target_uuid = target_uuid
        self.agent_class = agent_class
        self.agent_uuid = agent_uuid
        self.agent_name = agent_name
        self.tests = tests if tests is not None else EDIT_DOCUMENT_TESTS

    def run(
        self,
        on_trial: Callable[[EditDocumentTrial], None] | None = None,
        on_trial_start: Callable[[str], None] | None = None,
    ) -> BenchmarkEditDocumentResult:
        _provider_id, model_name, _args = db.resolved_model_kwargs(self.target_uuid)
        kind = "config" if db.get_model_config(self.target_uuid) is not None else "override"
        agent = _instantiate_agent(self.agent_class, self.agent_uuid, self.agent_name)
        # Pin a single candidate so the fallback loop has only this model.
        agent.model_group_uuid = None
        agent.candidate_model_uuids = [self.target_uuid]

        trials: list[EditDocumentTrial] = []
        correct = 0
        mistakes = 0
        failures = 0

        for test in self.tests:
            if on_trial_start is not None:
                on_trial_start(test.name)
            t0 = time.monotonic()
            # Capture the model's native thinking (the <think> block) vs content
            # across the structured call. The call streams, so even when a slow
            # reasoning model is cut off by the request timeout, the partial
            # counts received so far are still reported — distinct from the
            # EditPlan.reasoning JSON field, which never arrives on a timeout.
            with llm.capture_reasoning() as native:
                try:
                    result = agent.handle(
                        journal_id=0,
                        payload={"document": test.document, "instructions": test.instructions},
                    )
                    elapsed = time.monotonic() - t0
                    patches = result.get("patches")
                    agent_status = result.get("status")    # present for v2 only
                    agent_comment = result.get("comment")  # present for v2 only
                    applied = apply_patches(test.document, patches or [])
                    is_correct = applied == test.expected
                    trial = EditDocumentTrial(
                        test_name=test.name,
                        document=test.document,
                        instructions=test.instructions,
                        expected=test.expected,
                        applied=applied,
                        patches=patches,
                        agent_status=agent_status,
                        agent_comment=agent_comment,
                        thinking_chars=native.reasoning_chars,
                        content_chars=native.content_chars,
                        correct=is_correct,
                        elapsed=elapsed,
                        error=None,
                    )
                    if is_correct:
                        correct += 1
                    else:
                        mistakes += 1
                except Exception as e:
                    elapsed = time.monotonic() - t0
                    trial = EditDocumentTrial(
                        test_name=test.name,
                        document=test.document,
                        instructions=test.instructions,
                        expected=test.expected,
                        applied=None,
                        patches=None,
                        agent_status=None,
                        agent_comment=None,
                        # Partial counts received before the timeout/error.
                        thinking_chars=native.reasoning_chars,
                        content_chars=native.content_chars,
                        correct=False,
                        elapsed=elapsed,
                        error=f"{type(e).__name__}: {e}",
                    )
                    failures += 1
            trials.append(trial)
            if on_trial is not None:
                on_trial(trial)

        return BenchmarkEditDocumentResult(
            target_kind=kind,
            target_uuid=self.target_uuid,
            model_name=model_name,
            agent_name=self.agent_name,
            total=len(self.tests),
            correct=correct,
            mistakes=mistakes,
            failures=failures,
            trials=trials,
        )
