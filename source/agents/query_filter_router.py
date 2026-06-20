"""QueryFilterRouterAgent — two-stage LLM pipeline: relevance filter, then route.

Variant of QueryRouterAgent. Instead of asking one LLM to do "decide if the
candidate is relevant AND produce a reply" in a single call, this agent runs:

1. **Exact alias match** → resolve directly, no LLM call.
2. Else, top-K semantic candidates (ungated).
3. **LLM #1 (filter)** — given the user message + candidates, return the
   subset of qa_ids that DIRECTLY address the user. Hallucinated qa_ids are
   ignored.
4. The agent resolves *only* the candidates the filter kept (handlers run
   only when needed).
5. **LLM #2 (route)** — given the chat transcript and the pre-filtered
   relevant candidates, produce {subject, action, reply}. The routing prompt
   is short because it doesn't have to second-guess the candidates' relevance.

Adds two LLM calls in the non-exact path versus QueryRouter's one, but each
prompt has a single concern, which is cheap to follow even for small models.
"""

import json
import logging
from typing import Any, cast
from uuid import UUID

from llama_index.core.llms import ChatMessage, MessageRole
from pydantic import BaseModel, Field

import db
from agents.base import ModelGroupAgent
from chat.transcript import format_history
from llm import prepare_llm
from agents.query_handlers import QueryContext
from agents.query_kb_helpers import (
    Match,
    _ensure_populated,
    _exact_match,
    _load_kb,
    _resolve_match,
    _semantic_ranked,
    _vector_store,
    command_from_payload,
    get_entry,
    room_uuid_from_payload,
)
from agents.router import RouterResponse

logger = logging.getLogger(__name__)


class FilterDecision(BaseModel):
    """Output of the filter LLM call: which candidate qa_ids are relevant."""

    relevant_qa_ids: list[str] = Field(
        description=(
            "Subset of the listed candidate qa_ids that directly address the "
            "user's message. Use the empty list when none are relevant. Do not "
            "invent qa_ids — only use ones present in the candidates."
        )
    )


FILTER_SYSTEM_PROMPT: str = """\
You are a relevance filter. Given the user's latest chat message and a list of
candidate Q&A entries from a knowledge base, decide which (if any) of the
candidates DIRECTLY address the user's message.

A candidate is relevant when its question/answer is genuinely about what the
user is asking, telling, or doing.

A candidate is NOT relevant when it is about a different topic, or when the
user is volunteering information that the candidate does not speak to (for
example: the user says where THEY are from, but the candidate is about the
BOT's location — not relevant).

Return exactly one JSON object with one field:
- `relevant_qa_ids`: a list of qa_id strings (a subset of the listed
  candidates). Empty list if none are relevant. Do not invent qa_ids.

Output only the JSON object. No prose, no markdown fences."""


QUERY_FILTER_ROUTER_SYSTEM_PROMPT: str = """\
You are a chat assistant. Reply to the user's latest message.

You receive an IRC-style chat transcript with the latest user message marked
"Current message:", and zero or more **pre-filtered relevant** knowledge-base
candidates that directly address the user. The candidates have already been
checked for relevance — you do not need to second-guess that.

How to reply:
- If a candidate is listed, use its reply (verbatim if it fits, paraphrased
  briefly otherwise).
- If no candidates are listed, reply conversationally based on the chat alone.
- If the user is responding to your earlier question, acknowledge their answer
  and continue. Never repeat your own prior question or copy a prior agent
  reply verbatim.
- Keep replies short.

Return exactly one JSON object with three fields, and nothing else:
- `subject`: 10-20 word summary of the user's message in context.
- `action`: "yes" if the user clearly requests something done; "no" if no
  action is needed; "unclear" if you cannot tell.
- `reply`: a short reply. Always populate it.

Output only the JSON object. No prose, no markdown fences."""


TOP_K_FILTER: int = 5

# Posted to the room when the semantic-retrieval embeddings or the filter/route
# LLMs can't be reached (e.g. LM Studio is not running). Without this, the
# non-exact path raises out of handle() after the fail-fast attempt and the user
# sees nothing happen at all — see the exact-vs-semantic dependency split in the
# module docstring (exact matches need neither embeddings nor an LLM).
PROVIDER_UNREACHABLE_REPLY: str = (
    "I can't reach my language models right now, so I can't answer that. "
    "(The model server may be down — exact built-in commands still work.)"
)


