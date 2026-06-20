"""Unstructured chat agent — a plain-text sibling of StructuredChatAgent.

Like StructuredChatAgent it reads one chatroom's history (room_uuid from the
inbox payload), injects relevant long-term memories, and posts a reply back
into the room as its own chat_user. The difference is the model call: instead
of `as_structured_llm` it makes a single *plain-text* chat completion — no
structured output, no tools.

Because of that, it subclasses `ModelGroupAgent` directly (not
StructuredLLMAgent), mirroring ToolDemoAgent's shape. It requires its bound
model group to declare "Structured output: Must not have" — its members must
NOT be structured-output models. Function calling and reasoning are "don't
care". This is checked at runtime: setup() warns, and the model call raises if
the constraint isn't satisfied (the agent_models compatibility filter can only
express "must have", so the must-not-have rule lives here).

Shared with the structured agent: the transcript formatter
(chat.transcript.format_history) and memory retrieval
(memory.retrieval.build_chat_memory_block).
"""

import logging
import time
from typing import Any
from uuid import UUID

from llama_index.core.llms import ChatMessage, MessageRole

import db
from memory import retrieval as memory_retrieval
from agents.base import ModelGroupAgent, StatusSender
from chat.streaming import StreamingReplyWriter, extract_stream_deltas
from chat.transcript import format_history
from llm import prepare_llm
from agents.persona import resolve_persona_for_agent

logger = logging.getLogger(__name__)


UNSTRUCTURED_CHAT_SYSTEM_PROMPT: str = """\
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

Respond with a normal chat message in plain text. Do not return JSON and do
not wrap your reply in markdown code fences."""


