"""MCP agent — a chat agent whose tool list comes from MCP servers.

Reads `mcp.json`, spawns each configured server over stdio, pulls
their tools via LlamaIndex's `BasicMCPClient` + `McpToolSpec`, hands the
aggregated tool list to a `FunctionAgent`, and posts the reply back into
the room. Based on `agent_tool_demo.py`.
"""

import asyncio
import logging
import time
from typing import Any
from uuid import UUID

from llama_index.core.agent.workflow import FunctionAgent
from llama_index.core.tools import FunctionTool
from llama_index.tools.mcp import BasicMCPClient, McpToolSpec

import db
import llm as llm_module
from agents.base import ModelGroupAgent, StatusSender
from chat.transcript import format_history
from llm import ThinkingAwareOpenAILike
from agents.mcp_config import load_mcp_servers

logger = logging.getLogger(__name__)


MCP_SYSTEM_PROMPT: str = """\
You are replying in a group chat.

You will receive an IRC-style chat transcript:
1. Optional chat history, oldest first.
2. A clearly marked Current message at the bottom.

Reply only to the Current message, using the same language it uses. You
have access to tools provided by configured MCP servers — call them
whenever they help you answer the user, and use their results in your
reply. Respond with a normal chat message; do not wrap it in code
fences."""


async def _collect_mcp_tools() -> list[FunctionTool]:
    """Walk every server in mcp.json (stdio or HTTP/SSE), connect
    to it via BasicMCPClient, and aggregate the LlamaIndex FunctionTool
    list. A server that fails to start or list its tools is logged and
    skipped — one bad server does not disable the others."""
    tools: list[FunctionTool] = []
    servers = load_mcp_servers()
    if not servers:
        logger.warning(
            "mcp agent: no MCP servers configured in mcp.json; "
            "FunctionAgent will run without tools"
        )
    for server in servers:
        try:
            if server.url is not None:
                client = BasicMCPClient(server.url, headers=server.headers or None)
            else:
                client = BasicMCPClient(server.command or "", args=server.args)
            spec = McpToolSpec(client=client)
            server_tools = await spec.to_tool_list_async()
            tools.extend(server_tools)
            logger.info(
                "mcp agent: loaded %d tool(s) from server %s",
                len(server_tools), server.name,
            )
        except Exception as e:
            logger.warning(
                "mcp agent: failed to load tools from server %s: %s",
                server.name, e,
            )
    return tools


class MCPAgent(ModelGroupAgent):
    """Reads a chatroom's history (room_uuid from the inbox payload),
    runs a FunctionAgent with tools sourced from MCP servers, and posts
    the reply back into the room as its own chat_user."""

    def __init__(self, agent_uuid: UUID, name: str, send: StatusSender) -> None:
        super().__init__(agent_uuid, name, send)
        self.system_prompt = MCP_SYSTEM_PROMPT

    def setup(self) -> None:
        super().setup()
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
                "group created with the 'function calling' checkbox", self.name,
            )

    @staticmethod
    def _room_uuid(payload: dict[str, Any]) -> UUID:
        raw = payload.get("room_uuid")
        if not raw:
            raise ValueError("mcp agent payload missing 'room_uuid'")
        return raw if isinstance(raw, UUID) else UUID(str(raw))

    def user_prompt(self, payload: dict[str, Any]) -> str:
        return format_history(db.list_room_messages(self._room_uuid(payload)))

    async def _arun(self, the_llm: ThinkingAwareOpenAILike, user_prompt: str) -> str:
        """One FunctionAgent run: the model may call any MCP tool before
        replying. str(AgentOutput) is the final reply text."""
        tools = await _collect_mcp_tools()
        agent = FunctionAgent(
            tools=tools, llm=the_llm, system_prompt=self.system_prompt,
        )
        output = await agent.run(user_msg=user_prompt)
        return str(output).strip()

    def _run_function_agent(self, user_prompt: str) -> str:
        """Run the FunctionAgent, falling back through the model group's
        members in priority order. Raises if the bound group lacks the
        function-calling constraint or if all candidates fail."""
        if not self.group_requires_function_calling:
            raise RuntimeError(
                f"agent {self.name} needs a model group with the function-calling "
                "constraint. Create one with the 'function calling' checkbox on "
                "/modelgroup and bind it on /agent_models."
            )
        if not self.candidate_model_uuids:
            raise RuntimeError(
                f"agent {self.name} has no model group / candidate models bound"
            )
        last_error: Exception | None = None
        for model_uuid in self.candidate_model_uuids:
            try:
                provider_id, model_name, args = db.resolved_model_kwargs(model_uuid)
                logger.info(
                    "agent %s: calling model %s (provider %s) via FunctionAgent",
                    self.name, model_name, provider_id,
                )
                t0 = time.monotonic()
                # prepare_llm: for providers that support it (LM Studio),
                # ensures the loaded n_ctx is at least args["context_window"]
                # (otherwise llama.cpp rejects long prompts with
                # "n_keep > n_ctx"), then builds the LLM. For Jan this is a
                # no-op — context length is configured in Jan's UI.
                the_llm = llm_module.prepare_llm(provider_id, model_name, args)
                reply = asyncio.run(self._arun(the_llm, user_prompt))
                logger.info(
                    "agent %s: model %s responded in %.1fs",
                    self.name, model_name, time.monotonic() - t0,
                )
                return reply
            except Exception as e:
                last_error = e
                logger.warning(
                    "agent %s: model %s failed (%s); trying next in group",
                    self.name, model_uuid, e,
                )
        raise RuntimeError(
            f"agent {self.name}: all {len(self.candidate_model_uuids)} models "
            f"in the group failed; last error: {last_error}"
        )

    def handle(self, journal_id: UUID, payload: dict[str, Any]) -> dict[str, Any]:
        room_uuid = self._room_uuid(payload)
        user_prompt = self.user_prompt(payload)
        logger.info(
            "mcp agent prompts for room %s:\n"
            "=== SYSTEM PROMPT ===\n%s\n"
            "=== USER PROMPT ===\n%s\n"
            "=== END PROMPTS ===",
            room_uuid, self.system_prompt, user_prompt,
        )
        db.post_progress(room_uuid, self.agent_uuid, "thinking")
        reply = self._run_function_agent(user_prompt).strip()
        posted_uuid: str | None = None
        if reply:
            posted = db.post_chat_message(room_uuid, self.agent_uuid, reply, "markdown")
            posted_uuid = str(posted.uuid)
            logger.info(
                "mcp agent replied in room %s (message %s)", room_uuid, posted_uuid,
            )
        else:
            logger.warning(
                "mcp agent produced an empty reply in room %s; nothing posted",
                room_uuid,
            )
        return {"ok": True, "reply_content": reply, "posted_message_uuid": posted_uuid}
