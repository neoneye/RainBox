import asyncio
import json as _json
import logging
import random
import time
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

from llama_index.core.agent.workflow import FunctionAgent
from llama_index.core.base.llms.types import ChatResponse, TextBlock, ThinkingBlock
from llama_index.core.instrumentation import get_dispatcher
from llama_index.core.instrumentation.event_handlers import BaseEventHandler
from llama_index.core.instrumentation.events.llm import (
    LLMChatEndEvent,
    LLMChatInProgressEvent,
)
from llama_index.core.llms import LLM, ChatMessage, MessageRole
from llama_index.llms.openai_like import OpenAILike
from pydantic import BaseModel, Field

import providers

logger = logging.getLogger(__name__)


def prepare_llm(
    provider_id: str,
    model: str,
    arguments: dict[str, Any],
) -> LLM:
    """Build an LLM client for `model` on `provider_id`, after asking that
    provider to (re)load the model so its loaded context length is at least
    arguments['context_window'].

    Centralizing the load here means every UI test button, benchmark, and
    agent that goes through this constructor automatically gets the right
    context_length on providers that support it. Jan's ensure_loaded is a
    no-op; LM Studio's drives the `lms` CLI.

    Ollama uses the native `llama-index-llms-ollama` wrapper (see
    `_prepare_ollama_llm`) so chain-of-thought surfaces as a ThinkingBlock;
    every other provider uses ThinkingAwareOpenAILike over its OpenAI-compat
    endpoint. This is a pure constructor — it makes no capability decisions at
    runtime; thinking and structured-output settling happen on /models when the
    config is saved."""
    provider = providers.get(provider_id)
    desired_ctx = int(arguments.get("context_window") or 3900)
    provider.ensure_loaded(model, desired_ctx)
    if provider_id == "ollama":
        return _prepare_ollama_llm(model, arguments)
    merged: dict[str, Any] = dict(arguments)
    # `thinking` is a native-Ollama-only knob; OpenAILike has no such field
    # and would reject it. Drop it so callers can pass it uniformly.
    merged.pop("thinking", None)
    merged["model"] = model
    return ThinkingAwareOpenAILike(**merged)


def _prepare_ollama_llm(model: str, arguments: dict[str, Any]) -> LLM:
    """Build a native llama-index Ollama LLM by passing the resolved config
    arguments straight through.

    Ollama configs are stored in native `Ollama(...)` shape (base_url,
    request_timeout, thinking, context_window, temperature,
    is_function_calling_model — see providers/ollama.default_arguments), so this
    is a filter-and-splat: keep the keys that are Ollama constructor fields and
    drop the app-level flags (e.g. should_use_structured_outputs). Nothing is
    remapped, defaulted, or capability-checked at runtime — `thinking` and
    whether the model supports it are settled on the /models page when the
    config is saved, so a bad value can't surface as a runtime error here."""
    from llama_index.llms.ollama import Ollama

    native = {
        k: v
        for k, v in arguments.items()
        if k in Ollama.model_fields and k != "model"
    }
    return Ollama(model=model, **native)


def _extract_json_from_thinking(text: str) -> str:
    """Pull the JSON object out of a model's reasoning_content, which may contain
    a thinking preamble followed by the final JSON answer.

    Ported from PlanExe's ThinkingAwareOpenAILike helper."""
    try:
        _json.loads(text)
        return text
    except (_json.JSONDecodeError, ValueError):
        pass

    think_end = text.rfind("</think>")
    if think_end != -1:
        after_think = text[think_end + len("</think>"):].strip()
        if after_think:
            try:
                _json.loads(after_think)
                return after_think
            except (_json.JSONDecodeError, ValueError):
                pass

    pos = len(text)
    while True:
        pos = text.rfind("{", 0, pos)
        if pos == -1:
            break
        candidate = text[pos:]
        try:
            _json.loads(candidate)
            return candidate
        except (_json.JSONDecodeError, ValueError):
            pass
        pos -= 1

    return text


