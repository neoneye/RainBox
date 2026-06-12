"""Router agent — a specialized StructuredLLMAgent (structured output only).

Based on agent_chat.py: it reads one chatroom's message history (room_uuid from
the inbox payload) and renders it as the same IRC-style transcript the chat agent
uses. But instead of replying, it *triages* the Current message into a routing
decision via structured output (`as_structured_llm`) — a short subject summary
and whether the message requires an action. It does NOT use a FunctionAgent or
any tools.

The decision is posted back into the room as a JSON message (visible live via
SSE) and also returned on the journal result.

Specialized agents live in their own agent_<purpose>.py / *_agent.py module; the
shared base classes (Agent, ModelGroupAgent, StructuredLLMAgent) stay in agent.py.
"""

import json
import logging
from typing import Any, Literal, cast
from uuid import UUID

from pydantic import BaseModel, Field

import db
from agent import StatusSender, StructuredLLMAgent
from chat_transcript import format_history

logger = logging.getLogger(__name__)


class RouterResponse(BaseModel):
    subject: str = Field(
        description=(
            "Summarize the request in 10-20 words, using the whole conversation "
            "for context — the latest message may refer back to or build on "
            "earlier messages, so it is often not self-contained."
        )
    )
    action: Literal["no", "unclear", "yes"] = Field(
        description=(
            "Whether the Current message requires some action to happen: "
            '"yes" if it clearly asks for something to be done, "no" if no '
            'action is needed, "unclear" if you cannot tell.'
        )
    )
    # Required (no default) so the structured-output schema forces the model to
    # emit it — with a default it's optional and small models just drop it.
    reply: str = Field(
        description=(
            "A reply to show the user (may be an empty string). Ask a clarifying "
            'question when `action` is "unclear"; make small talk or acknowledge '
            'when `action` is "no". Use "" when `action` is "yes".'
        )
    )


ROUTER_SYSTEM_PROMPT: str = """\
You are a router. You triage the latest message in a group chat.

You will receive an IRC-style chat transcript:
1. Optional chat history, oldest first.
2. A clearly marked Current message at the bottom.

Triage the Current message, but interpret it using the chat history: the user is
often continuing an earlier thread or referring back to it (e.g. "yes, do that",
"the second one", "go ahead"), so the Current message on its own is frequently
insufficient. Read the whole transcript and resolve those references before
deciding.

Always summarize what the user is asking for in the Current (latest) message —
not an earlier message. Earlier messages are context only. The transcript may
include replies from other agents; never copy or imitate them.

Return exactly one JSON object with three fields, and nothing else:
- `subject`: a 10-20 word summary of what the user is actually asking for, resolved against the conversation (expand references like "that" / "it" / a bare "yes" using the history) — not just a paraphrase of the Current message in isolation.
- `action`: one of "yes", "no", "unclear" — does the Current message, read in the context of the conversation, require some action to be taken?
  - "yes": it clearly requests something be done (a task, a change, a lookup, an operation).
  - "no": no action is needed (small talk, a statement, a thank-you, or something already answered).
  - "unclear": you cannot tell whether an action is required.
- `reply`: a short, friendly reply to show the user. Ask a clarifying question when `action` is "unclear"; make small talk or acknowledge when `action` is "no". Use an empty string "" when `action` is "yes" (a clear request that gets handled elsewhere).

Examples (CURRENT message -> JSON):
- "hi"                         -> {"subject":"a greeting","action":"no","reply":"Hi! How can I help?"}
- "thanks!"                    -> {"subject":"thanks the agent","action":"no","reply":"You're welcome!"}
- "can you fix it?"            -> {"subject":"asks to fix something unspecified","action":"unclear","reply":"Sure — what exactly should I fix?"}
- "restart the staging server" -> {"subject":"asks to restart the staging server","action":"yes","reply":""}

Output only the JSON object. No prose, no markdown fences."""


class RouterAgent(StructuredLLMAgent):
    """Reads a chatroom's history (room_uuid from the inbox payload), classifies
    the latest message into {subject, action} via structured output, and posts
    the decision back into the room as a JSON message."""

    def __init__(self, agent_uuid: UUID, name: str, send: StatusSender) -> None:
        super().__init__(
            agent_uuid,
            name,
            send,
            system_prompt=ROUTER_SYSTEM_PROMPT,
            response_model=RouterResponse,
        )

    @staticmethod
    def _room_uuid(payload: dict[str, Any]) -> UUID:
        raw = payload.get("room_uuid")
        if not raw:
            raise ValueError("router agent payload missing 'room_uuid'")
        return raw if isinstance(raw, UUID) else UUID(str(raw))

    def user_prompt(self, payload: dict[str, Any]) -> str:
        messages = db.list_room_messages(self._room_uuid(payload))
        # Only real chat messages belong in the transcript. Dropping non-"message"
        # rows (e.g. the router's own {subject, action} "debug-router" output, or
        # any "thinking" rows) keeps the model summarizing the conversation
        # instead of parroting earlier diagnostic output.
        messages = [m for m in messages if m.get("kind") == "message"]
        return format_history(messages)

    def handle(self, journal_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        room_uuid = self._room_uuid(payload)
        user_prompt = self.user_prompt(payload)
        response = cast(RouterResponse, self._structured_call(user_prompt))
        debug = json.dumps(
            {
                "subject": response.subject,
                "action": response.action,
                "reply": response.reply,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        posted = db.post_chat_message(
            room_uuid, self.agent_uuid, debug, "json", kind="debug-router"
        )
        # Show the reply (clarification / small talk) to the user as a real
        # chat message when the model produced one.
        reply_text = (response.reply or "").strip()
        reply_uuid: str | None = None
        if reply_text:
            posted_reply = db.post_chat_message(
                room_uuid, self.agent_uuid, reply_text, "markdown", kind="message"
            )
            reply_uuid = str(posted_reply.uuid)
        logger.info(
            "router agent classified room %s: action=%s subject=%r reply=%s (debug %s)",
            room_uuid,
            response.action,
            response.subject,
            bool(reply_text),
            posted.uuid,
        )
        return {
            "ok": True,
            "subject": response.subject,
            "action": response.action,
            "reply": reply_text,
            "debug_message_uuid": str(posted.uuid),
            "reply_message_uuid": reply_uuid,
        }
