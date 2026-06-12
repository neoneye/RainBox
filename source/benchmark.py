"""String-transformation benchmarks for an LLM identified by ModelConfig /
ModelConfigOverride uuid.

Six benchmarks:
  - BenchmarkBase64Decode: random string -> base64 -> ask LLM to decode ->
    compare to the original.
  - BenchmarkBase64Encode: random string -> ask LLM to base64-encode ->
    compare to Python's reference encoding.
  - BenchmarkReverseString: random string -> ask LLM to reverse it ->
    compare to the Python-reversed string.
  - BenchmarkReverseList: random list of strings -> ask LLM to reverse
    the list order -> compare to Python's `lst[::-1]` (individual items
    must NOT be modified).
  - BenchmarkToolOrder: give a function-calling model three tools (func1, func2,
    func3) and ask it, in one prompt, to call them in a specific order. Each
    trial uses one of the 6 permutations (shuffled; the default 5 trials cover 5
    of the 6) -> correct iff all three were invoked once each, in the requested
    order.
  - BenchmarkToolRoute: give a function-calling model three tools (random,
    func1, func2). `random` returns the NAME of a function; the model must then
    call exactly that function -> correct iff the observed sequence is exactly
    [random, <whatever random returned>] (a data-dependent tool dispatch).

The first four use structured output via as_structured_llm(); the last uses a
LlamaIndex FunctionAgent (tool calling). All take a per-trial callback so
callers can stream progress, and all produce a BenchmarkResult with totals
(correct / mistakes / failures) and the full list of per-trial records.

CLI demo:
    python3 benchmark.py                              # decode, first available config
    python3 benchmark.py <uuid>                       # decode against specific target
    python3 benchmark.py <uuid> --encode              # encode against specific target
    python3 benchmark.py <uuid> --reverse             # reverse-string against target
    python3 benchmark.py <uuid> --reverse-list        # reverse-list against target
    python3 benchmark.py <uuid> --tool-order          # tool-call ordering (needs FC model)
    python3 benchmark.py <uuid> --tool-route          # data-dependent tool dispatch (needs FC model)
"""

import asyncio
import base64
import itertools
import json
import random
import string
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from uuid import UUID

from llama_index.core.agent.workflow import FunctionAgent
from llama_index.core.llms import ChatMessage, MessageRole
from pydantic import BaseModel, Field

import db
from llm import prepare_llm

DECODE_SYSTEM_PROMPT: str = (
    "You are a base64 decoder. The user message contains a base64-encoded "
    "ASCII string. Decode it and return the decoded plaintext.\n\n"
    "You MUST respond with a single JSON object that strictly adheres to the "
    "`Base64DecodeResponse` schema. The schema has exactly one field:\n"
    "  - `plaintext` (string): the exact decoded plaintext.\n\n"
    "Rules:\n"
    "  - Output the JSON object and nothing else — no prose, no markdown "
    "fences, no explanation, no leading or trailing text.\n"
    "  - Do not add any extra fields beyond `plaintext`.\n"
    "  - The value of `plaintext` must be the exact string that, when "
    "base64-encoded, produces the input. Do not paraphrase or summarize."
)


ENCODE_SYSTEM_PROMPT: str = (
    "You are a base64 encoder. The user message is an ASCII string. Encode "
    "it to base64 and return the result.\n\n"
    "You MUST respond with a single JSON object that strictly adheres to the "
    "`Base64EncodeResponse` schema. The schema has exactly one field:\n"
    "  - `base64` (string): the exact base64 encoding of the input string.\n\n"
    "Rules:\n"
    "  - Output the JSON object and nothing else — no prose, no markdown "
    "fences, no explanation, no leading or trailing text.\n"
    "  - Do not add any extra fields beyond `base64`.\n"
    "  - Use the standard base64 alphabet (A-Z, a-z, 0-9, +, /) and include "
    "the `=` padding where required. No newlines or whitespace inside the "
    "string."
)


