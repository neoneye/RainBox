"""Tests for UnstructuredChatAgent — wiring, the runtime
must-not-have-structured-output guard, and the end-to-end streaming path with a
fake LLM (real DB rows, no LM Studio)."""

import inspect
from types import SimpleNamespace
from uuid import uuid4

import pytest

import agents.base as agentmod
import agents.chat_unstructured as agent_chat_unstructured
import db
from agents.chat_unstructured import UnstructuredChatAgent


def test_chat_unstructured_in_agent_config():
    from agents.config import CHAT_UNSTRUCTURED_UUID, agent_config

    entry = agent_config["chat_unstructured"]
    assert entry["uuid"] == CHAT_UNSTRUCTURED_UUID
    assert entry["next"] is None
    # It needs structured output turned OFF — the /agent_models page offers it
    # only groups with "structured output: must not have" (and the model call
    # also enforces this at runtime). It must NOT require structured output.
    assert entry.get("excludes_structured_output") is True
    assert "requires_structured_output" not in entry
    assert "requires_function_calling" not in entry


def test_agent_group_options_filters_by_structured_output_constraint():
    """The /agent_models dropdown offers an excludes-structured-output agent only
    groups whose structured-output constraint is 'must not have'."""
    from webapp.agent_views import _agent_group_options

    def opt(name, sc):
        return {
            "uuid": name,
            "label": name,
            "requires_function_calling": False,
            "requires_structured_output": sc == "must_have",
            "structured_output_constraint": sc,
        }

    groups = [opt("none", "dont_care"), opt("on", "must_have"), opt("off", "must_not_have")]

    # An unstructured agent: only the must_not_have group is offered.
    offered = _agent_group_options({"excludes_structured_output": True}, groups)
    assert [o["uuid"] for o in offered] == ["off"]

    # A structured agent: only the must_have group.
    offered = _agent_group_options({"requires_structured_output": True}, groups)
    assert [o["uuid"] for o in offered] == ["on"]

    # An unconstrained agent: all groups.
    offered = _agent_group_options({}, groups)
    assert [o["uuid"] for o in offered] == ["none", "on", "off"]


def test_chat_unstructured_is_wired_as_responder():
    from agents.config import CHAT_UNSTRUCTURED_UUID
    from webapp.chat_api import CHAT_RESPONDER_UUIDS

    assert CHAT_UNSTRUCTURED_UUID in CHAT_RESPONDER_UUIDS


def test_subclasses_model_group_agent_not_structured():
    assert issubclass(UnstructuredChatAgent, agentmod.ModelGroupAgent)
    assert not issubclass(UnstructuredChatAgent, agentmod.StructuredLLMAgent)


def test_stream_reply_raises_without_must_not_have_constraint():
    """The guard fires before any model/DB access: a group that doesn't satisfy
    'structured output: must not have' makes _stream_reply raise immediately."""
    agent = UnstructuredChatAgent(
        agent_uuid=uuid4(), name="chat_unstructured", send=lambda _: None
    )
    agent.group_excludes_structured_output = False
    agent.candidate_model_uuids = [uuid4()]  # non-empty, to prove the order
    with pytest.raises(RuntimeError, match="must not have"):
        agent._stream_reply(uuid4(), "hello")


def test_user_prompt_accepts_journal_id():
    sig = inspect.signature(UnstructuredChatAgent.user_prompt)
    assert "journal_id" in sig.parameters, sig.parameters


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    try:
        yield app
    finally:
        ctx.pop()


def _openai_chunk(content="", reasoning=""):
    delta = SimpleNamespace(
        content=content or None, reasoning_content=reasoning or None
    )
    return SimpleNamespace(raw=SimpleNamespace(choices=[SimpleNamespace(delta=delta)]))


class _FakeStreamingLLM:
    def stream_chat(self, messages):
        yield _openai_chunk(reasoning="let me think ")
        yield _openai_chunk(reasoning="carefully")
        yield _openai_chunk(content="Hello")
        yield _openai_chunk(content=", world")


def test_stream_reply_creates_thinking_and_answer_rows(app_ctx, monkeypatch):
    """End-to-end: a fake streaming LLM produces reasoning then answer; the agent
    must leave two finalized rows — a 'thinking' bubble and a 'message' reply."""
    human = db.get_human_user()
    assert human is not None
    agent_uuid = uuid4()
    db.db.session.add(
        db.ChatUser(uuid=agent_uuid, name=f"cu-{uuid4().hex[:6]}", user_type="agent")
    )
    db.db.session.flush()
    room = db.create_chatroom(f"stream-{uuid4().hex[:6]}", human.uuid, [agent_uuid])
    try:
        monkeypatch.setattr(
            db, "resolved_model_kwargs", lambda u: ("lm_studio", "fake-model", {})
        )
        monkeypatch.setattr(
            agent_chat_unstructured, "prepare_llm", lambda *a, **k: _FakeStreamingLLM()
        )
        agent = UnstructuredChatAgent(
            agent_uuid=agent_uuid, name="chat_unstructured", send=lambda _: None
        )
        agent.group_excludes_structured_output = True
        agent.candidate_model_uuids = [uuid4()]

        reply = agent._stream_reply(room.uuid, "hi there")
        assert reply == "Hello, world"

        rows = db.list_room_messages(room.uuid)
        by_kind = {r["kind"]: r for r in rows if r["sender_uuid"] == str(agent_uuid)}
        assert by_kind["thinking"]["text"] == "let me think carefully"
        assert by_kind["thinking"]["streaming"] is False
        assert by_kind["message"]["text"] == "Hello, world"
        assert by_kind["message"]["streaming"] is False
    finally:
        db.db.session.query(db.Chatroom).filter(db.Chatroom.uuid == room.uuid).delete()
        db.db.session.query(db.ChatUser).filter(db.ChatUser.uuid == agent_uuid).delete()
        db.db.session.commit()
