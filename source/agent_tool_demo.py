"""Tool-demo agent — a chat agent that answers with a LlamaIndex FunctionAgent.

Based on agent_chat.py, but instead of a single structured-output call it runs a
llama_index `FunctionAgent` equipped with a `multiply` tool. It reads one
chatroom's history (room_uuid from the inbox payload), feeds that transcript to
the FunctionAgent as the user message, lets the model call `multiply` as needed,
and posts the agent's text reply back into the room as its own chat_user (which
shows up live via SSE).

FunctionAgent is async, so handle() bridges into it with asyncio.run. The model
still comes from the agent's bound model group (ModelGroupAgent.setup), tried in
priority order — the same fallback shape as StructuredLLMAgent._structured_call.
"""

import asyncio
import logging
import time
from typing import Any
from uuid import UUID

from llama_index.core.agent.workflow import FunctionAgent

import db
from agent import ModelGroupAgent, StatusSender
from chat_transcript import format_history
from llama_index.core.llms import LLM
from llm import prepare_llm

logger = logging.getLogger(__name__)


# A simple calculator tool.
def multiply(a: float, b: float) -> float:
    """Useful for multiplying two numbers."""
    return a * b


TOOL_DEMO_SYSTEM_PROMPT: str = """\
You are replying in a group chat.

You will receive an IRC-style chat transcript:
1. Optional chat history, oldest first.
2. A clearly marked Current message at the bottom.

Reply only to the Current message, using the same language it uses. You have a
`multiply` tool that multiplies two numbers — call it whenever the user asks for
a product or your answer depends on multiplying numbers, and use its result in
your reply. Respond with a normal chat message; do not wrap it in code fences."""


class ToolDemoAgent(ModelGroupAgent):
    """Reads a chatroom's history (room_uuid from the inbox payload), answers the
    latest message with a FunctionAgent that can call the `multiply` tool, and
    posts that reply back into the room as its own chat_user."""

    def __init__(self, agent_uuid: UUID, name: str, send: StatusSender) -> None:
        super().__init__(agent_uuid, name, send)
        self.system_prompt = TOOL_DEMO_SYSTEM_PROMPT

    def setup(self) -> None:
        super().setup()  # resolves the bound model group + candidate models
        group = (
            db.get_model_group(self.model_group_uuid)
            if self.model_group_uuid is not None
            else None
        )
        self.group_requires_function_calling = bool(
            group is not None and group.requires_function_calling
        )
        if not self.group_requires_function_calling:
            logger.warning(
                "agent %s: its model group does not have the function-calling "
                "constraint — members may not support tool calls; bind it to a "
                "group created with the 'function calling' checkbox",
                self.name,
            )

    @staticmethod
    def _room_uuid(payload: dict[str, Any]) -> UUID:
        raw = payload.get("room_uuid")
        if not raw:
            raise ValueError("tool-demo agent payload missing 'room_uuid'")
        return raw if isinstance(raw, UUID) else UUID(str(raw))

    def user_prompt(self, payload: dict[str, Any]) -> str:
        return format_history(db.list_room_messages(self._room_uuid(payload)))

    async def _arun(self, the_llm: LLM, user_prompt: str) -> str:
        """One FunctionAgent run: the model may call `multiply` before replying.
        str(AgentOutput) is the final reply text (response.content)."""
        agent = FunctionAgent(
            tools=[multiply], llm=the_llm, system_prompt=self.system_prompt
        )
        output = await agent.run(user_msg=user_prompt)
        return str(output).strip()

    def _run_function_agent(self, user_prompt: str) -> str:
        """Run the FunctionAgent, falling back through the model group's members
        in priority order. Raises if the bound group lacks the function-calling
        constraint, if there are no candidates, or if all of them fail."""
        if not self.group_requires_function_calling:
            raise RuntimeError(
                f"agent {self.name} needs a model group with the function-calling "
                "constraint (its members aren't guaranteed to support tool calls). "
                "Create a group with the 'function calling' checkbox on /modelgroups "
                "and bind it to this agent on /agent_models."
            )
        if not self.candidate_model_uuids:
            raise RuntimeError(
                f"agent {self.name} has no model group / candidate models bound"
            )
        last_error: Exception | None = None
        for model_uuid in self.candidate_model_uuids:
            try:
                _provider_id, model_name, args = db.resolved_model_kwargs(model_uuid)
                logger.info(
                    "agent %s: calling model %s via FunctionAgent (this loads it "
                    "into LM Studio if it isn't already; a large cold model may "
                    "take a while)",
                    self.name,
                    model_name,
                )
                t0 = time.monotonic()
                # FunctionAgent rejects an LLM that doesn't advertise function
                # calling. We rely on the group's function-calling constraint
                # (see setup) to guarantee members already have
                # is_function_calling_model=True in their resolved args — so no
                # forcing here. A misconfigured (non-FC) member just fails this
                # candidate and falls through to the next.
                the_llm = prepare_llm(_provider_id, model_name, args)
                reply = asyncio.run(self._arun(the_llm, user_prompt))
                logger.info(
                    "agent %s: model %s responded in %.1fs",
                    self.name,
                    model_name,
                    time.monotonic() - t0,
                )
                return reply
            except Exception as e:
                last_error = e
                logger.warning(
                    "agent %s: model %s failed (%s); trying next in group",
                    self.name,
                    model_uuid,
                    e,
                )
        raise RuntimeError(
            f"agent {self.name}: all {len(self.candidate_model_uuids)} models "
            f"in the group failed; last error: {last_error}"
        )

    def handle(self, journal_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        room_uuid = self._room_uuid(payload)
        user_prompt = self.user_prompt(payload)
        logger.info(
            "tool-demo agent prompts for room %s:\n"
            "=== SYSTEM PROMPT ===\n%s\n"
            "=== USER PROMPT ===\n%s\n"
            "=== END PROMPTS ===",
            room_uuid,
            self.system_prompt,
            user_prompt,
        )
        reply = self._run_function_agent(user_prompt).strip()
        posted_uuid: str | None = None
        if reply:
            posted = db.post_chat_message(room_uuid, self.agent_uuid, reply, "markdown")
            posted_uuid = str(posted.uuid)
            logger.info(
                "tool-demo agent replied in room %s (message %s)", room_uuid, posted_uuid
            )
        else:
            logger.warning(
                "tool-demo agent produced an empty reply in room %s; nothing posted",
                room_uuid,
            )
        return {"ok": True, "reply_content": reply, "posted_message_uuid": posted_uuid}