REVERSE_SYSTEM_PROMPT: str = (
    "You are a string reverser. The user message is an ASCII string. Reverse "
    "it character-by-character and return the reversed string.\n\n"
    "You MUST respond with a single JSON object that strictly adheres to the "
    "`ReverseStringResponse` schema. The schema has exactly one field:\n"
    "  - `reversed` (string): the user's string read right-to-left.\n\n"
    "Rules:\n"
    "  - Output the JSON object and nothing else — no prose, no markdown "
    "fences, no explanation, no leading or trailing text.\n"
    "  - Do not add any extra fields beyond `reversed`.\n"
    "  - The value of `reversed` must have the same length and the same "
    "characters as the input, in reverse order. Preserve case."
)


class Base64DecodeResponse(BaseModel):
    plaintext: str = Field(description="The decoded plaintext of the base64 input.")


class Base64EncodeResponse(BaseModel):
    base64: str = Field(description="The base64 encoding of the user-provided plaintext.")


class ReverseStringResponse(BaseModel):
    reversed: str = Field(description="The user's string read right-to-left.")


REVERSE_LIST_SYSTEM_PROMPT: str = (
    "You are a list reverser. The user message is a JSON array of ASCII "
    "strings. Reverse the order of the items in the list and return the "
    "reversed list.\n\n"
    "You MUST respond with a single JSON object that strictly adheres to the "
    "`ReverseListResponse` schema. The schema has exactly one field:\n"
    "  - `reversed` (array of strings): the user's list with the item order "
    "reversed.\n\n"
    "Rules:\n"
    "  - Output the JSON object and nothing else — no prose, no markdown "
    "fences, no explanation, no leading or trailing text.\n"
    "  - Do not add any extra fields beyond `reversed`.\n"
    "  - Do NOT modify individual items: keep each string exactly as given "
    "(same characters, same case, same length). Only the position of each "
    "item in the list changes.\n"
    "  - The output array must have the same number of items as the input."
)


class ReverseListResponse(BaseModel):
    reversed: list[str] = Field(
        description="The user's list with the item order reversed (items themselves are unchanged)."
    )


@dataclass
class Base64Trial:
    trial_index: int
    plaintext: str
    base64_input: str
    llm_decoded: str | None
    correct: bool
    elapsed: float
    error: str | None


@dataclass
class Base64EncodeTrial:
    trial_index: int
    plaintext: str
    expected_base64: str
    llm_base64: str | None
    correct: bool
    elapsed: float
    error: str | None


@dataclass
class ReverseStringTrial:
    trial_index: int
    plaintext: str
    expected_reversed: str
    llm_reversed: str | None
    correct: bool
    elapsed: float
    error: str | None


@dataclass
class ReverseListTrial:
    trial_index: int
    items: list[str]
    expected_reversed: list[str]
    llm_reversed: list[str] | None
    correct: bool
    elapsed: float
    error: str | None


@dataclass
class BenchmarkResult:
    target_kind: str  # "config" or "override"
    target_uuid: UUID
    model_name: str
    total: int
    correct: int
    mistakes: int  # LLM responded but output mismatched the reference
    failures: int  # LLM call / schema validation raised
    trials: list[Any] = field(default_factory=list)
    # Set by the function-calling benchmarks when they give up early (too many
    # timeouts). The runner treats an aborted result as a failed benchmark.
    aborted: bool = False
    abort_reason: str | None = None


def _resolve_target(target_uuid: UUID) -> tuple[str, str, dict[str, Any]]:
    """Return (provider_id, model_name, llamaindex_kwargs) for the target uuid.
    Thin alias for db.resolved_model_kwargs (shared with the agent layer)."""
    return db.resolved_model_kwargs(target_uuid)


def _target_kind(target_uuid: UUID) -> str:
    """Return "config" or "override" for a target uuid. Used to populate
    BenchmarkResult.target_kind for display in the verdict line."""
    return "config" if db.get_model_config(target_uuid) is not None else "override"


def _random_plaintext(length: int) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(random.choices(alphabet, k=length))


def warmup(target_uuid: UUID) -> float:
    """Send a plain text-completion call to LM Studio to load / page in the
    model before the first real benchmark trial. LM Studio's first request
    for a fresh model can take ~30s to load — that latency would otherwise
    skew the first benchmark's average. Plain `complete()` is enough; no
    structured output needed.

    Returns elapsed seconds. Raises if the call fails."""
    _provider_id, model_name, args = _resolve_target(target_uuid)
    the_llm = prepare_llm(_provider_id, model_name, args)
    t0 = time.monotonic()
    the_llm.complete("hi")
    return time.monotonic() - t0


