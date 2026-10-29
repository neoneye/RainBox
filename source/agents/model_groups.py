"""Which model group a call runs on, and how the call is made.

Two concerns that every structured call in the system shares, and that no
single agent owns: resolving a binding chain to a group's members
(`resolve_*`), and making one structured-output call against those members
with fallback (`structured_llm_call`).

They live apart from any agent because the callers are not agents. The
assistant's recall filter, second opinion, reply audit and language classifier
all come through here, as do /memory/developer and the eval harnesses — none
of which is a worker the supervisor spawns.
"""

import logging
import time
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from llama_index.core.llms import ChatMessage, MessageRole
from pydantic import BaseModel

import db
from llm import prepare_llm

logger = logging.getLogger(__name__)


def resolve_model_group(
    candidates: list[tuple[UUID, str]],
) -> tuple[UUID, str] | tuple[None, None]:
    """The generic binding-chain resolver: the first of `candidates`
    (`(agent_uuid, label)` pairs, tried in order) with a non-empty bound model
    group. Returns `(group_uuid, label)`, or `(None, None)` when nothing is
    bound anywhere. Callers own their chain — the assistant's is always
    `<slot> -> assistant.default` (`resolve_assistant_model_group`); this
    agent passes its own binding directly."""
    for agent_uuid, label in candidates:
        binding = db.get_agent_model_binding(agent_uuid)
        if binding is None or binding.model_group_uuid is None:
            continue
        if db.get_model_group_member_uuids(binding.model_group_uuid):
            return binding.model_group_uuid, label
    return None, None


def resolve_assistant_model_group(
    slot_uuid: UUID,
) -> tuple[UUID, str] | tuple[None, None]:
    """Model group for one assistant model call: the `assistant.*` slot bound
    to that call, else `assistant.default`.

    Two links, the same for every call the assistant makes — so binding only
    the default configures the whole assistant, and binding one slot moves
    exactly one call. The returned label is the slot's own name, which is also
    its /activity caller tag, so the trace says which row on /agentmodel
    answered rather than a word that has to be mapped back to one."""
    from agents.config import ASSISTANT_DEFAULT_UUID, role_name

    chain = [(slot_uuid, role_name(slot_uuid) or str(slot_uuid))]
    if slot_uuid != ASSISTANT_DEFAULT_UUID:
        chain.append((ASSISTANT_DEFAULT_UUID, "assistant.default"))
    return resolve_model_group(chain)



def resolve_assistant_model_uuids(
    slot_uuid: UUID,
) -> tuple[list[UUID], str] | tuple[None, None]:
    """`resolve_assistant_model_group`, unpacked to the group's priority-ordered
    member uuids — what `structured_llm_call` consumes."""
    group_uuid, label = resolve_assistant_model_group(slot_uuid)
    if group_uuid is None or label is None:
        return None, None
    return db.get_model_group_member_uuids(group_uuid), label


