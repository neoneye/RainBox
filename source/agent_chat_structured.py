"""Structured chat agent — a specialized StructuredLLMAgent.

Reads one chatroom's message history (the room_uuid comes in the inbox
payload), serves that history as the user prompt, and produces a single
structured field: the reply message. (Cheap local models got confused by an
action/ignore discriminator, so the agent now always replies.)

It posts the reply back into the room as its own chat_user (which shows up
live via SSE). It is only enqueued for human messages (see the trigger in
webapp/chat_api.py), so its own replies don't re-trigger it.

This is the structured-output variant (`as_structured_llm`). Its plain-text
sibling is `UnstructuredChatAgent` in agent_chat_unstructured.py; both share
the transcript formatter (chat.transcript.format_history) and the memory
retrieval helper (memory.retrieval.build_chat_memory_block).

Specialized agents live in their own agent_<purpose>.py module; the shared
base classes (Agent, ModelGroupAgent, StructuredLLMAgent) stay in agent.py.
"""

import json
import logging
from typing import Any, Literal, cast
from uuid import UUID

from pydantic import BaseModel, Field

import db
from memory import retrieval as memory_retrieval
from agent import StatusSender, StructuredLLMAgent
from chat.transcript import format_history

logger = logging.getLogger(__name__)


class ChatAgentResponse(BaseModel):
    # `reply_content` is the text shown in the room. `reply_format` lets the model
    # signal that `reply_content` holds a JSON document, instead of forcing cheap models
    # to hand-escape nested JSON (which they botch); handle() validates and
    # normalizes the JSON itself.
    reply_format: Literal["markdown", "json"] = Field(
        default="markdown",
        description='Use "json" only when reply_content is a raw JSON document; otherwise use "markdown".',
    )
    reply_content: str = Field(
       description="Your reply to the Current message only"
    )


CHAT_SYSTEM_PROMPT: str = """\
You are replying in a group chat.

You will receive an IRC-style chat transcript:
1. An optional "Relevant remembered facts:" block, each tagged with its
   kind, sensitivity, and provenance.
2. Optional chat history, oldest first.
3. A clearly marked Current message at the bottom.

Reply only to the Current message. Use earlier chat history only when it is
directly needed. Do not imitate previous agent replies — previous agent replies
may be wrong. Use the same language as the Current message unless the user asks
for another language.

Use remembered facts only when they are relevant to the Current message. Do
not reveal private facts unless the user is asking about themselves or the
project context makes it directly relevant. If a remembered fact conflicts
with the Current message, follow the Current message and mention the
conflict only when it is useful.

Return exactly one JSON object with two fields, and nothing else:
- `reply_format`: "markdown" for a normal message, or "json" when the user explicitly asks you to respond with or show JSON.
- `reply_content`: the message shown in the chat room.

When `reply_format` is "markdown", `reply_content` must be only the message content to post.

When `reply_format` is "json", `reply_content` must be only the raw JSON document.
Do not wrap it in markdown code fences. Add no prose before or after."""


class StructuredChatAgent(StructuredLLMAgent):
    """Reads a chatroom's history (room_uuid from the inbox payload), replies to
    the latest message, and posts that reply back into the room as its own
    chat_user."""

    def __init__(self, agent_uuid: UUID, name: str, send: StatusSender) -> None:
        super().__init__(
            agent_uuid,
            name,
            send,
            system_prompt=CHAT_SYSTEM_PROMPT,
            response_model=ChatAgentResponse,
        )

    @staticmethod
    def _room_uuid(payload: dict[str, Any]) -> UUID:
        raw = payload.get("room_uuid")
        if not raw:
            raise ValueError("chat agent payload missing 'room_uuid'")
        return raw if isinstance(raw, UUID) else UUID(str(raw))

    def user_prompt(
        self,
        payload: dict[str, Any],
        journal_id: int | None = None,
    ) -> str:
        room_uuid = self._room_uuid(payload)
        # Diagnostic rows (debug-memory, debug-query, debug-router, thinking,
        # progress, ...) are operator-only audit content. Filter them out so
        # they never end up in the LLM prompt or become the "Current message".
        messages = [
            m for m in db.list_room_messages(room_uuid)
            if m.get("kind") == "message"
        ]
        # Shared chat memory retrieval (query extraction + retrieval + telemetry).
        memory_block, query, memories = memory_retrieval.build_chat_memory_block(
            messages,
            agent_uuid=self.agent_uuid,
            room_uuid=room_uuid,
            journal_id=journal_id,
        )
        # Stash the retrieval result + the query for `handle` to log as
        # `debug-memory`. Per-instance state is fine because each agent
        # subprocess handles one job at a time.
        self._last_retrieval_query = query
        self._last_retrieved_memories = memories

        transcript = format_history(messages)
        if memory_block:
            return f"{memory_block}\n\n{transcript}"
        return transcript

    @staticmethod
    def _render_reply(response: ChatAgentResponse) -> str:
        """The text to post. For reply_format == "json", normalize valid JSON;
        otherwise wrap it as a JSON object so the room gets valid JSON."""
        if response.reply_format == "json":
            try:
                parsed = json.loads(response.reply_content)
            except (ValueError, TypeError):
                parsed = {"message": response.reply_content}
            return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
        return response.reply_content

    def handle(self, journal_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        room_uuid = self._room_uuid(payload)
        user_prompt = self.user_prompt(payload, journal_id=journal_id)
        logger.info(
            "chat agent prompts for room %s:\n"
            "=== SYSTEM PROMPT ===\n%s\n"
            "=== USER PROMPT ===\n%s\n"
            "=== END PROMPTS ===",
            room_uuid,
            self.system_prompt,
            user_prompt,
        )
        memory_retrieval.record_memory_use(
            journal_id=journal_id,
            room_uuid=room_uuid,
            agent_uuid=self.agent_uuid,
            query=getattr(self, "_last_retrieval_query", ""),
            memories=getattr(self, "_last_retrieved_memories", []),
        )
        db.post_progress(room_uuid, self.agent_uuid, "thinking")
        response = cast(ChatAgentResponse, self._structured_call(user_prompt))
        reply = self._render_reply(response).strip()
        content_type = "json" if response.reply_format == "json" else "markdown"
        posted_uuid: str | None = None
        if reply:
            posted = db.post_chat_message(room_uuid, self.agent_uuid, reply, content_type)
            posted_uuid = str(posted.uuid)
            logger.info("chat agent replied in room %s (message %s)", room_uuid, posted_uuid)
        else:
            logger.warning(
                "chat agent produced an empty reply in room %s; nothing posted", room_uuid
            )
        return {
            "ok": True,
            "reply_content": response.reply_content,
            "reply_format": response.reply_format,
            "posted_message_uuid": posted_uuid,
        }
