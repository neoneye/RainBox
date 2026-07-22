"""_settle_structured_result: the guard against llama-index's streaming
partial-parser corrupting the final structured object (observed live: a
decision's free-form args dict came back {} in `.raw` while the provider
text carried the arguments). The provider's true text wins whenever it
re-validates; the stream-parsed object is only the fallback."""

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