def _is_provider_unreachable(exc: BaseException) -> bool:
    """True if `exc` (or anything in its cause/context chain) looks like the
    model server being unreachable rather than a logic bug. Matches the OpenAI
    SDK's APIConnectionError/APITimeoutError by name (avoids importing openai
    here) and the 'Connection error' string the SDK logs, including when our
    _llm_structured has wrapped the cause in a RuntimeError."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        name = type(cur).__name__
        if name in {"APIConnectionError", "APITimeoutError", "ConnectionError"}:
            return True
        msg = str(cur).lower()
        if "connection error" in msg or "connection refused" in msg:
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _record_filter_events(
    *,
    query: str,
    room_uuid: UUID,
    agent_uuid: UUID,
    journal_id: UUID,
    source: str,
    retrieved: list[dict[str, Any]],
    relevant_ids: set[str],
    used_ids: set[str],
) -> None:
    """Write one event row per stage-decision for one query.

    `retrieved` is a list of dicts with keys `qa_id`, `rank`, `score`.
    `relevant_ids` is the set the LLM filter (or exact-alias step) kept;
    `used_ids` is the subset chosen for the final route/reply.

    Invariant: every id in `used_ids` MUST be in `relevant_ids`
    (you can't use something the filter dropped). The helper asserts
    this so an inconsistent caller fails loudly rather than writing a
    misleading event sequence.

    All event rows for one call are batched into a single transaction
    (`commit=False` per row, then one final `db.db.session.commit()`)
    to avoid 5-10 fsyncs per user query.

    Note: `used` events from this helper are an approximation — they
    are written for every accepted candidate even though the final
    answer may not have cited all of them. Consumers should read the
    `used_signal` metadata field on these rows to detect the
    approximation source.
    """
    if not retrieved:
        return
    assert used_ids <= relevant_ids, (
        f"used_ids={used_ids} must be a subset of relevant_ids={relevant_ids}"
    )
    for c in retrieved:
        qa_id = c["qa_id"]
        db.record_retrieval_event(
            target_type="qa_entry",
            target_id=qa_id,
            stage="retrieved",
            query=query,
            room_uuid=room_uuid,
            agent_uuid=agent_uuid,
            journal_id=journal_id,
            source=source,
            retrieval_rank=c.get("rank"),
            retrieval_score=c.get("score"),
            filter_label=None,
            commit=False,
        )
    retrieved_ids = {c["qa_id"] for c in retrieved}
    for qa_id in retrieved_ids:
        if qa_id in relevant_ids:
            db.record_retrieval_event(
                target_type="qa_entry",
                target_id=qa_id,
                stage="accepted",
                query=query,
                room_uuid=room_uuid,
                agent_uuid=agent_uuid,
                journal_id=journal_id,
                source=source,
                filter_label="relevant",
                commit=False,
            )
        else:
            db.record_retrieval_event(
                target_type="qa_entry",
                target_id=qa_id,
                stage="rejected",
                query=query,
                room_uuid=room_uuid,
                agent_uuid=agent_uuid,
                journal_id=journal_id,
                source=source,
                filter_label="irrelevant",
                commit=False,
            )
    for qa_id in used_ids:
        db.record_retrieval_event(
            target_type="qa_entry",
            target_id=qa_id,
            stage="used",
            query=query,
            room_uuid=room_uuid,
            agent_uuid=agent_uuid,
            journal_id=journal_id,
            source=source,
            filter_label="relevant",
            metadata={"used_signal": "accepted_candidate_approximation"},
            commit=False,
        )
    db.db.session.commit()


class QueryFilterRouterAgent(ModelGroupAgent):
    """Two-stage LLM pipeline: filter then route. See module docstring."""

    def _llm_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        response_model: type[BaseModel],
    ) -> BaseModel:
        """Structured-output call against the agent's bound model group, falling
        back through the group's members on failure. Like
        StructuredLLMAgent._structured_call but takes the schema as an argument
        so we can use two different schemas in one handle."""
        if not self.candidate_model_uuids:
            raise RuntimeError(
                f"agent {self.name} has no model group bound (assign one on /agent_models)"
            )
        messages = [
            ChatMessage(role=MessageRole.SYSTEM, content=system_prompt),
            ChatMessage(role=MessageRole.USER, content=user_prompt),
        ]
        last_error: Exception | None = None
        for model_uuid in self.candidate_model_uuids:
            try:
                _provider_id, model_name, args = db.resolved_model_kwargs(model_uuid)
                # Fail fast on a down/unreachable provider: the OpenAI client's
                # default exponential backoff (max_retries=10) turns one outage
                # into a ~30s+ hang per model with no UI feedback. We already
                # fall back across the group's members, so per-model retries add
                # latency without improving the odds. (Native-Ollama drops the
                # key in prepare_llm's field filter, so this is a no-op there.)
                args = {**args, "max_retries": 0}
                the_llm = prepare_llm(_provider_id, model_name, args)
                sllm = the_llm.as_structured_llm(response_model)
                result = cast(BaseModel, sllm.chat(messages).raw)
                # Remember which group member actually answered so the
                # get_model_info handler can report the real (post-fallback)
                # model rather than just the first candidate.
                self._active_model_uuid = model_uuid
                return result
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

    def _build_filter_prompt(self, query: str, candidates: list[Match]) -> str:
        lines = [f"Current user message: {query!r}", "", "Candidates:"]
        for c in candidates:
            entry = get_entry(c.qa_id) or {}
            kind = entry.get("kind", "?")
            lines.append(f"  - qa_id: {c.qa_id}")
            lines.append(f"    similarity score: {c.score:.3f}")
            lines.append(f"    matched_question: {c.matched_question!r}")
            lines.append(f"    kind: {kind}")
            if kind == "static":
                lines.append(f"    answer: {entry.get('answer', '')!r}")
            elif kind == "dynamic":
                lines.append(f"    handler: {entry.get('handler', '')}")
            lines.append("")
        return "\n".join(lines)

    def _build_route_prompt(
        self,
        room_uuid: UUID,
        relevant_qa_ids: list[str],
        resolved_replies: dict[str, str],
    ) -> str:
        msgs = [m for m in db.list_room_messages(room_uuid) if m.get("kind") == "message"]
        transcript = format_history(msgs)
        if not relevant_qa_ids:
            return transcript + "\n\nRelevant candidates: (none)"
        lines = ["", "Relevant candidates:"]
        for qa_id in relevant_qa_ids:
            lines.append(f"  - qa_id: {qa_id}")
            lines.append(f"    reply: {resolved_replies.get(qa_id, '')!r}")
        return transcript + "\n" + "\n".join(lines)

    def handle(self, journal_id: UUID, payload: dict[str, Any]) -> dict[str, Any]:
        room_uuid = room_uuid_from_payload(payload)
        query = command_from_payload(room_uuid, payload)
        if not query:
            return {"ok": True, "skipped": "no human query"}

        # Reset per-item: which group member ends up answering is decided during
        # this handle() (in _llm_structured's fallback loop); don't leak the
        # previous item's choice into this one's get_model_info answer.
        self._active_model_uuid: UUID | None = None

        ctx = QueryContext(
            room_uuid=room_uuid,
            query=query,
            payload=payload,
            agent_uuid=self.agent_uuid,
            model_group_uuid=self.model_group_uuid,
            candidate_model_uuids=list(self.candidate_model_uuids),
            active_model_uuid=self._active_model_uuid,
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
                room_uuid, self.agent_uuid, reply, "markdown", kind="message",
            )
            logger.info(
                "query_filter_router memory command room=%s kind=%s",
                room_uuid, mem_cmd.kind,
            )
            return {
                "ok": True,
                "method": "memory",
                "command_kind": mem_cmd.kind,
                "posted_message_uuid": str(posted.uuid),
            }

        _load_kb()
        vs = _vector_store()
        _ensure_populated(vs)

        # --- 1) exact alias → no LLM -----------------------------------------
        exact = _exact_match(query)
        if exact is not None:
            reply = _resolve_match(exact, ctx)
            try:
                _record_filter_events(
                    query=query,
                    room_uuid=room_uuid,
                    agent_uuid=self.agent_uuid,
                    journal_id=journal_id,
                    source="query_filter_router",
                    retrieved=[{"qa_id": exact.qa_id, "rank": 0, "score": 1.0}],
                    relevant_ids={exact.qa_id},
                    used_ids={exact.qa_id},
                )
            except Exception:
                logger.exception(
                    "telemetry: failed to record filter events; "
                    "swallowing so the user query is not blocked"
                )
                db.db.session.rollback()
            db.post_chat_message(
                room_uuid, self.agent_uuid,
                json.dumps({
                    "query": query,
                    "match": {
                        "qa_id": exact.qa_id, "method": "exact",
                        "score": exact.score,
                        "matched_question": exact.matched_question,
                    },
                }, ensure_ascii=False, separators=(",", ":")),
                "json", kind="debug-query",
            )
            posted = db.post_chat_message(
                room_uuid, self.agent_uuid, reply, "markdown", kind="message",
            )
            logger.info("query_filter_router room=%s exact qa_id=%s", room_uuid, exact.qa_id)
            return {
                "ok": True, "method": "exact",
                "matched_qa_id": exact.qa_id,
                "posted_message_uuid": str(posted.uuid),
            }

        # Everything below this point depends on the model server (LM Studio):
        # query embedding for semantic retrieval, then the filter and route LLM
        # calls. If the server is unreachable, fail fast (clients are tuned to
        # not retry/backoff) and post a graceful message rather than raising
        # silently and leaving the UI looking dead.
        try:
            # --- 2) top-K semantic candidates (ungated) ----------------------
            candidates = _semantic_ranked(query, vs)[:TOP_K_FILTER]

            # --- 3) LLM filter call -----------------------------------------
            relevant_qa_ids: list[str] = []
            resolved_replies: dict[str, str] = {}
            if candidates:
                db.post_progress(room_uuid, self.agent_uuid, "step 1 of 2: filtering candidates")
                filter_prompt = self._build_filter_prompt(query, candidates)
                filter_decision = cast(
                    FilterDecision,
                    self._llm_structured(FILTER_SYSTEM_PROMPT, filter_prompt, FilterDecision),
                )
                cand_qa_ids = {c.qa_id for c in candidates}
                # Drop hallucinated qa_ids; keep filter's order so the LLM-preferred
                # candidate comes first in the route prompt.
                relevant_qa_ids = [q for q in filter_decision.relevant_qa_ids if q in cand_qa_ids]
                # The filter call just ran, so a model in the group has now answered;
                # expose it to handlers (get_model_info) resolved below.
                ctx.active_model_uuid = self._active_model_uuid
                # --- 4) Resolve only the kept candidates --------------------
                for qa_id in relevant_qa_ids:
                    cand = next((c for c in candidates if c.qa_id == qa_id), None)
                    if cand is not None:
                        resolved_replies[qa_id] = _resolve_match(cand, ctx)

            db.post_chat_message(
                room_uuid, self.agent_uuid,
                json.dumps({
                    "query": query,
                    "candidates": [
                        {"qa_id": c.qa_id, "score": c.score, "matched_question": c.matched_question}
                        for c in candidates
                    ],
                    "filter_kept": relevant_qa_ids,
                    "resolved": resolved_replies,
                }, ensure_ascii=False, separators=(",", ":")),
                "json", kind="debug-filter",
            )

            # --- 5) LLM route call (simpler prompt) -------------------------
            db.post_progress(room_uuid, self.agent_uuid, "step 2 of 2: composing reply")
            route_prompt = self._build_route_prompt(room_uuid, relevant_qa_ids, resolved_replies)
            route_response = cast(
                RouterResponse,
                self._llm_structured(QUERY_FILTER_ROUTER_SYSTEM_PROMPT, route_prompt, RouterResponse),
            )
        except Exception as e:
            unreachable = _is_provider_unreachable(e)
            logger.warning(
                "query_filter_router room=%s non-exact path failed "
                "(provider_unreachable=%s): %s",
                room_uuid, unreachable, e,
            )
            reply = PROVIDER_UNREACHABLE_REPLY if unreachable else (
                "Something went wrong while answering that. Please try again."
            )
            # kind="notice", not "message": this is an operational message, not a
            # conversation turn. It stays visible in the UI but is excluded from
            # the transcript fed to the route LLM (which filters kind=="message"),
            # so the model can't parrot "I can't reach my models" back as if it
            # were the answer to the next query. Still clears progress bubbles.
            posted = db.post_chat_message(
                room_uuid, self.agent_uuid, reply, "markdown", kind="notice",
            )
            return {
                "ok": False,
                "method": "filter+route",
                "error": type(e).__name__,
                "provider_unreachable": unreachable,
                "posted_message_uuid": str(posted.uuid),
            }

        try:
            _record_filter_events(
                query=query,
                room_uuid=room_uuid,
                agent_uuid=self.agent_uuid,
                journal_id=journal_id,
                source="query_filter_router",
                retrieved=[
                    {"qa_id": m.qa_id, "rank": i, "score": m.score}
                    for i, m in enumerate(candidates)
                ],
                relevant_ids=set(relevant_qa_ids),
                used_ids=set(relevant_qa_ids),
            )
        except Exception:
            logger.exception(
                "telemetry: failed to record filter events; "
                "swallowing so the user query is not blocked"
            )
            db.db.session.rollback()

        db.post_chat_message(
            room_uuid, self.agent_uuid,
            json.dumps({
                "subject": route_response.subject,
                "action": route_response.action,
                "reply": route_response.reply,
                "used_candidates": relevant_qa_ids,
            }, ensure_ascii=False, separators=(",", ":")),
            "json", kind="debug-router",
        )

        reply_text = (route_response.reply or "").strip()
        reply_uuid: str | None = None
        if reply_text:
            posted = db.post_chat_message(
                room_uuid, self.agent_uuid, reply_text, "markdown", kind="message",
            )
            reply_uuid = str(posted.uuid)

        logger.info(
            "query_filter_router room=%s kept=%s action=%s reply=%s",
            room_uuid, relevant_qa_ids, route_response.action, bool(reply_text),
        )
        return {
            "ok": True, "method": "filter+route",
            "filter_kept": relevant_qa_ids,
            "subject": route_response.subject,
            "action": route_response.action,
            "reply": reply_text,
            "posted_message_uuid": reply_uuid,
        }
