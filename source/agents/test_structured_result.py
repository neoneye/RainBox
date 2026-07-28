"""_settle_structured_result: the guard against llama-index's streaming
partial-parser corrupting the final structured object (observed live: a
decision's free-form args dict came back {} in `.raw` while the provider
text carried the arguments). The provider's true text wins whenever it
re-validates; the stream-parsed object is only the fallback."""

import logging
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import BaseModel

import db
import llm
from agents.assistant import AssistantActionName, AssistantStepDecision
from agents.base import ModelGroupAgent

CORRUPTED = AssistantStepDecision(
    reason="compute", action=AssistantActionName.PYTHON_RUN, args={}
)
TRUE_TEXT = (
    '{"reason": "compute", "action": "python_run", '
    '"args": {"code": "print(357737172 * 0.3048)"}}'
)


def test_true_text_wins_over_the_corrupted_stream_object():
    result = ModelGroupAgent._settle_structured_result(
        AssistantStepDecision, CORRUPTED, TRUE_TEXT
    )
    assert isinstance(result, AssistantStepDecision)
    assert result.action is AssistantActionName.PYTHON_RUN
    assert result.args == {"code": "print(357737172 * 0.3048)"}


def test_unparseable_text_falls_back_to_the_stream_object():
    result = ModelGroupAgent._settle_structured_result(
        AssistantStepDecision, CORRUPTED, "not json at all"
    )
    assert result is CORRUPTED


def test_empty_text_falls_back_to_the_stream_object():
    result = ModelGroupAgent._settle_structured_result(
        AssistantStepDecision, CORRUPTED, None
    )
    assert result is CORRUPTED


def test_fence_remnant_text_is_recovered_not_discarded():
    """The live miss: the provider text was the valid object prefixed with
    a markdown-fence remnant ('json\\n{...}'), so re-validation failed and
    the corrupt stream object won — a required field arrived as None. The
    payload between the first '{' and the last '}' re-validates and wins."""
    result = ModelGroupAgent._settle_structured_result(
        AssistantStepDecision, CORRUPTED, "json\n" + TRUE_TEXT
    )
    assert isinstance(result, AssistantStepDecision)
    assert result.args == {"code": "print(357737172 * 0.3048)"}


def test_full_markdown_fence_is_recovered():
    result = ModelGroupAgent._settle_structured_result(
        AssistantStepDecision, CORRUPTED, f"```json\n{TRUE_TEXT}\n```"
    )
    assert result.args == {"code": "print(357737172 * 0.3048)"}


def test_prose_wrapped_object_is_recovered():
    result = ModelGroupAgent._settle_structured_result(
        AssistantStepDecision, CORRUPTED,
        f"Here is the decision:\n{TRUE_TEXT}\nLet me know."
    )
    assert result.args == {"code": "print(357737172 * 0.3048)"}


def test_schema_violating_stream_object_is_rejected_not_returned():
    """llama-index's partial parser builds the object WITHOUT validation, so
    `.raw` can carry a required field as None — pydantic would never produce
    that. Returning it hands every caller a schema-violating "parsed"
    object, and the failure surfaces far away as nonsense instead of an
    honest parse failure here. When neither the text nor the stream object
    validates, raise: the model-group loop falls through to the next
    candidate."""
    corrupt = AssistantStepDecision.model_construct(
        reason=None, action=AssistantActionName.PYTHON_RUN, args={}
    )
    with pytest.raises(ValueError, match="violates the schema"):
        ModelGroupAgent._settle_structured_result(
            AssistantStepDecision, corrupt, "not json at all"
        )


class _Reply(BaseModel):
    answer: str


class _FakeStructuredLLM:
    def stream_chat(self, _messages):
        yield SimpleNamespace(
            message=SimpleNamespace(content='{"answer":"ok"}'),
            raw=_Reply(answer="ok"),
        )


class _FakeLLM:
    def as_structured_llm(self, _response_model, callback_manager=None):
        return _FakeStructuredLLM()


def test_structured_call_log_names_the_resolved_provider(caplog, monkeypatch):
    model_uuid = uuid4()
    agent = ModelGroupAgent(uuid4(), "assistant", lambda _: None)
    agent.candidate_model_uuids = [model_uuid]
    monkeypatch.setattr(
        db,
        "resolved_model_kwargs",
        lambda _model_uuid: ("ollama", "gemma4:e4b", {}),
    )
    monkeypatch.setattr(llm, "prepare_llm", lambda *_args: _FakeLLM())
    caplog.set_level(logging.INFO, logger="agents.base")

    result = agent._structured_completion(
        system_prompt="Answer briefly.",
        user_prompt="Hello",
        response_model=_Reply,
    )

    assert result == _Reply(answer="ok")
    assert "calling model gemma4:e4b (provider ollama;" in caplog.text
    assert "LM Studio" not in caplog.text
