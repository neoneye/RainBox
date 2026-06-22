"""QueryAgent — answers chat questions from a JSONL Q&A registry via pgvector.

Two-stage match:

1. **Exact alias** — normalize the message and look it up in an in-memory table
   of every JSONL question alternate. Costs no embedding call and is correct by
   construction for verbatim phrases ("git branch", "What is your name?").
2. **Semantic** — embed the message via Ollama's embeddinggemma:300m (768-dim) and
   retrieve the top-K from a pgvector-backed table. Group hits by `qa_id`, take
   the max score per qa_id,
   require the best qa_id's score >= MIN_SCORE and beat the second qa_id by >=
   MIN_MARGIN — otherwise return no match (better a clean "no" than a confident
   wrong answer, because every input has *some* nearest neighbour).

For a match, post the entry's static `answer` or call the named dynamic handler
in `query_handlers.HANDLERS` (which receives a `QueryContext` so handlers can be
room-aware). Also post a small `debug-query` JSON row with the match info first,
then the answer.

The JSONL is embedded into `data_seed_memory` on first use. The KB is
**not** refreshed automatically when the JSONL changes; set the env var
`QUERY_AGENT_REBUILD_KB=1` (or truncate the table) and restart the agent process
to repopulate.
"""

import json
import logging
from typing import Any
from uuid import UUID

import db
from agents.base import Agent, StatusSender
from agents.query_handlers import QueryContext
from memory.seed_memory import (
    MIN_MARGIN,
    MIN_SCORE,
    Match,
    _ensure_populated,
    _exact_match,
    _load_kb,
    _resolve_match,
    _semantic_match,
    _vector_store,
    command_from_payload,
    room_uuid_from_payload,
    score_permille,
)

logger = logging.getLogger(__name__)


# --- Agent --------------------------------------------------------------------


class QueryAgent(Agent):
    """No-LLM chat agent that resolves a user message to one of the JSONL Q&A
    entries via exact alias or pgvector similarity, and posts the resulting
    answer (or a "no confident match" fallback)."""

    def __init__(self, agent_uuid: UUID, name: str, send: StatusSender) -> None:
        super().__init__(agent_uuid, name, send)

    def handle(self, journal_id: UUID, payload: dict[str, Any]) -> dict[str, Any]:
        room_uuid = room_uuid_from_payload(payload)
        query = command_from_payload(room_uuid, payload)
        if not query:
            return {"ok": True, "skipped": "no human query"}

        ctx = QueryContext(
            room_uuid=room_uuid,
            query=query,
            payload=payload,
            agent_uuid=self.agent_uuid,
        )

        # Memory commands take precedence over Q&A retrieval and must not
        # depend on LM Studio / pgvector being healthy: parse first, dispatch,
        # and return without touching the Q&A KB. Anything that doesn't parse
        # falls through to the existing Q&A path below.
        from memory.ops import handle_memory_command, parse_memory_command
        mem_cmd = parse_memory_command(query)
        if mem_cmd is not None:
            reply = handle_memory_command(ctx, mem_cmd)
            posted = db.post_chat_message(
                room_uuid, self.agent_uuid, reply, "markdown", kind="message"
            )
            logger.info(
                "query agent memory command room=%s kind=%s",
                room_uuid, mem_cmd.kind,
            )
            return {
                "ok": True,
                "method": "memory",
                "command_kind": mem_cmd.kind,
                "posted_message_uuid": str(posted.uuid),
            }

        db.post_progress(room_uuid, self.agent_uuid, "searching knowledge base")
        _load_kb()
        vs = _vector_store()
        _ensure_populated(vs)

        match = _exact_match(query) or _semantic_match(query, vs)

        # Always post the debug row first so the chat reads "decision, then answer"
        # (or "decision, then fallback").
        debug_payload: dict[str, Any] = {"query": query, "match": None}
        if match is not None:
            debug_payload["match"] = {
                "qa_id": match.qa_id,
                "method": match.method,
                "score": score_permille(match.score),
                "matched_question": match.matched_question,
                "second_qa_id": match.second_qa_id,
                "second_score": score_permille(match.second_score),
            }
        else:
            debug_payload["match"] = {
                "method": "none",
                "reason": "no confident match (below MIN_SCORE or ambiguous)",
                "MIN_SCORE": MIN_SCORE,
                "MIN_MARGIN": MIN_MARGIN,
            }
        db.post_chat_message(
            room_uuid,
            self.agent_uuid,
            json.dumps(debug_payload, ensure_ascii=False, separators=(",", ":")),
            "json",
            kind="debug-query",
        )

        if match is None:
            reply = "I don't have a confident match for that. Try rephrasing."
        else:
            reply = _resolve_match(match, ctx)
        posted = db.post_chat_message(
            room_uuid, self.agent_uuid, reply, "markdown", kind="message"
        )

        logger.info(
            "query agent room=%s match=%s method=%s score=%s",
            room_uuid,
            match.qa_id if match else None,
            match.method if match else None,
            match.score if match else None,
        )
        return {
            "ok": True,
            "matched_qa_id": match.qa_id if match else None,
            "method": match.method if match else None,
            "score": match.score if match else None,
            "posted_message_uuid": str(posted.uuid),
        }
