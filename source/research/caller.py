"""LLM access for the research pipeline: one class, two call shapes.

ModelCaller resolves a model group (by name or uuid) and runs every call
through the group's members in priority order, falling through on any
failure — the same fallback contract as agents/base.py, without the
agent-process machinery. `structured` uses as_structured_llm with the
wall-clock-deadline streaming pattern; `plain` is a plain chat for prose
stages (structured output over long prose hurts local models)."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Protocol, cast
from uuid import UUID

from pydantic import BaseModel

import db

if TYPE_CHECKING:
    from research.telemetry import Telemetry

logger = logging.getLogger(__name__)

# Research calls read whole sources and run reasoning models, so a chat-tuned
# per-model timeout (often 60s) times out routinely. This floor is applied to
# every member's resolved timeout; a configured value ABOVE it is kept.
DEFAULT_TIMEOUT_S = 120.0


class Caller(Protocol):
    def structured(
        self, system_prompt: str, user_prompt: str, response_model: type[BaseModel]
    ) -> BaseModel: ...

    def plain(self, system_prompt: str, user_prompt: str) -> str: ...


class ModelCaller:
    def __init__(
        self,
        model_group: str,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        telemetry: "Telemetry | None" = None,
    ) -> None:
        self.timeout_s = float(timeout_s)
        self.telemetry = telemetry
        self.group_uuid = _resolve_group_uuid(model_group)
        self.candidate_model_uuids: list[UUID] = db.get_model_group_member_uuids(
            self.group_uuid
        )
        if not self.candidate_model_uuids:
            raise RuntimeError(
                f"model group {model_group!r} has no members; add models to it "
                "on the /models page"
            )

    def describe_models(self) -> list[dict]:
        """Resolved settings of every group member, in fallback order — the
        telemetry header row, so a run's KPIs can be read next to the exact
        configs (context window, timeouts, overrides) that produced them."""
        described = []
        for model_uuid in self.candidate_model_uuids:
            provider_id, model_name, args = db.resolved_model_kwargs(model_uuid)
            described.append(
                {
                    "uuid": str(model_uuid),
                    "provider": provider_id,
                    "model": model_name,
                    "arguments": args,
                }
            )
        return described

    def structured(
        self, system_prompt: str, user_prompt: str, response_model: type[BaseModel]
    ) -> BaseModel:
        def call(the_llm, args) -> BaseModel:
            sllm = the_llm.as_structured_llm(response_model)
            timeout_s = float(
                args.get("request_timeout") or args.get("timeout") or self.timeout_s
            )
            deadline = time.monotonic() + timeout_s
            last = None
            for last in sllm.stream_chat(self._messages(system_prompt, user_prompt)):
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        f"structured stream exceeded {timeout_s:.0f}s "
                        "(model still generating)"
                    )
            if last is None or last.raw is None:
                raise RuntimeError("structured stream produced no response")
            return cast(BaseModel, last.raw)

        return self._with_fallback(call, "structured", system_prompt)

    def plain(self, system_prompt: str, user_prompt: str) -> str:
        def call(the_llm, args) -> str:
            response = the_llm.chat(self._messages(system_prompt, user_prompt))
            text = str(response.message.content or "").strip()
            if not text:
                raise RuntimeError("model returned an empty reply")
            return text

        return self._with_fallback(call, "plain", system_prompt)

    def _messages(self, system_prompt: str, user_prompt: str):
        from llama_index.core.llms import ChatMessage, MessageRole

        return [
            ChatMessage(role=MessageRole.SYSTEM, content=system_prompt),
            ChatMessage(role=MessageRole.USER, content=user_prompt),
        ]

    def _with_fallback(self, call, kind: str, system_prompt: str):
        import llm

        attempts: list[dict] = []
        t_start = time.monotonic()
        last_error: Exception | None = None
        for model_uuid in self.candidate_model_uuids:
            t0 = time.monotonic()
            model_name = str(model_uuid)
            try:
                provider_id, model_name, args = db.resolved_model_kwargs(model_uuid)
                args = _apply_timeout_floor(provider_id, dict(args), self.timeout_s)
                the_llm = llm.prepare_llm(provider_id, model_name, args)
                result = call(the_llm, args)
                attempts.append({"model": model_name, "ms": _ms(t0), "error": None})
                self._record_call(kind, system_prompt, model_name, t_start, attempts)
                return result
            except Exception as exc:
                logger.warning("research model %s failed: %s", model_uuid, exc)
                attempts.append({"model": model_name, "ms": _ms(t0), "error": str(exc)})
                last_error = exc
        self._record_call(kind, system_prompt, None, t_start, attempts)
        raise RuntimeError(
            "all models in the research model group failed"
        ) from last_error

    def _record_call(
        self,
        kind: str,
        system_prompt: str,
        served_by: str | None,
        t_start: float,
        attempts: list[dict],
    ) -> None:
        if self.telemetry is None:
            return
        from research.telemetry import label_for

        self.telemetry.record(
            {
                "event": "llm_call",
                "label": label_for(system_prompt),
                "kind": kind,
                "served_by": served_by,
                "ms": _ms(t_start),
                "attempts": attempts,
            }
        )


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


def _apply_timeout_floor(provider_id: str, args: dict, floor_s: float) -> dict:
    """Raise the model's configured timeout to at least `floor_s`; never
    lower a higher value. Ollama's native wrapper names the knob
    `request_timeout`; every other provider is OpenAI-compat with `timeout`
    (see llm.prepare_llm)."""
    key = "request_timeout" if provider_id == "ollama" else "timeout"
    current = args.get(key)
    if current is None or float(current) < floor_s:
        args[key] = floor_s
    return args


def _resolve_group_uuid(model_group: str) -> UUID:
    try:
        return UUID(model_group)
    except ValueError:
        pass
    groups = db.list_model_groups()
    for group in groups:
        if group.name == model_group:
            return group.uuid
    names = sorted(group.name for group in groups)
    raise RuntimeError(
        f"model group {model_group!r} not found; available groups: {names}"
    )
