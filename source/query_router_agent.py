"""QueryRouterAgent — a crossover of QueryAgent and RouterAgent.

Pipeline:

1. **Exact alias** (verbatim phrase in the JSONL) → resolve the entry directly
   and post the answer. **No LLM call**. Fast and deterministic.
2. **Otherwise** → grab the top-1 semantic candidate from the pgvector KB
   *without* the confidence gate, `_resolve_match` it (so a dynamic handler's
   real output is materialized — not just its name), and pass that as context
   to the RouterAgent LLM (structured `{subject, action, reply}`). Post the
   LLM's reply.

This gives operators "free" answers for well-aliased intents (`git branch`,
`What is your name?`) while still falling through to the LLM — with the
query-agent's best guess + handler output as a hint — for everything else.
"""

import json
import logging
from typing import Any, cast
from uuid import UUID

import db
from agent import StatusSender, StructuredLLMAgent
from chat.transcript import format_history
from query_kb_helpers import (
    Match,
    _ensure_populated,
    _exact_match,
    _load_kb,
    _resolve_match,
    _semantic_ranked,
    _vector_store,
)
from query_handlers import QueryContext
from router_agent import RouterResponse

logger = logging.getLogger(__name__)


# Dedicated prompt for QueryRouter: like the router but with explicit guidance
# for *this* flow (KB candidate may be irrelevant; don't echo your own prior
# question; respond to the user's latest message in conversation).
QUERY_ROUTER_SYSTEM_PROMPT: str = """\
You are a chat assistant. Triage the user's latest message and produce a
structured response that the chat UI will display.

You will receive:
1. An IRC-style chat transcript, oldest → newest, with the user's latest message
   marked "Current message:".
2. A "Query-agent candidate" hint at the end: a knowledge-base entry whose
   embedded question was the most similar to the user's message. It MAY or MAY
   NOT be relevant — it is a hint, never an answer by default.

How to reply:
- Read the Current message in the context of the conversation, including any
  question YOU asked earlier that the user might now be answering.
- If the candidate directly answers the user (e.g. the user asks the bot's
  name and the candidate is identity.name), use its `candidate reply`.
- If the user is making small talk, volunteering information about themselves,
  or the candidate is about a different topic (e.g. the candidate is about the
  bot's location but the user is sharing THEIR location), IGNORE the candidate
  and reply naturally and briefly.
- If the user is responding to a previous question YOU asked, acknowledge
  their answer and continue the conversation. NEVER repeat your own previous
  question, and NEVER copy a previous agent reply verbatim.
- Keep replies short and direct.

Return exactly one JSON object with three fields, and nothing else:
- `subject`: 10-20 word summary of what the user is saying or asking, resolved
  against the conversation.
- `action`: "yes" if it clearly requests something done; "no" if no action is
  needed (small talk, statement, thanks); "unclear" if you cannot tell.
- `reply`: a short conversational reply to the user. Always populate it.

Examples (transcript shown in shorthand; candidate elided when irrelevant):

  <bot> Where are you from?
  <user> im from denmark
  →  {"subject":"user says they are from Denmark","action":"no","reply":"Nice — whereabouts in Denmark?"}

  <user> What is your name?
  candidate: identity.name → "My name is Rainbox. …"
  →  {"subject":"user asks the bot's name","action":"yes","reply":"My name is Rainbox. You can also just call me bot."}

  <user> thanks
  →  {"subject":"user thanks the agent","action":"no","reply":"You're welcome."}

  <user> please refactor the websocket scheduler
  →  {"subject":"user requests a code refactor","action":"yes","reply":"I don't have that capability — I only answer from a curated knowledge base."}

Output only the JSON object. No prose, no markdown fences."""


