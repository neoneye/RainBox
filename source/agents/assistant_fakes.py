"""Fake-model seam for deterministic assistant-loop tests.

The only live-model seam in the assistant is `AssistantAgent._decide_next_step`.
Tests replace it with a *scripted* provider so the loop, step cap, validation,
dispatch, and trace shape can be exercised without LM Studio, the network, or a
live model — monkeypatch `_decide_next_step` with `scripted_decisions(...)`.

    from agents.assistant import AssistantActionName, AssistantStepDecision
    from agents.assistant_fakes import scripted_decisions

    monkeypatch.setattr(
        agent, "_decide_next_step",
        scripted_decisions(
            AssistantStepDecision(reason="Need git status.",
                                  action=AssistantActionName.MEMORY_QUERY,
                                  args={"query": "git status"}),
            AssistantStepDecision(reason="Have enough.",
                                  action=AssistantActionName.REPLY,
                                  args={"message": "Working tree clean.", "audit": "OK"}),
        ),
    )
"""

from collections.abc import Callable
from typing import Any

from agents.assistant import AssistantStepDecision


def scripted_decisions(
    *decisions: AssistantStepDecision,
) -> Callable[..., AssistantStepDecision]:
    """Return a stand-in for `_decide_next_step` that yields `decisions` in order.

    The returned callable ignores its keyword arguments (the real method is
    called as `_decide_next_step(messages=..., scratchpad=..., step_index=...)`),
    so a test only has to script the model's outputs, not its inputs.

    It raises `AssertionError` if the loop asks for more decisions than were
    scripted. That over-consumption guard is the point: a loop that calls the
    model an unexpected extra time (a missing terminal step, a runaway loop) is
    a test failure, not silent behaviour.
    """
    queue = list(decisions)

    def fake_decide_next_step(**_kwargs: Any) -> AssistantStepDecision:
        assert queue, "assistant requested more decisions than were scripted"
        return queue.pop(0)

    return fake_decide_next_step
