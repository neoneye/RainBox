"""QueryFilterRouterAgent — two-stage LLM pipeline: relevance filter, then route.

Variant of QueryRouterAgent. Instead of asking one LLM to do "decide if the
candidate is relevant AND produce a reply" in a single call, this agent runs:

1. **Exact alias match** → resolve directly, no LLM call.
2. Else, top-K semantic candidates (ungated).
3. **LLM #1 (filter)** — given the user message + candidates, score every
   candidate on three Likert scales (direct/indirect/relevancy). The
   keep/drop decision is made in code from those scores
   (`apply_filter_scores`): fewer than top-K candidates → keep all; a full
   list → keep those scoring high enough. Hallucinated qa_ids are ignored.
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
from dataclasses import dataclass
from typing import Any, Literal, cast
from uuid import UUID

from llama_index.core.llms import ChatMessage, MessageRole
from pydantic import BaseModel, Field

import db
from agents.base import ModelGroupAgent
from chat.transcript import format_history
from llm import prepare_llm
from agents.query_handlers import QueryContext
from memory.seed_memory import (
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
    score_permille,
)
from agents.router import RouterResponse

logger = logging.getLogger(__name__)


class FilterScore(BaseModel):
    """One candidate's relevance scores from the filter LLM. The LLM only
    scores; keeping or dropping is decided in code (`apply_filter_scores`)."""

    id: str = Field(description="The qa_id.")
    direct: Literal["1", "2", "3", "4", "5"] = Field(
        description="Does this `directly` address the query. Likert scale."
    )
    indirect: Literal["1", "2", "3", "4", "5"] = Field(
        description="Does this `indirectly` address the query. Likert scale."
    )
    relevancy: Literal["1", "2", "3", "4", "5"] = Field(
        description="Is it somehow `relevant` to the query. Likert scale."
    )


class FilterDecision(BaseModel):
    """Output of the filter LLM call: a score row per listed candidate."""

    items: list[FilterScore] = Field(
        description="All the listed candidate qa_ids, each with its scores."
    )


FILTER_SYSTEM_PROMPT: str = """\
You are a relevance scorer. Given the user's latest chat message and a list of
candidate Q&A entries from a knowledge base, score EVERY candidate on three
Likert scales from "1" (not at all) to "5" (fully):

- `direct`: the candidate's question/answer directly addresses what the user
  is asking, telling, or doing.
- `indirect`: the candidate addresses the message indirectly — closely related
  context, e.g. for a question about a person, an entry about that person's
  family or household.
- `relevancy`: the candidate is somehow relevant to the message at all.