def structured_llm_call(
    agent_name: str,
    candidate_model_uuids: list[UUID],
    system_prompt: str,
    user_prompt: str,
    response_model: type[BaseModel],
    usage_out: dict[str, Any] | None = None,
    attempts_out: list[dict[str, Any]] | None = None,
    observer: Any = None,
) -> tuple[BaseModel, UUID]:
    """One structured-output call, falling back through a model group's members
    on failure. Returns (result, answering_model_uuid); raises RuntimeError when
    no group is bound or every member fails. Free function (not an agent method)
    so non-agent callers — the assistant's memory_query recall filter, the
    /memory/developer page — can make the same call against any group's members.

    `agent_name` labels log/error messages AND becomes the call's caller label
    on /activity. Without the tag, attribution walks the stack for the
    innermost application frame (llm.activity.call_origin) — which is this
    function — so without the tag every call through here reports as one
    indistinguishable row, whoever actually made it. Four different assistant
    jobs (recall filter, second opinion, reply audit, language classifier) come
    through this helper.

    Pass `usage_out` to have this call's cost written into it as
    {input, output, ms, requested_at} — StructuredLLMAgent's `_last_usage`
    plus when the answering member was asked, which after a fallback is not
    when the call began. An out-parameter rather than a wider return type
    because every existing caller ignores usage, and only the succeeding
    member's cost is recorded there.

    Pass `attempts_out` to have every member that failed appended to it, in
    the shape of a step row's `rejected_attempts` with `outcome: "failed"`:
    its model, when it was asked, how long it ran, the error, and the
    reasoning and response it had streamed when it failed. A member that
    times out mid-generation is the case this exists for — its seconds are
    otherwise a gap in the run's trace, and what it was writing when it was
    cut off is the only evidence of why.

    The answer is consumed as a stream, as `ModelGroupAgent` does, for the
    same two reasons: the tokens arrive incrementally, so a cut-off member
    still leaves its partial text in the instrumentation tally; and the
    member's timeout bounds the whole stream as a wall-clock deadline, where
    a per-read timeout never trips on a model that keeps generating.

    Pass `observer` to watch the call while it runs: its `started(model_uuid,
    model_name, timeout_seconds)` as each member is asked, `progress(model_uuid,
    model_name, reasoning, response_text)` as its stream grows, and
    `failed(model_uuid, model_name, error, reasoning, response_text)` when it
    fails. The assistant turns these into its run's in-flight checkpoint, so
    the page shows the member being asked right now, not the call before it."""
    if not candidate_model_uuids:
        raise RuntimeError(
            f"agent {agent_name} has no model group bound (assign one on /agentmodel)"
        )
    messages = [
        ChatMessage(role=MessageRole.SYSTEM, content=system_prompt),
        ChatMessage(role=MessageRole.USER, content=user_prompt),
    ]
    last_error: Exception | None = None
    # Per-call token accounting, same pattern as StructuredLLMAgent: a
    # TokenCountingHandler on the structured LLM sees input/output tokens even
    # though `.raw` is the parsed model, not a usage dict.
    from llama_index.core.callbacks import CallbackManager, TokenCountingHandler

    from llama_index.core.instrumentation.dispatcher import instrument_tags

    from agents.base import (ModelGroupAgent, WhitespaceRunaway,
                             check_whitespace_runaway, truncate_middle)
    from llm import capture_reasoning

    for model_uuid in candidate_model_uuids:
        # A member that got stuck padding whitespace is asked again before
        # the group moves on (ModelGroupAgent.WHITESPACE_RUNAWAY_RETRIES):
        # the padding is a sampling accident, so the same request usually
        # comes back whole, sooner than a next member that may need loading.
        for try_index in range(1 + ModelGroupAgent.WHITESPACE_RUNAWAY_RETRIES):
            # Per member: one counter across a fallback charges the answering
            # member for the failed one's prompt too.
            token_counter = TokenCountingHandler()
            attempt_at = datetime.now(UTC)
            t0 = time.monotonic()
            model_name = ""
            reasoning: str | None = None
            response_text: str | None = None
            try:
                _provider_id, model_name, args = db.resolved_model_kwargs(model_uuid)
                timeout_s = float(args.get("request_timeout") or args.get("timeout") or 60.0)
                if observer is not None:
                    observer.started(model_uuid, model_name, timeout_s)
                # Fail fast on a down/unreachable provider: the OpenAI client's
                # default exponential backoff (max_retries=10) turns one outage
                # into a ~30s+ hang per model with no UI feedback. We already
                # fall back across the group's members, so per-model retries add
                # latency without improving the odds. (Native-Ollama drops the
                # key in prepare_llm's field filter, so this is a no-op there.)
                args = {**args, "max_retries": 0}
                the_llm = prepare_llm(_provider_id, model_name, args)
                sllm = the_llm.as_structured_llm(
                    response_model, callback_manager=CallbackManager([token_counter])
                )
                deadline = time.monotonic() + timeout_s
                last = None
                with instrument_tags({"caller": agent_name}), \
                        capture_reasoning() as tally:
                    try:
                        for last in sllm.stream_chat(messages):
                            raw_text = (tally.content_text
                                        or getattr(getattr(last, "message", None),
                                                   "content", None)
                                        or "")
                            if observer is not None:
                                observer.progress(
                                    model_uuid, model_name,
                                    tally.reasoning_text.strip() or None,
                                    raw_text.strip() or None)
                            check_whitespace_runaway(raw_text)
                            if time.monotonic() > deadline:
                                raise TimeoutError(
                                    f"structured stream exceeded {timeout_s:.0f}s "
                                    "(model still generating)")
                    finally:
                        reasoning = tally.reasoning_text.strip() or None
                        response_text = (
                            tally.content_text
                            or getattr(getattr(last, "message", None), "content", None)
                            or "").strip() or None
                if last is None:
                    raise RuntimeError("structured stream produced no response")
                result = ModelGroupAgent._settle_structured_result(
                    response_model, cast(BaseModel, last.raw), response_text)
                if usage_out is not None:
                    usage_out.update({
                        "input": token_counter.prompt_llm_token_count,
                        "output": token_counter.completion_llm_token_count,
                        "ms": int((time.monotonic() - t0) * 1000),
                        "requested_at": attempt_at.isoformat(),
                    })
                return result, model_uuid
            except Exception as e:
                last_error = e
                logger.warning(
                    "agent %s: model %s failed (%s); trying next in group",
                    agent_name, model_uuid, e,
                )
                if observer is not None:
                    try:
                        observer.failed(model_uuid, model_name, e,
                                        reasoning, response_text)
                    except Exception:   # noqa: BLE001 — watching must not cost the call
                        logger.warning("agent %s: observer failed", agent_name,
                                       exc_info=True)
                if attempts_out is not None:
                    attempts_out.append({
                        "outcome": "failed",
                        "model_uuid": str(model_uuid),
                        "model_name": model_name,
                        "requested_at": attempt_at.isoformat(),
                        "ms": int((time.monotonic() - t0) * 1000),
                        "input_tokens": token_counter.prompt_llm_token_count,
                        "output_tokens": token_counter.completion_llm_token_count,
                        "reasoning": truncate_middle(
                            reasoning or "",
                            ModelGroupAgent.REJECTED_ATTEMPT_REASONING_CHARS) or None,
                        "response": truncate_middle(
                            response_text or "",
                            ModelGroupAgent.REJECTED_RESPONSE_ECHO_CHARS) or None,
                        "error": f"{type(e).__name__}: {e}",
                    })
                if (isinstance(e, WhitespaceRunaway)
                        and try_index < ModelGroupAgent.WHITESPACE_RUNAWAY_RETRIES):
                    continue
            break
    raise RuntimeError(
        f"agent {agent_name}: all {len(candidate_model_uuids)} models "
        f"in the group failed; last error: {last_error}"
    )


