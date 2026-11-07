"""A turn drops the tool families switched off for the assistant in its room."""
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from agents.assistant import (
    CAPABILITIES,
    AssistantActionName,
    AssistantAgent,
    switchable_tool_families,
)


def _agent_with_all_caps():
    agent = AssistantAgent.__new__(AssistantAgent)
    agent.agent_uuid = uuid4()
    agent._caps = dict(CAPABILITIES)
    return agent


def test_a_switched_off_family_leaves_the_turn():
    agent = _agent_with_all_caps()
    run = SimpleNamespace(metadata_={})
    with patch("agents.assistant.db.member_disabled_tool_families", return_value={"kanban"}), \
         patch("agents.assistant.db.session"):
        agent._apply_room_tool_switches(uuid4(), run)
    assert not any(c.family == "kanban" for c in agent._caps.values())
    assert AssistantActionName.MEMORY_QUERY in agent._caps
    assert run.metadata_["room_disabled_tool_families"] == ["kanban"]


def test_the_turn_can_always_answer():
    agent = _agent_with_all_caps()
    run = SimpleNamespace(metadata_={})
    with patch("agents.assistant.db.member_disabled_tool_families",
               return_value={"conversation"}), patch("agents.assistant.db.session"):
        agent._apply_room_tool_switches(uuid4(), run)
    assert AssistantActionName.REPLY in agent._caps
    assert run.metadata_ == {}


def test_no_room_means_no_switches():
    agent = _agent_with_all_caps()
    with patch("agents.assistant.db.member_disabled_tool_families") as read:
        agent._apply_room_tool_switches(None, SimpleNamespace(metadata_={}))
    read.assert_not_called()
    assert len(agent._caps) == len(CAPABILITIES)


def test_switchable_families_leave_out_conversation():
    families = switchable_tool_families()
    assert "kanban" in families and "diary" in families
    assert "conversation" not in families