A candidate about a different topic, or one the user's message does not speak
to (for example: the user says where THEY are from, but the candidate is about
the BOT's location) scores low on all three scales.

Each candidate carries a `similarity score`: an integer from 0 to 1000 (higher
means a closer semantic match; 1000 is an exact match). Treat it as a hint, not
a hard threshold — a high score still has to be on-topic to score high.

You do not decide what is kept or dropped — that decision is made downstream
from your scores. Score every listed candidate; omit none; do not invent ids.

Return exactly one JSON object with one field:
- `items`: a list with one entry per listed candidate:
  {"id": "<qa_id>", "direct": "1".."5", "indirect": "1".."5",
   "relevancy": "1".."5"}

Output only the JSON object. No prose, no markdown fences."""


TOP_K_FILTER: int = 5

# Code-side keep/drop policy over the LLM's scores (docs in apply_filter_scores).
FILTER_KEEP_THRESHOLD: int = 4


@dataclass
class ScoredCandidate:
    """One candidate after the code-side keep/drop decision: the LLM's three
    scores (0 = the LLM omitted this candidate) plus the verdict."""

    qa_id: str
    direct: int
    indirect: int
    relevancy: int
    kept: bool


def apply_filter_scores(
    decision: FilterDecision, candidates: list[Match], *, top_k: int = TOP_K_FILTER
) -> list[ScoredCandidate]:
    """The keep/drop decision, in code — the LLM only supplies scores.

    Policy: with fewer than `top_k` candidates there is no real competition, so
    ALL candidates are kept (an over-aggressive scorer can no longer empty a
    small result set); with a full list, a candidate is kept when any of its
    scales reaches FILTER_KEEP_THRESHOLD. Score rows for ids not in
    `candidates` (hallucinated) are ignored; candidates the LLM did not score
    default to 0/0/0 (dropped on a full list, kept on a small one). Returns
    every candidate ordered best-first (direct, then indirect, then relevancy,
    then semantic rank)."""
    candidate_ids = {c.qa_id for c in candidates}
    by_id: dict[str, FilterScore] = {}
    for item in decision.items:
        if item.id in candidate_ids and item.id not in by_id:
            by_id[item.id] = item
    keep_all = len(candidates) < top_k
    scored: list[ScoredCandidate] = []
    for c in candidates:
        item = by_id.get(c.qa_id)
        d, i, r = ((int(item.direct), int(item.indirect), int(item.relevancy))
                   if item is not None else (0, 0, 0))
        kept = keep_all or max(d, i, r) >= FILTER_KEEP_THRESHOLD
        scored.append(ScoredCandidate(
            qa_id=c.qa_id, direct=d, indirect=i, relevancy=r, kept=kept))
    # Stable sort: ties keep the semantic ranking order of `candidates`.
    scored.sort(key=lambda s: (-s.direct, -s.indirect, -s.relevancy))
    return scored


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


def build_filter_prompt(query: str, candidates: list[Match]) -> str:
    """User prompt for the relevance-filter LLM call: the user's message plus
    each candidate's qa_id/path/score/question and its answer (static) or
    handler name (dynamic). Shared with the assistant's memory_query seed
    filter and the /memory/developer page, so all filter callers present
    candidates identically."""
    lines = [f"Current user message: {query!r}", "", "Candidates:"]
    for c in candidates:
        entry = get_entry(c.qa_id) or {}
        kind = entry.get("kind", "?")
        lines.append(f"  - qa_id: {c.qa_id}")
        path = entry.get("path")
        if path:
            lines.append(f"    path: {path}")
        lines.append(f"    similarity score: {score_permille(c.score)}")
        lines.append(f"    matched_question: {c.matched_question!r}")
        lines.append(f"    kind: {kind}")
        if kind == "static":
            lines.append(f"    answer: {entry.get('answer', '')!r}")
        elif kind == "dynamic":
            lines.append(f"    handler: {entry.get('handler', '')}")
        lines.append("")
    return "\n".join(lines)


def structured_llm_call(
    agent_name: str,
    candidate_model_uuids: list[UUID],
    system_prompt: str,
    user_prompt: str,
    response_model: type[BaseModel],
) -> tuple[BaseModel, UUID]:
    """One structured-output call, falling back through a model group's members
    on failure. Returns (result, answering_model_uuid); raises RuntimeError when
    no group is bound or every member fails. `agent_name` only labels log/error
    messages. Free function (not an agent method) so non-agent callers — the
    assistant's memory_query seed filter, the /memory/developer page — can make
    the same call against any group's members."""
    if not candidate_model_uuids:
        raise RuntimeError(
            f"agent {agent_name} has no model group bound (assign one on /agentmodel)"
        )
    messages = [
        ChatMessage(role=MessageRole.SYSTEM, content=system_prompt),
        ChatMessage(role=MessageRole.USER, content=user_prompt),
    ]
    last_error: Exception | None = None
    for model_uuid in candidate_model_uuids:
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
            return result, model_uuid
        except Exception as e:
            last_error = e
            logger.warning(
                "agent %s: model %s failed (%s); trying next in group",
                agent_name, model_uuid, e,
            )
    raise RuntimeError(
        f"agent {agent_name}: all {len(candidate_model_uuids)} models "
        f"in the group failed; last error: {last_error}"
    )

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
        result, model_uuid = structured_llm_call(
            self.name, self.candidate_model_uuids,
            system_prompt, user_prompt, response_model,
        )
        # Remember which group member actually answered so the get_model_info
        # handler can report the real (post-fallback) model rather than just
        # the first candidate.
        self._active_model_uuid = model_uuid
        return result

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
                        "score": score_permille(exact.score),
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

            # --- 3) LLM scores + code-side keep/drop ------------------------
            relevant_qa_ids: list[str] = []
            resolved_replies: dict[str, str] = {}
            scored: list[ScoredCandidate] = []
            if candidates:
                db.post_progress(room_uuid, self.agent_uuid, "step 1 of 2: filtering candidates")
                filter_prompt = build_filter_prompt(query, candidates)
                filter_decision = cast(
                    FilterDecision,
                    self._llm_structured(FILTER_SYSTEM_PROMPT, filter_prompt, FilterDecision),
                )
                # The LLM only scored; the keep/drop policy runs here. Kept
                # ids come back best-first so the strongest candidate leads
                # the route prompt.
                scored = apply_filter_scores(filter_decision, candidates)
                relevant_qa_ids = [s.qa_id for s in scored if s.kept]
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
                        {
                            "id": c.qa_id,
                            "path": (get_entry(c.qa_id) or {}).get("path", ""),
                            "score": score_permille(c.score),
                            "matched_question": c.matched_question,
                        }
                        for c in candidates
                    ],
                    "filter_scores": [
                        {"id": s.qa_id, "direct": s.direct, "indirect": s.indirect,
                         "relevancy": s.relevancy, "kept": s.kept}
                        for s in scored
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