class ThinkingAwareOpenAILike(OpenAILike):
    """OpenAILike subclass that handles Qwen3-style thinking tokens.

    When thinking is enabled, LM Studio puts all model output into
    `reasoning_content` and leaves `content` empty. The parent class only reads
    `content`, so structured-output parsing crashes. This subclass intercepts
    `chat()`, and when `content` is empty, extracts the final JSON answer from
    `reasoning_content` and substitutes it as the message text.

    Ported from PlanExe's ThinkingAwareOpenAILike."""

    def chat(self, messages: Sequence[ChatMessage], **kwargs: Any) -> ChatResponse:
        response = super().chat(messages, **kwargs)
        content = response.message.content
        if content and content.strip() != "":
            return response
        raw = getattr(response, "raw", None)
        if not (raw and hasattr(raw, "choices") and raw.choices):
            return response
        raw_message = raw.choices[0].message
        reasoning_content = getattr(raw_message, "reasoning_content", None)
        if not reasoning_content:
            return response
        extracted = _extract_json_from_thinking(reasoning_content)
        logger.info(
            "ThinkingAwareOpenAILike: content empty; pulled %d chars from "
            "reasoning_content (%d chars total)",
            len(extracted),
            len(reasoning_content),
        )
        response.message = ChatMessage(
            role=response.message.role,
            blocks=[TextBlock(text=extracted)],
            additional_kwargs=response.message.additional_kwargs,
        )
        return response


class PingResponse(BaseModel):
    message: str = Field(description="A short greeting message.")


PING_SYSTEM_PROMPT: str = (
    "You are a ping/pong probe. Your task is to confirm liveness by replying "
    'with the word "pong".\n\n'
    "You MUST respond with a single JSON object that strictly adheres to the "
    "`PingResponse` schema. The schema has exactly one field:\n"
    "  - `message` (string): set this to exactly the lowercase word \"pong\".\n\n"
    "Rules:\n"
    "  - Output the JSON object and nothing else — no prose, no explanation, "
    "no markdown fences, no leading or trailing text.\n"
    "  - Do not add any extra fields beyond `message`.\n"
    "  - The exact value of `message` must be the four characters: p, o, n, g."
)


def _reasoning_content_text(response: ChatResponse) -> tuple[str, str]:
    """Return (reasoning_text, content_text) from one chat response, covering
    both the native Ollama wrapper (a ThinkingBlock in message.blocks) and the
    OpenAI-compat path (reasoning_content / reasoning on the raw choice)."""
    msg = getattr(response, "message", None)
    content = (getattr(msg, "content", None) or "") if msg is not None else ""
    reasoning = "".join(
        b.content or ""
        for b in (getattr(msg, "blocks", None) or [])
        if isinstance(b, ThinkingBlock)
    )
    if not reasoning:
        raw = getattr(response, "raw", None)
        choices = getattr(raw, "choices", None) if raw is not None else None
        if choices:
            rm = choices[0].message
            reasoning = (
                getattr(rm, "reasoning_content", None)
                or getattr(rm, "reasoning", None)
                or ""
            )
    return reasoning, content


