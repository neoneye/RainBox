"""Tests for the assistant decision contract and the fake-model seam.

No DB and no LM Studio dependency: these exercise pure schema + helper
behaviour. They are the PR 1 half of the eval harness — they prove the seam
that every PR 2-4 loop test will rely on actually behaves as scripted.
"""

import pytest
from pydantic import ValidationError

from agents.assistant import AssistantActionName, AssistantStepDecision
from agents.assistant_fakes import scripted_decisions


def _reply(message: str) -> AssistantStepDecision:
    return AssistantStepDecision(
        reason="done", action=AssistantActionName.REPLY, args={"message": message}
    )


# --- the fake-model seam ------------------------------------------------------


def test_scripted_decisions_returns_in_order():
    fake = scripted_decisions(_reply("one"), _reply("two"))
    first = fake(transcript="", scratchpad=[], step_index=0)
    second = fake(transcript="", scratchpad=[], step_index=1)
    assert first.args["message"] == "one"
    assert second.args["message"] == "two"


def test_scripted_decisions_raises_when_over_consumed():
    fake = scripted_decisions(_reply("only"))
    fake(transcript="", scratchpad=[], step_index=0)
    with pytest.raises(AssertionError, match="more decisions than were scripted"):
        fake(transcript="", scratchpad=[], step_index=1)


def test_scripted_decisions_accepts_decide_next_step_kwargs():
    """The stand-in must accept the exact kwargs the real _decide_next_step is
    called with, so monkeypatching it is a drop-in replacement."""
    fake = scripted_decisions(_reply("ok"))
    decision = fake(transcript="<human> hi", scratchpad=[{"action": "x"}], step_index=3)
    assert decision.action is AssistantActionName.REPLY


def test_scripted_decisions_empty_raises_immediately():
    fake = scripted_decisions()
    with pytest.raises(AssertionError, match="more decisions than were scripted"):
        fake(transcript="", scratchpad=[], step_index=0)


# --- the decision schema ------------------------------------------------------


def test_decision_args_default_to_empty_dict():
    decision = AssistantStepDecision(
        reason="just ask", action=AssistantActionName.ASK_CLARIFYING_QUESTION
    )
    assert decision.args == {}


def test_decision_parses_action_from_string_value():
    """Structured output arrives as JSON, so the action comes in as its string
    value and must coerce to the enum member."""
    decision = AssistantStepDecision.model_validate(
        {"reason": "look it up", "action": "query_qa", "args": {"query": "git status"}}
    )
    assert decision.action is AssistantActionName.QUERY_QA


def test_decision_rejects_unknown_action():
    with pytest.raises(ValidationError):
        AssistantStepDecision.model_validate(
            {"reason": "do a thing", "action": "delete_everything", "args": {}}
        )


def test_action_enum_covers_the_known_action_surface():
    """Lock the action surface so an accidental rename/removal is caught. The
    read-only set (PR 1-4), the memory write family (PR 9), and the kanban
    log-and-undo write family (move / complete / comment)."""
    assert {a.value for a in AssistantActionName} == {
        "reply",
        "ask_clarifying_question",
        "query_memory",
        "query_qa",
        "workspace_read_command",
        "kanban_read",
        "remember",
        "activate_memory",
        "forget_memory",
        "kanban_move",
        "kanban_complete",
        "kanban_comment",
        "kanban_create",
        "kanban_delete_task",
        "set_reminder",
        "edit_file",
        "propose_skill",
        "activate_skill",
        "skill_delete",
        "reject_memory_candidate",
        "reactivate_memory",
    }
