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
from typing import Protocol, cast
from uuid import UUID

from pydantic import BaseModel

import db

logger = logging.getLogger(__name__)


class Caller(Protocol):
    def structured(
        self, system_prompt: str, user_prompt: str, response_model: type[BaseModel]
    ) -> BaseModel: ...

    def plain(self, system_prompt: str, user_prompt: str) -> str: ...


class ModelCaller:
    def __init__(self, model_group: str) -> None:
        self.group_uuid = _resolve_group_uuid(model_group)
        self.candidate_model_uuids: list[UUID] = db.get_model_group_member_uuids(
            self.group_uuid
        )
        if not self.candidate_model_uuids:
            raise RuntimeError(
                f"model group {model_group!r} has no members; add models to it "
                "on the /models page"
            )

    def structured(
        self, system_prompt: str, user_prompt: str, response_model: type[BaseModel]
    ) -> BaseModel:
        def call(the_llm, args) -> BaseModel:
            sllm = the_llm.as_structured_llm(response_model)
            timeout_s = float(
                args.get("request_timeout") or args.get("timeout") or 60.0
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

        return self._with_fallback(call)

    def plain(self, system_prompt: str, user_prompt: str) -> str:
        def call(the_llm, args) -> str:
            response = the_llm.chat(self._messages(system_prompt, user_prompt))
            text = str(response.message.content or "").strip()
            if not text:
                raise RuntimeError("model returned an empty reply")
            return text

        return self._with_fallback(call)

    def _messages(self, system_prompt: str, user_prompt: str):
        from llama_index.core.llms import ChatMessage, MessageRole

        return [
            ChatMessage(role=MessageRole.SYSTEM, content=system_prompt),
            ChatMessage(role=MessageRole.USER, content=user_prompt),
        ]

    def _with_fallback(self, call):
        import llm

        last_error: Exception | None = None
        for model_uuid in self.candidate_model_uuids:
            try:
                provider_id, model_name, args = db.resolved_model_kwargs(model_uuid)
                the_llm = llm.prepare_llm(provider_id, model_name, args)
                return call(the_llm, args)
            except Exception as exc:
                logger.warning("research model %s failed: %s", model_uuid, exc)
                last_error = exc
        raise RuntimeError(
            "all models in the research model group failed"
        ) from last_error


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
