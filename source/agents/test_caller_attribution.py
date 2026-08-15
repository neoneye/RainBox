"""How an agent's LLM calls label themselves on /activity.

The convention: `<owner>.<call>`, with no blanket prefix in front of it. A
prefix carried by every row (the old `agent.`) is not attribution — it is the
same word repeated down the column, pushing the part that differs to the right.
The first segment names the *owner* instead, so the calls an operator thinks of
as the assistant's sort together whichever agent process made them.
"""

from uuid import uuid4

from agents.assistant_run_summarizer import AssistantRunSummarizerAgent
from agents.base import ModelGroupAgent


def _agent(cls, name):
    return cls(agent_uuid=uuid4(), name=name, send=lambda _msg: None)


def test_caller_tag_is_the_agent_name_unprefixed():
    agent = _agent(ModelGroupAgent, "query_filter_router")
    assert agent._caller_tag() == "query_filter_router"


def test_purpose_becomes_the_second_segment():
    """The assistant makes several distinct calls; each needs its own row on
    /activity, or a slow audit is indistinguishable from a slow decide."""
    agent = _agent(ModelGroupAgent, "assistant")
    assert agent._caller_tag("decide") == "assistant.decide"
    assert agent._caller_tag("acceptance_criteria") == "assistant.acceptance_criteria"


def test_run_summarizer_attributes_itself_to_the_assistant():
    """It is a separate agent only to stay off the reply's critical path — as
    a caller it is one of the assistant's calls, not a peer of it."""
    agent = _agent(AssistantRunSummarizerAgent, "assistant_run_summarizer")
    assert agent._caller_tag() == "assistant.run_summarizer"


def test_caller_name_override_is_opt_in():
    """Agents that own their own name keep it; nothing is renamed by default."""
    assert ModelGroupAgent.caller_name is None
    assert _agent(ModelGroupAgent, "kanban_worker")._caller_tag() == "kanban_worker"
