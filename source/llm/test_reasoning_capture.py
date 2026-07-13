"""Tests for the reasoning-capture tally in `llm`: `_ReasoningTally` collects
the model's native thinking TEXT (not just char counts) across chat events,
covering both the native-Ollama shape (a ThinkingBlock in message.blocks) and
the timeout case (the in-flight cumulative partial is retained).

Pure unit tests — events are constructed directly, no provider or DB.
"""

from llama_index.core.base.llms.types import (
    ChatResponse,
    TextBlock,
    ThinkingBlock,
)
from llama_index.core.instrumentation.events.llm import (
    LLMChatEndEvent,
    LLMChatInProgressEvent,
)
from llama_index.core.llms import ChatMessage, MessageRole

from llm import _ReasoningTally


def _response(reasoning: str, content: str) -> ChatResponse:
    blocks = []
    if reasoning:
        blocks.append(ThinkingBlock(content=reasoning))
    blocks.append(TextBlock(text=content))
    return ChatResponse(
        message=ChatMessage(role=MessageRole.ASSISTANT, blocks=blocks),
        # A dict raw marks a real provider response (the tally skips the
        # structured wrapper's reconstructed response, whose raw is the parsed
        # pydantic object).
        raw={"provider": "fake"},
    )


def _messages() -> list[ChatMessage]:
    return [ChatMessage(role=MessageRole.USER, content="ping")]


def test_completed_chat_reasoning_text_is_captured():
    tally = _ReasoningTally()
    tally.handle(LLMChatEndEvent(
        messages=_messages(), response=_response("let me think about this", "pong")
    ))
    assert tally.reasoning_text == "let me think about this"
    assert tally.reasoning_chars == len("let me think about this")
    assert tally.content_chars == len("pong")


def test_inflight_partial_is_kept_when_no_end_event_fires():
    """A streamed call cut off by a timeout only ever emitted in-progress
    events; the latest cumulative partial must still be readable."""
    tally = _ReasoningTally()
    tally.handle(LLMChatInProgressEvent(
        messages=_messages(), response=_response("thinking so f", "")
    ))
    tally.handle(LLMChatInProgressEvent(
        messages=_messages(), response=_response("thinking so far", "")
    ))
    assert tally.reasoning_text == "thinking so far"


def test_end_event_settles_the_inflight_partial():
    """In-progress partials are cumulative; the End event's full reasoning
    replaces them (no duplication)."""
    tally = _ReasoningTally()
    tally.handle(LLMChatInProgressEvent(
        messages=_messages(), response=_response("thinking", "")
    ))
    tally.handle(LLMChatEndEvent(
        messages=_messages(), response=_response("thinking, done", "pong")
    ))
    assert tally.reasoning_text == "thinking, done"


def test_non_reasoning_model_yields_empty_reasoning_text():
    tally = _ReasoningTally()
    tally.handle(LLMChatEndEvent(
        messages=_messages(), response=_response("", "pong")
    ))
    assert tally.reasoning_text == ""
    assert tally.reasoning_chars == 0
    assert tally.content_chars == len("pong")


def test_multiple_completed_chats_are_joined():
    """A model-group fallback or multi-call block can complete several chats;
    each completed chat's reasoning is kept, blank-line separated."""
    tally = _ReasoningTally()
    tally.handle(LLMChatEndEvent(
        messages=_messages(), response=_response("first thought", "a")
    ))
    tally.handle(LLMChatEndEvent(
        messages=_messages(), response=_response("second thought", "b")
    ))
    assert tally.reasoning_text == "first thought\n\nsecond thought"