class _ReasoningTally(BaseEventHandler):
    """Dispatcher handler that captures the model's reasoning text (and tallies
    reasoning vs content chars) across every underlying LLM chat. Capturing at
    the chat-event level (not the final response) is what makes the reasoning
    visible for structured-output and tool calls, whose wrappers drop the
    thinking block from the result.

    `completed_reasoning` holds each *completed* chat's reasoning
    (LLMChatEndEvent); `inflight_reasoning` holds the latest *cumulative*
    partial of the chat in progress (LLMChatInProgressEvent), which is the only
    data left when a streamed call is cut off by a timeout — so
    `reasoning_text`, `content_text`, and the char counts stay meaningful even
    when no End fires."""

    totals: dict[str, int] = Field(
        default_factory=lambda: {"reasoning": 0, "content": 0}
    )
    inflight: dict[str, int] = Field(
        default_factory=lambda: {"reasoning": 0, "content": 0}
    )
    completed_reasoning: list[str] = Field(default_factory=list)
    inflight_reasoning: str = Field(default="")
    completed_content: list[str] = Field(default_factory=list)
    inflight_content: str = Field(default="")

    @classmethod
    def class_name(cls) -> str:
        return "ReasoningTally"

    def handle(self, event: Any, **kwargs: Any) -> Any:
        if not isinstance(event, (LLMChatEndEvent, LLMChatInProgressEvent)):
            return
        response = getattr(event, "response", None)
        if response is None:
            return
        # Skip the structured/tool wrapper's reconstructed response (its `raw`
        # is the parsed pydantic object) so content isn't double-counted; real
        # provider responses carry a dict (native Ollama) or a `.choices` list.
        raw = getattr(response, "raw", None)
        if not (isinstance(raw, dict) or hasattr(raw, "choices")):
            return
        reasoning, content = _reasoning_content_text(response)
        if isinstance(event, LLMChatEndEvent):
            if reasoning:
                self.completed_reasoning.append(reasoning)
            if content:
                self.completed_content.append(content)
            self.totals["reasoning"] += len(reasoning)
            self.totals["content"] += len(content)
            self.inflight_reasoning = ""
            self.inflight_content = ""
            self.inflight["reasoning"] = 0
            self.inflight["content"] = 0
        else:  # in-progress events carry the cumulative partial, not a delta
            self.inflight_reasoning = reasoning
            self.inflight_content = content
            self.inflight["reasoning"] = len(reasoning)
            self.inflight["content"] = len(content)

    @property
    def reasoning_text(self) -> str:
        """All reasoning captured so far: completed chats' reasoning plus the
        in-flight partial, blank-line separated. Empty for a non-reasoning
        model (it emits no reasoning channel at all)."""
        parts = [*self.completed_reasoning]
        if self.inflight_reasoning:
            parts.append(self.inflight_reasoning)
        return "\n\n".join(parts)

    @property
    def reasoning_chars(self) -> int:
        return self.totals["reasoning"] + self.inflight["reasoning"]

    @property
    def content_text(self) -> str:
        """Completed content plus the latest cumulative in-flight partial."""
        parts = [*self.completed_content]
        if self.inflight_content:
            parts.append(self.inflight_content)
        return "\n\n".join(parts)

    @property
    def content_chars(self) -> int:
        return self.totals["content"] + self.inflight["content"]


@contextmanager
def capture_reasoning() -> Iterator["_ReasoningTally"]:
    """Capture the model's native thinking (the <think> block / reasoning
    channel) across every underlying LLM chat in the block, via instrumentation.
    Yields a tally whose `.reasoning_text` / `.content_text` and char counts
    stay readable both on success and after a timeout (the latest streamed
    partial is retained). Works through structured-output /
    tool wrappers that drop the thinking block from the final response —
    capturing what a reasoning model thinks before producing structured output,
    which a schema field like EditPlan.reasoning cannot show."""
    tally = _ReasoningTally()
    dispatcher = get_dispatcher()
    dispatcher.add_event_handler(tally)
    try:
        yield tally
    finally:
        try:
            dispatcher.event_handlers.remove(tally)
        except ValueError:
            pass


