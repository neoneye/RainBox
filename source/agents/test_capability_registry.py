"""Tests for the capability registry (PR 8): one code-owned record per action,
and disabling a capability removes it from BOTH the prompt catalog and the
dispatch path (the Phase 4 gate).
"""

from uuid import uuid4

import pytest

import db
from db import AssistantRun, AssistantStep
from agents.assistant import (
    CAPABILITIES,
    AssistantActionName,
    AssistantAgent,
    AssistantStepDecision,
    Capability,
    capability_report,
    enabled_capabilities,
)
from agents.assistant_fakes import scripted_decisions
from agents.config import ASSISTANT_UUID


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    try:
        yield app
    finally:
        # Always clear any operator disable so it can't leak into other tests.
        db.set_setting("assistant.disabled_capabilities", [])
        db.db.session.commit()
        db.db.session.rollback()
        ctx.pop()


def _agent() -> AssistantAgent:
    return AssistantAgent(agent_uuid=ASSISTANT_UUID, name="assistant", send=lambda _: None)


# --- registry shape -----------------------------------------------------------


def test_registry_covers_every_action():
    assert set(CAPABILITIES) == set(AssistantActionName)
    for name, cap in CAPABILITIES.items():
        assert isinstance(cap, Capability)
        assert cap.name is name
        if cap.terminal:
            assert cap.action is None  # terminal actions are posted by the loop
        else:
            assert cap.action is not None  # read actions have a dispatcher


def test_no_capability_encodes_permission_in_family():
    # family is a grouping, never a permission flag.
    for cap in CAPABILITIES.values():
        assert cap.family not in {"read", "write"}


# --- catalog generated from the registry --------------------------------------


def test_catalog_lists_enabled_prompt_exposed_capabilities():
    agent = _agent()
    catalog = agent._action_catalog()
    for name, cap in CAPABILITIES.items():
        if cap.prompt_exposed:
            assert name.value in catalog


# --- the gate: disable removes from prompt AND dispatch -----------------------


def test_disabled_capability_removed_from_prompt_and_dispatch(app_ctx):
    db.set_setting("assistant.disabled_capabilities", ["query_qa"])
    db.db.session.commit()

    human = db.get_human_user()
    assert human is not None
    room = db.create_chatroom(f"reg-test-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])
    db.post_chat_message(room.uuid, human.uuid, "what is the git status?")

    agent = _agent()
    agent._decide_next_step = scripted_decisions(
        AssistantStepDecision(reason="look it up", action=AssistantActionName.QUERY_QA,
                              args={"query": "git status"}),
        AssistantStepDecision(reason="answer", action=AssistantActionName.REPLY,
                              args={"message": "done"}),
    )
    try:
        result = agent.handle(uuid4(), {"room_uuid": str(room.uuid)})

        # Removed from the prompt catalog.
        assert "query_qa" not in agent._action_catalog()
        # Removed from dispatch: the query_qa step is a validation failure
        # (planned -> failed), never a running/observed dispatch.
        steps = (
            db.db.session.query(AssistantStep)
            .filter(AssistantStep.run_id == result["assistant_run_id"])
            .order_by(AssistantStep.id)
            .all()
        )
        phases = [(s.action, s.phase) for s in steps]
        assert ("query_qa", "running") not in phases
        assert ("query_qa", "observed") not in phases
        assert ("query_qa", "failed") in phases
        assert ("reply", "final") in phases
        assert result["status"] == "finished"
    finally:
        db.db.session.query(AssistantRun).filter(
            AssistantRun.room_uuid == room.uuid
        ).delete()
        db.db.session.query(db.Chatroom).filter(db.Chatroom.uuid == room.uuid).delete()
        db.db.session.commit()


def test_enabled_capabilities_excludes_disabled(app_ctx):
    db.set_setting("assistant.disabled_capabilities", ["kanban_read"])
    db.db.session.commit()
    enabled = enabled_capabilities()
    assert AssistantActionName.KANBAN_READ not in enabled
    assert AssistantActionName.QUERY_MEMORY in enabled


def test_capability_report_reflects_disable(app_ctx):
    db.set_setting("assistant.disabled_capabilities", ["workspace_read_command"])
    db.db.session.commit()
    report = {r["name"]: r for r in capability_report()}
    assert report["workspace_read_command"]["enabled"] is False
    assert report["query_memory"]["enabled"] is True
    # report carries the inspectable metadata the operator needs.
    assert report["query_qa"]["family"] == "query"
    assert report["reply"]["terminal"] is True