class BenchmarkBase64Decode:
    """Probes an LLM's base64 decoding accuracy.

    The target uuid may identify either a ModelConfig or a
    ModelConfigOverride. The LLM is constructed via prepare_llm, which routes
    Ollama to the native wrapper and other providers to ThinkingAwareOpenAILike
    so reasoning models (Qwen3-style "content empty, JSON in
    reasoning_content") also work."""

    def __init__(
        self,
        target_uuid: UUID,
        num_trials: int = 10,
        string_length: int = 8,
    ):
        self.target_uuid = target_uuid
        self.num_trials = num_trials
        self.string_length = string_length

    def run(
        self,
        on_trial: Callable[[Base64Trial], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> BenchmarkResult:
        _provider_id, model_name, args = _resolve_target(self.target_uuid)
        the_llm = prepare_llm(_provider_id, model_name, args)
        sllm = the_llm.as_structured_llm(Base64DecodeResponse)

        trials: list[Base64Trial] = []
        aborted = False
        abort_reason: str | None = None
        for i in range(self.num_trials):
            if should_stop is not None and should_stop():
                aborted = True
                abort_reason = "stopped by user"
                break
            plaintext = _random_plaintext(self.string_length)
            b64 = base64.b64encode(plaintext.encode("ascii")).decode("ascii")
            messages = [
                ChatMessage(role=MessageRole.SYSTEM, content=DECODE_SYSTEM_PROMPT),
                ChatMessage(role=MessageRole.USER, content=b64),
            ]
            t0 = time.monotonic()
            decoded: str | None = None
            error: str | None = None
            try:
                response = sllm.chat(messages)
                decoded = response.raw.plaintext
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
            elapsed = time.monotonic() - t0

            correct = error is None and decoded == plaintext
            trial = Base64Trial(
                trial_index=i,
                plaintext=plaintext,
                base64_input=b64,
                llm_decoded=decoded,
                correct=correct,
                elapsed=elapsed,
                error=error,
            )
            trials.append(trial)
            if on_trial is not None:
                on_trial(trial)

        return BenchmarkResult(
            target_kind=_target_kind(self.target_uuid),
            target_uuid=self.target_uuid,
            model_name=model_name,
            total=len(trials),
            correct=sum(1 for t in trials if t.correct),
            mistakes=sum(1 for t in trials if t.error is None and not t.correct),
            failures=sum(1 for t in trials if t.error is not None),
            trials=list(trials),
            aborted=aborted,
            abort_reason=abort_reason,
        )


class BenchmarkBase64Encode:
    """Probes an LLM's base64 encoding accuracy — mirror of
    BenchmarkBase64Decode in the opposite direction.

    The user prompt is a random ASCII plaintext; the LLM must respond with
    its base64 encoding (standard alphabet, including `=` padding) as the
    sole `base64` field of a Base64EncodeResponse JSON object."""

    def __init__(
        self,
        target_uuid: UUID,
        num_trials: int = 10,
        string_length: int = 8,
    ):
        self.target_uuid = target_uuid
        self.num_trials = num_trials
        self.string_length = string_length

    def run(
        self,
        on_trial: Callable[[Base64EncodeTrial], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> BenchmarkResult:
        _provider_id, model_name, args = _resolve_target(self.target_uuid)
        the_llm = prepare_llm(_provider_id, model_name, args)
        sllm = the_llm.as_structured_llm(Base64EncodeResponse)

        trials: list[Base64EncodeTrial] = []
        aborted = False
        abort_reason: str | None = None
        for i in range(self.num_trials):
            if should_stop is not None and should_stop():
                aborted = True
                abort_reason = "stopped by user"
                break
            plaintext = _random_plaintext(self.string_length)
            expected = base64.b64encode(plaintext.encode("ascii")).decode("ascii")
            messages = [
                ChatMessage(role=MessageRole.SYSTEM, content=ENCODE_SYSTEM_PROMPT),
                ChatMessage(role=MessageRole.USER, content=plaintext),
            ]
            t0 = time.monotonic()
            llm_b64: str | None = None
            error: str | None = None
            try:
                response = sllm.chat(messages)
                llm_b64 = response.raw.base64
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
            elapsed = time.monotonic() - t0

            correct = error is None and llm_b64 == expected
            trial = Base64EncodeTrial(
                trial_index=i,
                plaintext=plaintext,
                expected_base64=expected,
                llm_base64=llm_b64,
                correct=correct,
                elapsed=elapsed,
                error=error,
            )
            trials.append(trial)
            if on_trial is not None:
                on_trial(trial)

        return BenchmarkResult(
            target_kind=_target_kind(self.target_uuid),
            target_uuid=self.target_uuid,
            model_name=model_name,
            total=len(trials),
            correct=sum(1 for t in trials if t.correct),
            mistakes=sum(1 for t in trials if t.error is None and not t.correct),
            failures=sum(1 for t in trials if t.error is not None),
            trials=list(trials),
            aborted=aborted,
            abort_reason=abort_reason,
        )


class BenchmarkReverseList:
    """Probes an LLM's ability to reverse the ORDER of items in a list
    without modifying the individual items.

    Generates a random list of ASCII strings, sends it as a JSON array in
    the user message, and asks the LLM to return the list in reversed
    order. Compares against Python's `items[::-1]` with strict element-wise
    equality (so any item mutation also counts as a mistake)."""

    def __init__(
        self,
        target_uuid: UUID,
        num_trials: int = 10,
        num_items: int = 5,
        item_length: int = 4,
    ):
        self.target_uuid = target_uuid
        self.num_trials = num_trials
        self.num_items = num_items
        self.item_length = item_length

    def run(
        self,
        on_trial: Callable[[ReverseListTrial], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> BenchmarkResult:
        _provider_id, model_name, args = _resolve_target(self.target_uuid)
        the_llm = prepare_llm(_provider_id, model_name, args)
        sllm = the_llm.as_structured_llm(ReverseListResponse)

        trials: list[ReverseListTrial] = []
        aborted = False
        abort_reason: str | None = None
        for i in range(self.num_trials):
            if should_stop is not None and should_stop():
                aborted = True
                abort_reason = "stopped by user"
                break
            items = [_random_plaintext(self.item_length) for _ in range(self.num_items)]
            expected = list(reversed(items))
            user_msg = json.dumps(items)
            messages = [
                ChatMessage(role=MessageRole.SYSTEM, content=REVERSE_LIST_SYSTEM_PROMPT),
                ChatMessage(role=MessageRole.USER, content=user_msg),
            ]
            t0 = time.monotonic()
            llm_list: list[str] | None = None
            error: str | None = None
            try:
                response = sllm.chat(messages)
                llm_list = list(response.raw.reversed)
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
            elapsed = time.monotonic() - t0

            correct = error is None and llm_list == expected
            trial = ReverseListTrial(
                trial_index=i,
                items=items,
                expected_reversed=expected,
                llm_reversed=llm_list,
                correct=correct,
                elapsed=elapsed,
                error=error,
            )
            trials.append(trial)
            if on_trial is not None:
                on_trial(trial)

        return BenchmarkResult(
            target_kind=_target_kind(self.target_uuid),
            target_uuid=self.target_uuid,
            model_name=model_name,
            total=len(trials),
            correct=sum(1 for t in trials if t.correct),
            mistakes=sum(1 for t in trials if t.error is None and not t.correct),
            failures=sum(1 for t in trials if t.error is not None),
            trials=list(trials),
            aborted=aborted,
            abort_reason=abort_reason,
        )


class BenchmarkReverseString:
    """Probes an LLM's ability to reverse a string character-by-character.

    Generates random ASCII strings, asks the LLM to return each one read
    right-to-left, and compares against Python's reference `s[::-1]`. Same
    target-uuid / on_trial-callback / BenchmarkResult contract as the
    base64 benchmarks."""

    def __init__(
        self,
        target_uuid: UUID,
        num_trials: int = 10,
        string_length: int = 8,
    ):
        self.target_uuid = target_uuid
        self.num_trials = num_trials
        self.string_length = string_length

    def run(
        self,
        on_trial: Callable[[ReverseStringTrial], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> BenchmarkResult:
        _provider_id, model_name, args = _resolve_target(self.target_uuid)
        the_llm = prepare_llm(_provider_id, model_name, args)
        sllm = the_llm.as_structured_llm(ReverseStringResponse)

        trials: list[ReverseStringTrial] = []
        aborted = False
        abort_reason: str | None = None
        for i in range(self.num_trials):
            if should_stop is not None and should_stop():
                aborted = True
                abort_reason = "stopped by user"
                break
            plaintext = _random_plaintext(self.string_length)
            expected = plaintext[::-1]
            messages = [
                ChatMessage(role=MessageRole.SYSTEM, content=REVERSE_SYSTEM_PROMPT),
                ChatMessage(role=MessageRole.USER, content=plaintext),
            ]
            t0 = time.monotonic()
            llm_rev: str | None = None
            error: str | None = None
            try:
                response = sllm.chat(messages)
                llm_rev = response.raw.reversed
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
            elapsed = time.monotonic() - t0

            correct = error is None and llm_rev == expected
            trial = ReverseStringTrial(
                trial_index=i,
                plaintext=plaintext,
                expected_reversed=expected,
                llm_reversed=llm_rev,
                correct=correct,
                elapsed=elapsed,
                error=error,
            )
            trials.append(trial)
            if on_trial is not None:
                on_trial(trial)

        return BenchmarkResult(
            target_kind=_target_kind(self.target_uuid),
            target_uuid=self.target_uuid,
            model_name=model_name,
            total=len(trials),
            correct=sum(1 for t in trials if t.correct),
            mistakes=sum(1 for t in trials if t.error is None and not t.correct),
            failures=sum(1 for t in trials if t.error is not None),
            trials=list(trials),
            aborted=aborted,
            abort_reason=abort_reason,
        )


# The function-calling benchmarks below run a LlamaIndex FunctionAgent, which can
# hang on a model that loops or stalls. Cap each trial, and abandon the whole
# benchmark once too many trials time out (no point running the rest).
TOOL_CALL_TIMEOUT: float = 60.0
TIMEOUT_ABORT_THRESHOLD: int = 2


def _run_agent(agent: FunctionAgent, user_msg: str) -> None:
    """Run a FunctionAgent to completion, cancelling the in-flight run and
    raising TimeoutError if it exceeds TOOL_CALL_TIMEOUT seconds."""

    async def _go() -> None:
        await asyncio.wait_for(agent.run(user_msg=user_msg), timeout=TOOL_CALL_TIMEOUT)

    asyncio.run(_go())


TOOL_ORDER_SYSTEM_PROMPT: str = (
    "You can call three tools: `func1`, `func2`, and `func3`. They take no "
    "arguments. Call all three in the exact order the user asks, calling each "
    "exactly once. After all three calls have completed, reply with a short "
    "confirmation."
)


@dataclass
class ToolOrderTrial:
    trial_index: int
    expected_calls: list[str]
    observed_calls: list[str]
    correct: bool
    elapsed: float
    error: str | None


class BenchmarkToolOrder:
    """Probes whether a function-calling model invokes three tools in the
    requested order.

    Each trial hands the model three no-op tools (`func1`, `func2`, `func3`) via
    a LlamaIndex FunctionAgent and asks it, in one prompt, to call them in a
    specific order. The order is one of the 3! = 6 permutations; the permutations
    are shuffled and one is used per trial (so the default 5 trials exercise 5 of
    the 6, and a model can't pass by always emitting the same order). The tools
    append their name to a per-trial list as they run, so the trial is `correct`
    iff the observed sequence exactly matches the requested permutation (each of
    the three called once, in order).

    Requires a function-calling target (is_function_calling_model=True);
    FunctionAgent rejects a model that doesn't advertise tool use, which is
    captured as a per-trial failure."""

    def __init__(self, target_uuid: UUID, num_trials: int = 5):
        self.target_uuid = target_uuid
        self.num_trials = num_trials

    def run(
        self,
        on_trial: Callable[[ToolOrderTrial], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> BenchmarkResult:
        _provider_id, model_name, args = _resolve_target(self.target_uuid)

        # All 6 orderings of the three tools, shuffled; each trial uses one
        # (so the default 5 trials exercise 5 of the 6 permutations).
        perms = [list(p) for p in itertools.permutations(["func1", "func2", "func3"])]
        random.shuffle(perms)
        orders = [perms[i % len(perms)] for i in range(self.num_trials)]

        trials: list[ToolOrderTrial] = []
        timeouts = 0
        aborted = False
        abort_reason: str | None = None
        for i in range(self.num_trials):
            if should_stop is not None and should_stop():
                aborted = True
                abort_reason = "stopped by user"
                break
            expected = orders[i]
            a, b, c = expected
            calls: list[str] = []

            def func1() -> str:
                """Records that func1 was called."""
                calls.append("func1")
                return "func1 done"

            def func2() -> str:
                """Records that func2 was called."""
                calls.append("func2")
                return "func2 done"

            def func3() -> str:
                """Records that func3 was called."""
                calls.append("func3")
                return "func3 done"

            t0 = time.monotonic()
            error: str | None = None
            timed_out = False
            try:
                the_llm = prepare_llm(_provider_id, model_name, args)
                agent = FunctionAgent(
                    tools=[func1, func2, func3],
                    llm=the_llm,
                    system_prompt=TOOL_ORDER_SYSTEM_PROMPT,
                )
                _run_agent(
                    agent,
                    f"Call the tools in exactly this order: {a}, then {b}, then {c}. Call each exactly once.",
                )
            except (asyncio.TimeoutError, TimeoutError):
                timed_out = True
                error = f"timed out after {TOOL_CALL_TIMEOUT:g}s"
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
            elapsed = time.monotonic() - t0

            observed = list(calls)
            correct = error is None and observed == expected
            trial = ToolOrderTrial(
                trial_index=i,
                expected_calls=list(expected),
                observed_calls=observed,
                correct=correct,
                elapsed=elapsed,
                error=error,
            )
            trials.append(trial)
            if on_trial is not None:
                on_trial(trial)

            if timed_out:
                timeouts += 1
                if timeouts >= TIMEOUT_ABORT_THRESHOLD:
                    aborted = True
                    abort_reason = (
                        f"{timeouts} trials timed out after {TOOL_CALL_TIMEOUT:g}s; "
                        f"aborted with {self.num_trials - len(trials)} trial(s) unrun"
                    )
                    break

        return BenchmarkResult(
            target_kind=_target_kind(self.target_uuid),
            target_uuid=self.target_uuid,
            model_name=model_name,
            total=len(trials),
            correct=sum(1 for t in trials if t.correct),
            mistakes=sum(1 for t in trials if t.error is None and not t.correct),
            failures=sum(1 for t in trials if t.error is not None),
            trials=list(trials),
            aborted=aborted,
            abort_reason=abort_reason,
        )


TOOL_ROUTE_SYSTEM_PROMPT: str = (
    "You can call three tools: `random`, `func1`, and `func2`. They take no "
    "arguments. First call `random` exactly once — it returns the NAME of a "
    "function, either \"func1\" or \"func2\". Then call exactly that one named "
    "function (and not the other one). After that tool call completes, reply "
    "with a short confirmation."
)


def _choose_func_target() -> str:
    """Pick which function `random` should route to this trial."""
    return random.choice(["func1", "func2"])


@dataclass
class ToolRouteTrial:
    trial_index: int
    routed_to: str            # the name `random` returned this trial
    expected_calls: list[str]  # ["random", routed_to]
    observed_calls: list[str]
    correct: bool
    elapsed: float
    error: str | None


class BenchmarkToolRoute:
    """Probes whether a function-calling model can dispatch on a tool's output.

    Each trial gives the model three no-op tools via a LlamaIndex FunctionAgent:
    `random` (returns the NAME of a function — "func1" or "func2", chosen
    randomly), and `func1` / `func2`. The model must call `random` first, read
    which function it named, then call exactly that function. The tools record
    their name as they run, so the trial is `correct` iff the observed sequence
    is exactly [random, <name random returned>] (the wrong function counts as a
    mistake).

    Unlike BenchmarkToolOrder, the second call depends on the first call's
    result, so this exercises sequential, data-dependent tool use.

    Requires a function-calling target (is_function_calling_model=True);
    FunctionAgent rejects a model that doesn't advertise tool use, which is
    captured as a per-trial failure."""

    def __init__(self, target_uuid: UUID, num_trials: int = 5):
        self.target_uuid = target_uuid
        self.num_trials = num_trials

    def run(
        self,
        on_trial: Callable[[ToolRouteTrial], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> BenchmarkResult:
        _provider_id, model_name, args = _resolve_target(self.target_uuid)

        trials: list[ToolRouteTrial] = []
        timeouts = 0
        aborted = False
        abort_reason: str | None = None
        for i in range(self.num_trials):
            if should_stop is not None and should_stop():
                aborted = True
                abort_reason = "stopped by user"
                break
            calls: list[str] = []
            routed_to = _choose_func_target()

            def random() -> str:
                """Call this FIRST. Returns the name of the function to call next."""
                calls.append("random")
                return routed_to

            def func1() -> str:
                """Call this only if `random` returned "func1"."""
                calls.append("func1")
                return "func1 done"

            def func2() -> str:
                """Call this only if `random` returned "func2"."""
                calls.append("func2")
                return "func2 done"

            t0 = time.monotonic()
            error: str | None = None
            timed_out = False
            try:
                the_llm = prepare_llm(_provider_id, model_name, args)
                agent = FunctionAgent(
                    tools=[random, func1, func2],
                    llm=the_llm,
                    system_prompt=TOOL_ROUTE_SYSTEM_PROMPT,
                )
                _run_agent(
                    agent,
                    "Call random first, then call exactly the function it names.",
                )
            except (asyncio.TimeoutError, TimeoutError):
                timed_out = True
                error = f"timed out after {TOOL_CALL_TIMEOUT:g}s"
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
            elapsed = time.monotonic() - t0

            observed = list(calls)
            expected = ["random", routed_to]
            correct = error is None and observed == expected
            trial = ToolRouteTrial(
                trial_index=i,
                routed_to=routed_to,
                expected_calls=expected,
                observed_calls=observed,
                correct=correct,
                elapsed=elapsed,
                error=error,
            )
            trials.append(trial)
            if on_trial is not None:
                on_trial(trial)

            if timed_out:
                timeouts += 1
                if timeouts >= TIMEOUT_ABORT_THRESHOLD:
                    aborted = True
                    abort_reason = (
                        f"{timeouts} trials timed out after {TOOL_CALL_TIMEOUT:g}s; "
                        f"aborted with {self.num_trials - len(trials)} trial(s) unrun"
                    )
                    break

        return BenchmarkResult(
            target_kind=_target_kind(self.target_uuid),
            target_uuid=self.target_uuid,
            model_name=model_name,
            total=len(trials),
            correct=sum(1 for t in trials if t.correct),
            mistakes=sum(1 for t in trials if t.error is None and not t.correct),
            failures=sum(1 for t in trials if t.error is not None),
            trials=list(trials),
            aborted=aborted,
            abort_reason=abort_reason,
        )


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a]
    encode_mode = "--encode" in args
    reverse_mode = "--reverse" in args
    reverse_list_mode = "--reverse-list" in args
    tool_order_mode = "--tool-order" in args
    tool_route_mode = "--tool-route" in args
    args = [a for a in args if a not in ("--encode", "--reverse", "--reverse-list", "--tool-order", "--tool-route")]
    if sum(int(x) for x in (encode_mode, reverse_mode, reverse_list_mode, tool_order_mode, tool_route_mode)) > 1:
        raise SystemExit(
            "pick at most one of --encode / --reverse / --reverse-list / --tool-order / --tool-route"
        )

    app = db.make_app()
    db.init_db(app)
    with app.app_context():
        if args:
            target = UUID(args[0])
        else:
            configs = [c for c in db.list_model_configs() if c.available]
            if not configs:
                raise SystemExit("no available model configs to benchmark against")
            target = configs[0].uuid
            print(f"(no uuid given; using {configs[0].model_name} -> {target})")

        if encode_mode:
            def progress_e(trial: Base64EncodeTrial) -> None:
                mark = "✓" if trial.correct else ("✗" if trial.error is None else "!")
                line = (
                    f"  [{trial.trial_index + 1:02d}] {mark} "
                    f"plaintext={trial.plaintext!r:>12} "
                    f"expected={trial.expected_base64!r:>16} "
                    f"got={trial.llm_base64!r:>16} "
                    f"({trial.elapsed:.2f}s)"
                )
                if trial.error:
                    line += f"  ERROR={trial.error}"
                print(line, flush=True)

            print(f"=== BenchmarkBase64Encode ===")
            result = BenchmarkBase64Encode(target, num_trials=5, string_length=6).run(
                on_trial=progress_e
            )
        elif reverse_list_mode:
            def progress_rl(trial: ReverseListTrial) -> None:
                mark = "✓" if trial.correct else ("✗" if trial.error is None else "!")
                line = (
                    f"  [{trial.trial_index + 1:02d}] {mark} "
                    f"items={trial.items} "
                    f"expected={trial.expected_reversed} "
                    f"got={trial.llm_reversed} "
                    f"({trial.elapsed:.2f}s)"
                )
                if trial.error:
                    line += f"  ERROR={trial.error}"
                print(line, flush=True)

            print(f"=== BenchmarkReverseList ===")
            result = BenchmarkReverseList(
                target, num_trials=5, num_items=5, item_length=4
            ).run(on_trial=progress_rl)
        elif tool_order_mode:
            def progress_to(trial: ToolOrderTrial) -> None:
                mark = "✓" if trial.correct else ("✗" if trial.error is None else "!")
                line = (
                    f"  [{trial.trial_index + 1:02d}] {mark} "
                    f"expected={trial.expected_calls} "
                    f"observed={trial.observed_calls} "
                    f"({trial.elapsed:.2f}s)"
                )
                if trial.error:
                    line += f"  ERROR={trial.error}"
                print(line, flush=True)

            print(f"=== BenchmarkToolOrder ===")
            result = BenchmarkToolOrder(target, num_trials=5).run(on_trial=progress_to)
        elif tool_route_mode:
            def progress_tr(trial: ToolRouteTrial) -> None:
                mark = "✓" if trial.correct else ("✗" if trial.error is None else "!")
                line = (
                    f"  [{trial.trial_index + 1:02d}] {mark} "
                    f"routed_to={trial.routed_to} "
                    f"observed={trial.observed_calls} "
                    f"({trial.elapsed:.2f}s)"
                )
                if trial.error:
                    line += f"  ERROR={trial.error}"
                print(line, flush=True)

            print(f"=== BenchmarkToolRoute ===")
            result = BenchmarkToolRoute(target, num_trials=5).run(on_trial=progress_tr)
        elif reverse_mode:
            def progress_r(trial: ReverseStringTrial) -> None:
                mark = "✓" if trial.correct else ("✗" if trial.error is None else "!")
                line = (
                    f"  [{trial.trial_index + 1:02d}] {mark} "
                    f"plaintext={trial.plaintext!r:>12} "
                    f"expected={trial.expected_reversed!r:>12} "
                    f"got={trial.llm_reversed!r:>12} "
                    f"({trial.elapsed:.2f}s)"
                )
                if trial.error:
                    line += f"  ERROR={trial.error}"
                print(line, flush=True)

            print(f"=== BenchmarkReverseString ===")
            result = BenchmarkReverseString(target, num_trials=5, string_length=6).run(
                on_trial=progress_r
            )
        else:
            def progress_d(trial: Base64Trial) -> None:
                mark = "✓" if trial.correct else ("✗" if trial.error is None else "!")
                line = (
                    f"  [{trial.trial_index + 1:02d}] {mark} "
                    f"plaintext={trial.plaintext!r:>12} "
                    f"decoded={trial.llm_decoded!r:>12} "
                    f"({trial.elapsed:.2f}s)"
                )
                if trial.error:
                    line += f"  ERROR={trial.error}"
                print(line, flush=True)

            print(f"=== BenchmarkBase64Decode ===")
            result = BenchmarkBase64Decode(target, num_trials=5, string_length=6).run(
                on_trial=progress_d
            )

        print(
            f"\nverdict: model={result.model_name} kind={result.target_kind} "
            f"uuid={result.target_uuid}"
        )
        print(
            f"  total={result.total} correct={result.correct} "
            f"mistakes={result.mistakes} failures={result.failures}"
        )
        if result.aborted:
            print(f"  ABORTED: {result.abort_reason}")
