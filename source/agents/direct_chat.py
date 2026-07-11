"""Direct LLM chat agent — the responder for room_type='direct' chatrooms.

A direct room is a one-to-one conversation between the operator and a single
model, LM Studio-style: the model sees the ENTIRE room history as proper
system/user/assistant chat messages (not the IRC-style transcript the
group-chat agents get), and replies with one plain-text completion. No
structured output, no tools, no memory retrieval, no persona.

Unlike the other LLM agents it is NOT a ModelGroupAgent: the model comes from
the room row itself (Chatroom.model_uuid — a ModelConfig or
ModelConfigOverride uuid, chosen in the /chat Settings sidebar), falling back
to the global chat.default_model setting when the room has none. The system
prompt is the room's system_prompt (empty = no system message). Both are read
fresh each turn, so changing them mid-conversation applies from the next
turn on.
"""

import logging
import time
from typing import Any
from uuid import UUID

from llama_index.core.llms import ChatMessage, MessageRole

import db
from agents.base import Agent
from chat.streaming import StreamingReplyWriter, extract_stream_deltas
from llm import prepare_llm

logger = logging.getLogger(__name__)

NO_MODEL_NOTICE: str = (
    "No model selected for this chat, and no global default is available. "
    "Open the right panel, choose “Settings”, and pick a model — or set "
    "chat.default_model on the /settings page."
)


class DirectChatAgent(Agent):
    """Replies in a direct room: full history in, one streamed plain-text
    completion out. The triggering payload is {room_uuid, message_uuid}."""

    # The model comes from the room's settings (or chat.default_model), never
    # from an /agent_models binding — keep this agent off that page.
    uses_model_group = False

    @staticmethod
    def _room_uuid(payload: dict[str, Any]) -> UUID:
        raw = payload.get("room_uuid")
        if not raw:
            raise ValueError("direct chat agent payload missing 'room_uuid'")
        return raw if isinstance(raw, UUID) else UUID(str(raw))

    @staticmethod
    def build_messages(
        system_prompt: str, history: list[dict[str, Any]]
    ) -> list[ChatMessage]:
        """The LLM message list: optional system message (blank prompt = none),
        then every kind='message' row oldest-first — human rows as `user`,
        everything else as `assistant`. The triggering message is simply the
        last user row; no window is applied (the model sees the whole room)."""
        messages: list[ChatMessage] = []
        if system_prompt.strip():
            messages.append(
                ChatMessage(role=MessageRole.SYSTEM, content=system_prompt)
            )
        for m in history:
            if m.get("kind") != "message":
                continue
            role = (
                MessageRole.USER
                if m.get("sender_type") == "human"
                else MessageRole.ASSISTANT
            )
            messages.append(ChatMessage(role=role, content=m.get("text", "")))
        return messages

    def _make_writer(self, room_uuid: UUID) -> StreamingReplyWriter:
        """A StreamingReplyWriter creating/updating this agent's rows in the
        room: kind="thinking" for reasoning, kind="message" for the answer
        (same shape as the unstructured chat agent)."""
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
        """Recover an answer a model emitted inside its reasoning channel: the
        text after the last </think>. Empty if there's no such tail."""
        idx = reasoning.rfind("</think>")
        return reasoning[idx + len("</think>"):].strip() if idx != -1 else ""

    def _stream_reply(
        self, room_uuid: UUID, model_uuid: UUID, messages: list[ChatMessage]
    ) -> str:
        """Stream one completion from the room's model into live
        thinking/answer rows. Single model — no fallback list; any failure
        closes the streaming rows, posts a kind="notice" failure message into
        the room (the journal's `failed` status is invisible in the chat UI),
        and raises (the item still journals `failed`)."""
        t0 = time.monotonic()
        writer = self._make_writer(room_uuid)
        reasoning_text = ""
        model_name = None
        try:
            provider_id, model_name, args = db.resolved_model_kwargs(model_uuid)
            logger.info(
                "agent %s: streaming from model %s (this loads it into the "
                "provider if it isn't already; a large cold model may take a while)",
                self.name, model_name,
            )
            timeout_s = float(
                args.get("request_timeout") or args.get("timeout") or 60.0
            )
            the_llm = prepare_llm(provider_id, model_name, args)
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
            final_answer = self._answer_from_reasoning(reasoning_text) \
                if writer.answer_id is None else None
            reply = writer.finish(final_answer=final_answer).strip()
            logger.info(
                "agent %s: model %s finished in %.1fs (%d reply chars)",
                self.name, model_name, time.monotonic() - t0, len(reply),
            )
            return reply
        except Exception as exc:
            # Close any live rows so the UI doesn't show a stuck cursor. A DB
            # error mid-flush leaves the transaction aborted — roll it back
            # first so these closing writes can land, and keep them
            # best-effort so the original error is what propagates.
            db.db.session.rollback()
            if writer.reasoning_id is not None or writer.answer_id is not None:
                try:
                    writer.finish()
                except Exception:
                    logger.exception(
                        "agent %s: could not close streaming rows", self.name
                    )
            # Surface the failure in the room itself — without this a turn
            # that dies before its first token (e.g. a ReadTimeout while a
            # cold model loads) leaves the chat silent. kind="notice" is
            # excluded from transcripts, so the model never sees it.
            try:
                db.post_chat_message(
                    room_uuid, self.agent_uuid,
                    f"⚠️ Reply failed — {type(exc).__name__}: {exc} "
                    f"(model {model_name or model_uuid}, "
                    f"after {time.monotonic() - t0:.0f}s)",
                    kind="notice",
                )
            except Exception:
                logger.exception(
                    "agent %s: could not post failure notice", self.name
                )
            raise

    @staticmethod
    def _default_model_uuid() -> UUID | None:
        """The global default model for rooms with none selected: the
        chat.default_model setting (an explicit value, or its dynamic default —
        the alphabetically earliest model config override). None when it is
        unset or no longer resolves to a model."""
        raw = db.get_setting("chat.default_model")
        if not raw:
            return None
        try:
            target = UUID(str(raw))
            db.resolved_model_kwargs(target)
            return target
        except (ValueError, LookupError):
            logger.warning(
                "chat.default_model %r does not resolve to a model; ignoring",
                raw,
            )
            return None

    def handle(self, journal_id: UUID, payload: dict[str, Any]) -> dict[str, Any]:
        room_uuid = self._room_uuid(payload)
        room = db.get_chatroom(room_uuid)
        if room is None:
            raise ValueError(f"chatroom {room_uuid} not found")
        if room.room_type != "direct":
            raise ValueError(
                f"room {room_uuid} is type {room.room_type!r}, not 'direct'"
            )
        model_uuid = room.model_uuid or self._default_model_uuid()
        if model_uuid is None:
            # Friendly nudge instead of a failed journal. kind="notice" is
            # excluded from transcripts, so the model never sees it.
            db.post_chat_message(
                room_uuid, self.agent_uuid, NO_MODEL_NOTICE, kind="notice"
            )
            return {"ok": True, "notice": "no_model"}
        history = db.list_room_messages(room_uuid)
        messages = self.build_messages(
            db.resolve_room_system_prompt(room), history
        )
        reply = self._stream_reply(room_uuid, model_uuid, messages)
        if not reply:
            logger.warning(
                "direct chat agent produced an empty reply in room %s", room_uuid
            )
        return {"ok": True, "reply_content": reply}