def run_named_test(
    action: str, provider_id: str, model: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Dispatch a named /models probe ('test_chat' / 'test_structuredoutput' /
    'test_tool') to its test function. Returns a dict with `message`, `elapsed`,
    and `reasoning_chars` / `content_chars` (tallied across every underlying LLM
    chat via instrumentation, so the counts survive structured-output / tool
    wrappers). Raises on an unknown action or any provider/LlamaIndex failure.
    Shared by the web layer and the killable subprocess worker."""
    with capture_reasoning() as tally:
        if action == "test_tool":
            message, elapsed = test_tool_call(provider_id, model, arguments)
        elif action == "test_chat":
            message, elapsed = test_chat(provider_id, model, arguments)
        elif action == "test_structuredoutput":
            message, elapsed = test_structured_output(provider_id, model, arguments)
        else:
            raise ValueError(f"unknown test action: {action!r}")
    return {
        "message": str(message),
        "elapsed": elapsed,
        "reasoning_chars": tally.reasoning_chars,
        "content_chars": tally.content_chars,
    }


def test_structured_output(
    provider_id: str, model: str, arguments: dict[str, Any]
) -> tuple[str, float]:
    """Run a ping/pong structured-output test against the named provider
    using arbitrary OpenAILike constructor kwargs. Used by the /models
    page to validate a proposed config — works for any config because we
    always force should_use_structured_outputs=True for the probe itself.
    Without this, OpenAILike falls back to a tool-based JSON extraction
    path that requires the `tools` parameter and most servers reject with
    HTTP 400.

    Wraps the model in ThinkingAwareOpenAILike — safe for both reasoning
    and non-reasoning models (the reasoning_content fallback only fires
    when content is empty).

    Returns (message_text, elapsed_seconds). Raises on any provider /
    LlamaIndex / schema-validation failure — caller renders the error."""
    arguments = {**arguments, "should_use_structured_outputs": True, "max_retries": 0}
    the_llm = prepare_llm(provider_id, model, arguments)
    sllm = the_llm.as_structured_llm(PingResponse)
    messages = [
        ChatMessage(role=MessageRole.SYSTEM, content=PING_SYSTEM_PROMPT),
        ChatMessage(role=MessageRole.USER, content="ping"),
    ]
    t0 = time.monotonic()
    response = sllm.chat(messages)
    elapsed = time.monotonic() - t0
    return response.raw.message, elapsed


CHAT_TEST_SYSTEM_PROMPT: str = "answer with 'pong'"


def test_chat(
    provider_id: str, model: str, arguments: dict[str, Any]
) -> tuple[str, float]:
    """Run a plain-text chat probe (no structured output, no tools): system
    "answer with 'pong'", user "ping". Passes if the reply contains "pong" — a
    high-temperature model may wrap prose around it, so we only require
    containment and return the full reply for inspection.

    Returns (full_reply_text, elapsed_seconds). Raises on any provider /
    LlamaIndex failure, or if the reply doesn't contain "pong"."""
    the_llm = prepare_llm(provider_id, model, arguments)
    messages = [
        ChatMessage(role=MessageRole.SYSTEM, content=CHAT_TEST_SYSTEM_PROMPT),
        ChatMessage(role=MessageRole.USER, content="ping"),
    ]
    t0 = time.monotonic()
    response = the_llm.chat(messages)
    elapsed = time.monotonic() - t0
    text = str(response.message.content or "").strip()
    if "pong" not in text.lower():
        raise RuntimeError(f"reply did not contain 'pong': {text!r}")
    return text, elapsed


REASONING_TEST_SYSTEM_PROMPT: str = "You are a helpful assistant."
REASONING_TEST_USER_PROMPT: str = (
    "What is 17 * 24? Think step by step, then give the final answer."
)


def stream_test_streaming(
    provider_id: str, model: str, arguments: dict[str, Any]
):
    """Generator version of the streaming probe: same intent as the old
    test_streaming (chain-of-thought-eliciting prompt, deltas dumped live to
    stdout, time-to-first-token tracked) but yields incremental stat dicts so
    the /models UI can render live progress and offer a Stop button.

    Each yielded dict has keys:
      chunk, content_chunks, reasoning_chunks,
      content_len, reasoning_len,
      ttft (float seconds or None), elapsed (float seconds),
      done (bool — True on the final yield only, never on intermediates),
    Yields are throttled to roughly one every 100ms; one final dict with
    `done=True` is always yielded last.

    Iterating to completion consumes the entire provider response; if the
    caller (e.g. a Flask response generator) stops iterating early because the
    client disconnected, the underlying HTTP stream to the provider is closed
    by garbage collection."""
    import sys

    # `thinking` rides in via `arguments` (the New override form's Ollama
    # checkbox), so the stream test honors whatever the caller selected — on
    # surfaces chain-of-thought, off doesn't. Non-Ollama providers drop the key.
    arguments = {**arguments, "max_retries": 0}
    the_llm = prepare_llm(provider_id, model, arguments)
    messages = [
        ChatMessage(role=MessageRole.SYSTEM, content=REASONING_TEST_SYSTEM_PROMPT),
        ChatMessage(role=MessageRole.USER, content=REASONING_TEST_USER_PROMPT),
    ]
    content_len = 0
    reasoning_len = 0
    chunk_count = 0
    content_chunks = 0
    reasoning_chunks = 0
    first_token_at: float | None = None
    last_kind: str | None = None

    sys.stdout.write(f"\n--- test_streaming({model}) START ---\n")
    sys.stdout.flush()
    t0 = time.monotonic()
    last_yield = t0

    def _stats(done: bool) -> dict[str, Any]:
        return {
            "chunk": chunk_count,
            "content_chunks": content_chunks,
            "reasoning_chunks": reasoning_chunks,
            "content_len": content_len,
            "reasoning_len": reasoning_len,
            "ttft": first_token_at,
            "elapsed": time.monotonic() - t0,
            "done": done,
        }

    for chunk in the_llm.stream_chat(messages):
        chunk_count += 1
        raw = getattr(chunk, "raw", None)
        if raw is not None and hasattr(raw, "choices") and raw.choices:
            # OpenAI-compat shape (LM Studio / Jan): the content and reasoning
            # deltas live on the raw choice. Ollama-over-/v1 names it `reasoning`.
            delta = raw.choices[0].delta
            c = getattr(delta, "content", None) or ""
            rc = (
                getattr(delta, "reasoning_content", None)
                or getattr(delta, "reasoning", None)
                or ""
            )
        else:
            # native llama-index shape (Ollama wrapper): content arrives on
            # chunk.delta, thinking on additional_kwargs["thinking_delta"].
            c = getattr(chunk, "delta", None) or ""
            ak = getattr(chunk, "additional_kwargs", None) or {}
            rc = ak.get("thinking_delta") or ""
        if (c or rc) and first_token_at is None:
            first_token_at = time.monotonic() - t0
        if rc:
            if last_kind != "reasoning":
                sys.stdout.write("\n[reasoning]\n")
                last_kind = "reasoning"
            sys.stdout.write(rc)
            sys.stdout.flush()
            reasoning_len += len(rc)
            reasoning_chunks += 1
        if c:
            if last_kind != "content":
                sys.stdout.write("\n[content]\n")
                last_kind = "content"
            sys.stdout.write(c)
            sys.stdout.flush()
            content_len += len(c)
            content_chunks += 1
        now = time.monotonic()
        if now - last_yield >= 0.1:
            yield _stats(done=False)
            last_yield = now

    elapsed = time.monotonic() - t0
    ttft_str = f"{first_token_at:.2f}s" if first_token_at is not None else "n/a"
    sys.stdout.write(
        f"\n--- test_streaming({model}) END "
        f"(TTFT {ttft_str}, total {elapsed:.2f}s, "
        f"{chunk_count} chunks: content {content_chunks}/{content_len} chars, "
        f"reasoning {reasoning_chunks}/{reasoning_len} chars) ---\n"
    )
    sys.stdout.flush()
    yield _stats(done=True)


TOOL_TEST_SYSTEM_PROMPT: str = (
    "send the number mentioned in the user prompt to the send_number tool"
)


def test_tool_call(
    provider_id: str, model: str, arguments: dict[str, Any]
) -> tuple[str, float]:
    """Run a tool-calling test against the named provider with a llama_index
    FunctionAgent: generate a fresh random number, ask the model to pass it
    to a `send_number` tool, and verify the tool was actually invoked with
    that exact number.

    The /models UI exposes this probe even for configs whose saved
    `is_function_calling_model` is false — so the user can ask "does this model
    support tools at all?" before creating an override. To get past the
    FunctionAgent precondition we always force the flag to True for the test;
    the saved config is untouched.

    Returns (message, elapsed_seconds). Raises if the provider/LlamaIndex
    errors, the tool was never called, or it was called with the wrong
    number."""
    arguments = {**arguments, "is_function_calling_model": True, "max_retries": 0}
    expected = random.randint(1000, 9999)
    received: list[int] = []

    def send_number(number: int) -> str:
        """Receive a single integer."""
        received.append(number)
        return "received"

    the_llm = prepare_llm(provider_id, model, arguments)
    agent = FunctionAgent(
        tools=[send_number], llm=the_llm, system_prompt=TOOL_TEST_SYSTEM_PROMPT
    )

    async def _run() -> Any:
        return await agent.run(user_msg=f"the number is {expected}")

    t0 = time.monotonic()
    asyncio.run(_run())
    elapsed = time.monotonic() - t0

    if not received:
        raise RuntimeError("send_number tool was never invoked")
    if expected not in received:
        raise RuntimeError(
            f"send_number was invoked with {received}, expected {expected}"
        )
    return f"send_number invoked with {expected}", elapsed
