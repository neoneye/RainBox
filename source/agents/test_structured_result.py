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


def test_fence_remnant_text_is_recovered_not_discarded():
    """The live miss (run 8b82ba60): the provider text was the valid object
    prefixed with a markdown-fence remnant ('json\\n{...}'), so re-validation
    failed and the corrupt stream object won — a required str field arrived
    as None. The payload between the first '{' and the last '}' re-validates
    and must win."""
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
    object; a live run then died deep in the criteria code with "unusable
    language tag None" instead of an honest parse failure here. When neither
    the text nor the stream object validates, raise: the model-group loop
    then falls through to the next candidate."""
    import pytest

    corrupt = AssistantStepDecision.model_construct(
        reason=None, action=None, args=None
    )
    with pytest.raises(ValueError, match="did not return a valid"):
        ModelGroupAgent._settle_structured_result(
            AssistantStepDecision, corrupt, "not json at all"
        )