class UnstructuredChatAgent(ModelGroupAgent):
    """Reads a chatroom's history (room_uuid from the inbox payload), replies to
    the latest message with a single plain-text completion, and posts that reply
    back into the room as its own chat_user. Requires a model group with the
    "structured output: must not have" constraint."""

    def __init__(self, agent_uuid: UUID, name: str, send: StatusSender) -> None:
        super().__init__(agent_uuid, name, send)
        self.system_prompt = UNSTRUCTURED_CHAT_SYSTEM_PROMPT
        self.persona = None
        self.group_excludes_structured_output = False

    def setup(self) -> None:
        super().setup()  # resolves the bound model group + candidate models
        # Persona-as-data: if this runnable identity maps to a persona, use its
        # system prompt instead of the class constant. Non-persona instances
        # (the plain chat_unstructured agent) keep the default — a pure superset.
        self.persona = resolve_persona_for_agent(self.agent_uuid)
        if self.persona is not None:
            self.system_prompt = self.persona.system_prompt
            logger.info("agent %s: using persona %r system prompt", self.name, self.persona.slug)
        group = (
            db.get_model_group(self.model_group_uuid)
            if self.model_group_uuid is not None
            else None
        )
        self.group_excludes_structured_output = bool(
            group is not None
            and group.structured_output_constraint == "must_not_have"
        )
        if not self.group_excludes_structured_output:
            logger.warning(
                "agent %s: its model group does not have the 'structured output: "
                "must not have' constraint — bind it to a group created with that "
                "constraint on /modelgroups",
                self.name,
            )

    @staticmethod
    def _room_uuid(payload: dict[str, Any]) -> UUID:
        raw = payload.get("room_uuid")
        if not raw:
            raise ValueError("unstructured chat agent payload missing 'room_uuid'")
        return raw if isinstance(raw, UUID) else UUID(str(raw))

    def user_prompt(
        self,
        payload: dict[str, Any],
        journal_id: UUID | None = None,
    ) -> str:
        room_uuid = self._room_uuid(payload)
        # Managed persona-to-persona turn: use the conversation context builder
        # (runtime preamble + turn/budget + bounded recent transcript) instead of
        # the generic chat context + memory retrieval.
        if payload.get("run_uuid") and self.persona is not None:
            self._last_retrieval_query = ""
            self._last_retrieved_memories = []
            return self._conversation_user_prompt(payload, room_uuid)
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
        # Stash for `handle` to log as `debug-memory`. Per-instance state is fine
        # because each agent subprocess handles one job at a time.
        self._last_retrieval_query = query
        self._last_retrieved_memories = memories

        transcript = format_history(messages)
        if memory_block:
            return f"{memory_block}\n\n{transcript}"
        return transcript

    def _conversation_user_prompt(self, payload: dict[str, Any], room_uuid: UUID) -> str:
        """Per-turn prompt for a persona inside a managed conversation: a runtime
        preamble (who you are, who else is present, the turn budget, the DONE
        contract) plus the last `CONVO_LAST_N` visible turns. Manager-authored
        rows (debug + summaries) are excluded so the personas converse with each
        other, not with the scheduler."""
        from agents.conversation import (
            CONVO_LAST_N,
            build_conversation_prompt,
        )
        from agents.config import CONVERSATION_MANAGER_UUID

        run = db.get_conversation_run(payload.get("run_uuid"))
        mgr = str(CONVERSATION_MANAGER_UUID)
        visible = [
            m for m in db.list_room_messages(room_uuid)
            if m.get("kind") == "message" and m.get("sender_uuid") != mgr
        ]
        transcript = format_history(visible[-CONVO_LAST_N:]) if visible else ""
        policy = (run.turn_policy if run is not None else None) or {}
        participants = (run.participants if run is not None else None) or []
        other_names: list[str] = []
        for p in participants:
            pid = p.get("agent_uuid")
            if not pid or pid == str(self.agent_uuid):
                continue
            other = resolve_persona_for_agent(UUID(pid))
            other_names.append(other.name if other is not None else (p.get("slug") or "agent"))
        speaker_name = self.persona.name if self.persona is not None else self.name
        turn = payload.get("turn", run.turn if run is not None else 0)
        return build_conversation_prompt(
            speaker_name, other_names, turn, policy.get("max_turns"), transcript
        )

    def _make_writer(self, room_uuid: UUID) -> StreamingReplyWriter:
        """A StreamingReplyWriter that creates/updates this agent's rows in the
        room. The reasoning row is kind="thinking" (persists, shown expanded);
        the answer row is kind="message"."""
        def create(kind: str, streaming: bool) -> int:
            return db.post_chat_message(
                room_uuid, self.agent_uuid, "", content_type="markdown",
                kind=kind, streaming=streaming,
            ).id

        def update(message_id: int, text: str, streaming: bool) -> None:
            db.update_chat_message(message_id, text, streaming=streaming)

        return StreamingReplyWriter(create=create, update=update)

    @staticmethod
    def _answer_from_reasoning(reasoning: str) -> str:
        """Recover an answer that a model emitted inside its reasoning channel:
        the text after the last </think>. Empty if there's no such tail."""
        idx = reasoning.rfind("</think>")
        return reasoning[idx + len("</think>"):].strip() if idx != -1 else ""

    def _stream_reply(self, room_uuid: UUID, user_prompt: str) -> str:
        """Stream a plain-text reply into live thinking/answer rows, falling
        back through the model group's members for *startup* failures. Once a
        model starts producing tokens we commit to it (no re-posting under a
        different model). Raises if the group lacks the must-not-have-structured
        constraint, there are no candidates, or every candidate fails to start.

        A wall-clock deadline bounds the whole stream (a per-read timeout would
        never trip on a continuously-streaming model)."""
        if not self.group_excludes_structured_output:
            raise RuntimeError(
                f"agent {self.name} needs a model group with the 'structured "
                "output: must not have' constraint. Create one on /modelgroups "
                "and bind it to this agent on /agent_models."
            )
        if not self.candidate_model_uuids:
            raise RuntimeError(
                f"agent {self.name} has no model group / candidate models bound"
            )
        messages = [
            ChatMessage(role=MessageRole.SYSTEM, content=self.system_prompt),
            ChatMessage(role=MessageRole.USER, content=user_prompt),
        ]
        last_error: Exception | None = None
        for model_uuid in self.candidate_model_uuids:
            _provider_id, model_name, args = db.resolved_model_kwargs(model_uuid)
            logger.info(
                "agent %s: streaming from model %s (this loads it into LM Studio "
                "if it isn't already; a large cold model may take a while)",
                self.name, model_name,
            )
            t0 = time.monotonic()
            timeout_s = float(args.get("request_timeout") or args.get("timeout") or 60.0)
            writer = self._make_writer(room_uuid)
            reasoning_text = ""
            try:
                the_llm = prepare_llm(_provider_id, model_name, args)
                stream = the_llm.stream_chat(messages)
                deadline = time.monotonic() + timeout_s
                for chunk in stream:
                    if time.monotonic() > deadline:
                        raise TimeoutError(
                            f"chat stream exceeded {timeout_s:.0f}s "
                            "(model still generating)"
                        )
                    reasoning_delta, content_delta = extract_stream_deltas(chunk)
                    reasoning_text += reasoning_delta
                    writer.add_reasoning(reasoning_delta)
                    writer.add_answer(content_delta)
                # Recover an answer the model put inside its reasoning channel.
                final_answer = self._answer_from_reasoning(reasoning_text) \
                    if writer.answer_id is None else None
                reply = writer.finish(final_answer=final_answer).strip()
                logger.info(
                    "agent %s: model %s finished in %.1fs (%d reply chars)",
                    self.name, model_name, time.monotonic() - t0, len(reply),
                )
                return reply
            except Exception as e:
                last_error = e
                if writer.reasoning_id is not None or writer.answer_id is not None:
                    # Already streaming visible output — don't re-post under
                    # another model. Close the rows and surface the failure.
                    writer.finish()
                    logger.warning(
                        "agent %s: model %s failed mid-stream (%s)",
                        self.name, model_uuid, e,
                    )
                    raise
                logger.warning(
                    "agent %s: model %s failed before producing output (%s); "
                    "trying next in group", self.name, model_uuid, e,
                )
        raise RuntimeError(
            f"agent {self.name}: all {len(self.candidate_model_uuids)} models "
            f"in the group failed; last error: {last_error}"
        )

    def handle(self, journal_id: UUID, payload: dict[str, Any]) -> dict[str, Any]:
        room_uuid = self._room_uuid(payload)
        user_prompt = self.user_prompt(payload, journal_id=journal_id)
        logger.info(
            "unstructured chat agent prompts for room %s:\n"
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
        # No progress placeholder: the live reasoning bubble supersedes it.
        reply = self._stream_reply(room_uuid, user_prompt)
        if not reply:
            logger.warning(
                "unstructured chat agent produced an empty reply in room %s",
                room_uuid,
            )
        result: dict[str, Any] = {"ok": True, "reply_content": reply}
        if self.persona is not None:
            # Provenance: which persona + prompt produced this turn, for
            # reproducibility (Phase 0 records it on the journal result).
            result["persona_id"] = str(self.persona.persona_id)
            result["prompt_sha256"] = self.persona.prompt_sha256
        return result
