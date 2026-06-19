"""Assistant decision contract — the structured shape a single ReAct step emits.

This module is intentionally small in PR 1: it defines only the *contract* the
assistant loop will speak, not the loop itself. The `AssistantAgent` runtime
(the bounded plan -> act -> observe loop) lands in PR 2 in this same file; the
read-only action dispatcher lands in PR 4.

Keeping the schema here in PR 1 lets the eval harness drive a deterministic
fake model (see `agents/assistant_fakes.py`) before any live LLM behaviour
exists. The schema is the linchpin of every PR 1-4 loop test: tests feed a
scripted sequence of `AssistantStepDecision` objects and assert on the trace.

See docs/proposals/2026-06-19-improvements-v2.md ("Step-decision schema") for
the binding rationale. The shape is illustrative-until-promoted: it may be
refined by a later PR as long as it still satisfies the assistant contracts.
"""

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class AssistantActionName(str, Enum):
    """The bounded set of capabilities a single assistant step may request.

    This enum is the primitive capability registry (Phase 4 formalizes it with
    metadata). The model can only ever name an action in this enum; code, not
    prompt text, decides what each one is allowed to do.

    PR 1 ships the full read-only enum so the eval harness can script the
    actions PRs 2-4 will implement, but only the two terminal actions are wired
    in PR 2 and the read actions in PR 4.
    """

    # Terminal actions (PR 2): the loop ends the run and posts a chat message.
    REPLY = "reply"
    ASK_CLARIFYING_QUESTION = "ask_clarifying_question"

    # Read-only actions (PR 4): each performs one bounded read and returns an
    # observation the loop feeds back to the model.
    QUERY_MEMORY = "query_memory"
    QUERY_QA = "query_qa"
    WORKSPACE_READ_COMMAND = "workspace_read_command"
    KANBAN_READ = "kanban_read"


class AssistantStepDecision(BaseModel):
    """One structured decision the model emits per loop step.

    Emitted via the provider's grammar-constrained structured-output mode
    (`as_structured_llm`), not freeform string parsing. The dispatcher owns
    action-specific argument typing for now, so `args` stays an open dict until
    the action surface is large enough to justify a per-action union.
    """

    reason: str = Field(
        description=(
            "Brief operator-facing rationale for this step. This is an audit "
            "note shown in the trace, not hidden chain-of-thought."
        )
    )
    action: AssistantActionName
    args: dict[str, Any] = Field(default_factory=dict)