def assistant_slot_uuids() -> list[UUID]:
    """Every `assistant.*` model slot: one per model call the assistant makes."""
    from agents.config import (
        ASSISTANT_ACCEPTANCE_CRITERIA_UUID,
        ASSISTANT_DECIDE_UUID,
        ASSISTANT_DEFAULT_UUID,
        ASSISTANT_MEMORY_FILTER_UUID,
        ASSISTANT_REPLY_AUDIT_UUID,
        ASSISTANT_REQUEST_SUMMARY_UUID,
        ASSISTANT_RESPONSE_LANGUAGE_CLASSIFIER_UUID,
        ASSISTANT_RUN_SUMMARIZER_UUID,
        ASSISTANT_SECOND_OPINION_UUID,
    )

    return [ASSISTANT_DEFAULT_UUID, ASSISTANT_DECIDE_UUID, ASSISTANT_ACCEPTANCE_CRITERIA_UUID,
            ASSISTANT_REQUEST_SUMMARY_UUID, ASSISTANT_MEMORY_FILTER_UUID,
            ASSISTANT_SECOND_OPINION_UUID, ASSISTANT_REPLY_AUDIT_UUID,
            ASSISTANT_RESPONSE_LANGUAGE_CLASSIFIER_UUID, ASSISTANT_RUN_SUMMARIZER_UUID]


def model_base_url(member_uuid: UUID) -> str | None:
    """The endpoint a model config (or override) actually calls: its own
    `base_url`/`api_base` argument, else its provider's base URL. None when
    the member cannot be resolved."""
    from providers import registry

    try:
        provider_id, _model, kwargs = db.resolved_model_kwargs(member_uuid)
    except Exception:   # noqa: BLE001 — unresolvable counts as not-local
        return None
    url = kwargs.get("base_url") or kwargs.get("api_base")
    if url:
        return str(url)
    try:
        return registry.get(provider_id).base_url()
    except Exception:   # noqa: BLE001
        return None


def assistant_models_all_local() -> tuple[bool, str | None]:
    """(True, None) when every member of every assistant slot's resolved
    group calls a loopback host; else (False, the first offending slot's
    label). Fallback members count: a group falls back across them. Diary
    text may reach only such models (proposal §1, model locality)."""
    from agents.config import role_name
    from diary.embeddings import is_loopback_url

    for slot in assistant_slot_uuids():
        members, _label = resolve_assistant_model_uuids(slot)
        for member in members or []:
            url = model_base_url(member)
            if url is None or not is_loopback_url(url):
                return False, role_name(slot) or str(slot)
    return True, None