class QueryRouterAgent(StructuredLLMAgent):
    """Combines QueryAgent's KB lookup with RouterAgent's LLM triage. See module
    docstring for the routing pipeline."""

    def __init__(self, agent_uuid: UUID, name: str, send: StatusSender) -> None:
        super().__init__(
            agent_uuid,
            name,
            send,
            system_prompt=QUERY_ROUTER_SYSTEM_PROMPT,
            response_model=RouterResponse,
        )

    @staticmethod
    def _room_uuid(payload: dict[str, Any]) -> UUID:
        raw = payload.get("room_uuid")
        if not raw:
            raise ValueError("query_router agent payload missing 'room_uuid'")
        return raw if isinstance(raw, UUID) else UUID(str(raw))

    @staticmethod
    def _command_from_payload(room_uuid: UUID, payload: dict[str, Any]) -> str | None:
        msgs = db.list_room_messages(room_uuid)
        msg_uuid = payload.get("message_uuid")
        if msg_uuid:
            for m in msgs:
                if m.get("uuid") == str(msg_uuid) and m.get("sender_type") == "human":
                    return (m.get("text") or "").strip()
            return None
        for m in reversed(msgs):
            if m.get("sender_type") == "human":
                return (m.get("text") or "").strip()
        return None

    def _build_user_prompt(
        self,
        room_uuid: UUID,
        candidate: Match | None,
        candidate_reply: str | None,
    ) -> str:
        """IRC-style transcript (real messages only) plus an addendum that
        gives the LLM the query-agent's best guess as a hint."""
        msgs = [m for m in db.list_room_messages(room_uuid) if m.get("kind") == "message"]
        transcript = format_history(msgs)
        if candidate is None:
            hint = "\n\nQuery-agent candidate: (none — nothing in the KB looked relevant)."
        else:
            hint = (
                "\n\nQuery-agent candidate:"
                f"\n  qa_id: {candidate.qa_id}"
                f"\n  similarity score: {candidate.score:.3f}"
                f"\n  matched question alternate: {candidate.matched_question!r}"
                f"\n  candidate reply: {candidate_reply!r}"
            )
        return transcript + hint

    def handle(self, journal_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        room_uuid = self._room_uuid(payload)
        query = self._command_from_payload(room_uuid, payload)
        if not query:
            return {"ok": True, "skipped": "no human query"}

        _load_kb()
        vs = _vector_store()
        _ensure_populated(vs)
        ctx = QueryContext(
            room_uuid=room_uuid,
            query=query,
            payload=payload,
            agent_uuid=self.agent_uuid,
        )

        # --- 1) exact alias → no LLM -----------------------------------------
        exact = _exact_match(query)
        if exact is not None:
            reply = _resolve_match(exact, ctx)
            debug_q = json.dumps(
                {
                    "query": query,
                    "match": {
                        "qa_id": exact.qa_id,
                        "method": "exact",
                        "score": exact.score,
                        "matched_question": exact.matched_question,
                    },
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            db.post_chat_message(
                room_uuid, self.agent_uuid, debug_q, "json", kind="debug-query"
            )
            posted = db.post_chat_message(
                room_uuid, self.agent_uuid, reply, "markdown", kind="message"
            )
            logger.info("query_router room=%s exact qa_id=%s", room_uuid, exact.qa_id)
            return {
                "ok": True,
                "method": "exact",
                "matched_qa_id": exact.qa_id,
                "posted_message_uuid": str(posted.uuid),
            }

        # --- 2) semantic candidate (ungated) → LLM with that as context ------
        candidates = _semantic_ranked(query, vs)
        candidate: Match | None = candidates[0] if candidates else None
        candidate_reply: str | None = None
        if candidate is not None:
            candidate_reply = _resolve_match(candidate, ctx)

        debug_q_payload: dict[str, Any] = {"query": query, "candidate": None}
        if candidate is not None:
            debug_q_payload["candidate"] = {
                "qa_id": candidate.qa_id,
                "method": "semantic",
                "score": candidate.score,
                "matched_question": candidate.matched_question,
                "reply": candidate_reply,
            }
        db.post_chat_message(
            room_uuid,
            self.agent_uuid,
            json.dumps(debug_q_payload, ensure_ascii=False, separators=(",", ":")),
            "json",
            kind="debug-query",
        )

        user_prompt = self._build_user_prompt(room_uuid, candidate, candidate_reply)
        response = cast(RouterResponse, self._structured_call(user_prompt))

        db.post_chat_message(
            room_uuid,
            self.agent_uuid,
            json.dumps(
                {
                    "subject": response.subject,
                    "action": response.action,
                    "reply": response.reply,
                    "candidate_qa_id": candidate.qa_id if candidate else None,
                    "candidate_score": candidate.score if candidate else None,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "json",
            kind="debug-router",
        )

        reply_text = (response.reply or "").strip()
        reply_uuid: str | None = None
        if reply_text:
            posted = db.post_chat_message(
                room_uuid, self.agent_uuid, reply_text, "markdown", kind="message"
            )
            reply_uuid = str(posted.uuid)

        logger.info(
            "query_router room=%s semantic candidate=%s score=%s action=%s reply=%s",
            room_uuid,
            candidate.qa_id if candidate else None,
            candidate.score if candidate else None,
            response.action,
            bool(reply_text),
        )
        return {
            "ok": True,
            "method": "semantic+llm" if candidate else "no-candidate+llm",
            "candidate_qa_id": candidate.qa_id if candidate else None,
            "candidate_score": candidate.score if candidate else None,
            "subject": response.subject,
            "action": response.action,
            "reply": reply_text,
            "posted_message_uuid": reply_uuid,
        }
