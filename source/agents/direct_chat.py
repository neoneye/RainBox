"""Direct LLM chat agent — the responder for room_type='direct' chatrooms.

A direct room is a one-to-one conversation between the operator and a single
model: the model sees the room history (all of it, or the newest
history_window kind="message" rows) as proper system/user/assistant chat
messages (not the IRC-style transcript the group-chat agents get), and
replies with one plain-text completion. No
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
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from llama_index.core.llms import ChatMessage, MessageRole
from sqlalchemy.orm import Session

import db
from agents.base import Agent
from agents.turn_stop import StopRequested, StopWatch, TurnStopped
from chat.generation_stats import GenerationStats
from chat.streaming import StreamingReplyWriter, extract_stream_deltas
from llm import prepare_llm

logger = logging.getLogger(__name__)

_END = object()  # `next(chunks, _END)`: the stream is exhausted

NO_MODEL_NOTICE: str = (
    "No model selected for this chat, and no global default is available. "
    "Open the right panel, choose “Settings”, and pick a model — or set "
    "chat.default_model on the /config page."
)


class DirectChatAgent(Agent):
    """Replies in a direct room: full history in, one streamed plain-text
    completion out. The triggering payload is {room_uuid, message_uuid}."""

    # The model comes from the room's settings (or chat.default_model), never
    # from an /agentmodel binding — keep this agent off that page.
    uses_model_group = False

    @staticmethod
    def _room_uuid(payload: dict[str, Any]) -> UUID:
        raw = payload.get("room_uuid")
        if not raw:
            raise ValueError("direct chat agent payload missing 'room_uuid'")
        return raw if isinstance(raw, UUID) else UUID(str(raw))

    @staticmethod
    def build_messages(
        system_prompt: str, history: list[dict[str, Any]],
        window: int | None = None,
    ) -> list[ChatMessage]:
        """The LLM message list: optional system message (blank prompt = none),
        then kind='message' rows oldest-first — human rows as `user`,
        everything else as `assistant`. `window` keeps only the newest that
        many message rows (the room's history_window); None = every row. The
        system message is never counted. The triggering message is simply
        the last user row."""
        messages: list[ChatMessage] = []
        if system_prompt.strip():
            messages.append(
                ChatMessage(role=MessageRole.SYSTEM, content=system_prompt)
            )
        rows = [m for m in history if m.get("kind") == "message"]
        if window is not None and window > 0:
            rows = rows[-window:]
        for m in rows:
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
        (same shape as the unstructured chat agent).

        The thinking row carries meta["turn_started_at"]: when the turn's
        working bubble started (the operator's send), read now, because that
        bubble is reaped once the reply lands. The chat's "Worked for" line
        counts from it, so it doesn't restart at zero when the reasoning row
        takes over from the bubble."""
        started = db.progress_started_at(room_uuid, self.agent_uuid) \
            or datetime.now(UTC)

        def create(kind: str, streaming: bool) -> int:
            meta = {"turn_started_at": started.isoformat()} \
                if kind == "thinking" else None
            return db.post_chat_message(
                room_uuid, self.agent_uuid, "", content_type="markdown",
                kind=kind, streaming=streaming, meta=meta,
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

    @staticmethod
    def _stop_probe(journal_id: UUID | None) -> Callable[[], bool] | None:
        """What the StopWatch polls: has the operator flagged this turn's
        journal row. Reads on a session of its own, bound to the engine
        captured here on the main thread — the watcher runs on a thread
        with no app context, and the scoped db.session must never be shared
        across threads (the activity recorder documents why). None, an
        inert watch, when the turn has no journal (tests driving
        _stream_reply directly)."""
        if journal_id is None:
            return None
        engine = db.db.engine

        def probe() -> bool:
            with Session(bind=engine) as session:
                return db.stop_requested(journal_id, session=session)
        return probe

    def _stream_reply(
        self, room_uuid: UUID, model_uuid: UUID, messages: list[ChatMessage],
        request_timeout: int | None = None, journal_id: UUID | None = None,
    ) -> str:
        """Stream one completion from the room's model into live
        thinking/answer rows. `request_timeout` (the room's Settings override,
        seconds) beats the model config's request_timeout/timeout and the 60s
        fallback; it is also handed to the HTTP client so the socket read
        timeout grows with it. Single model — no fallback list; any failure
        closes the streaming rows, posts a kind="notice" failure message into
        the room (the journal's `failed` status is invisible in the chat UI),
        and raises (the item still journals `failed`).

        A Stop request on `journal_id` (agents/turn_stop.py) interrupts the
        stream — mid-read included — keeps the text that streamed, settles
        the rows, posts a stop notice, and raises TurnStopped (journal
        `stopped`)."""
        t0 = time.monotonic()
        writer = self._make_writer(room_uuid)
        reasoning_text = ""
        model_name = None
        stream: Any = None
        watch = StopWatch(self._stop_probe(journal_id))
        try:
            provider_id, model_name, args = db.resolved_model_kwargs(model_uuid)
            logger.info(
                "agent %s: streaming from model %s (provider %s; a cold model "
                "may take a while)",
                self.name, model_name, provider_id,
            )
            if request_timeout:
                # The HTTP-client field differs per path: native Ollama reads
                # request_timeout, the OpenAI-compat clients read timeout
                # (and silently ignore request_timeout).
                key = "request_timeout" if provider_id == "ollama" else "timeout"
                args[key] = request_timeout
            timeout_s = float(
                request_timeout
                or args.get("request_timeout") or args.get("timeout") or 60.0
            )
            the_llm = prepare_llm(provider_id, model_name, args)
            # The two calls that block on the network sit in interruptible
            # windows: opening the stream (a cold model sends nothing until
            # it has loaded) and each read. Between reads — the writer's
            # database flushes — a stop waits for the next boundary.
            # OpenAI-compatible servers report token usage on a stream only
            # when asked; native Ollama always does, and rejects unknown args.
            stream_kwargs: dict[str, Any] = {} if provider_id == "ollama" \
                else {"stream_options": {"include_usage": True}}
            stats = GenerationStats()
            with watch:
                with watch.interruptible():
                    stream = the_llm.stream_chat(messages, **stream_kwargs)
                deadline = time.monotonic() + timeout_s
                chunks = iter(stream)
                while True:
                    watch.raise_if_requested()
                    with watch.interruptible():
                        chunk = next(chunks, _END)
                    if chunk is _END:
                        break
                    if time.monotonic() > deadline:
                        raise TimeoutError(
                            f"chat stream exceeded {timeout_s:.0f}s "
                            "(model still generating)"
                        )
                    reasoning_delta, content_delta = extract_stream_deltas(chunk)
                    stats.observe(chunk, bool(reasoning_delta or content_delta))
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
            if writer.answer_id is not None:
                # The tok/s badge under the reply. Best-effort: the reply
                # has landed, and a stats write must not turn it into a failure.
                try:
                    db.set_chat_message_generation(
                        writer.answer_id, stats.result())
                except Exception:
                    db.session.rollback()
                    logger.exception(
                        "agent %s: could not store generation stats", self.name)
            return reply
        except StopRequested:
            # The operator pressed Stop. Close the generator (its HTTP
            # connection with it — the server stops generating), keep what
            # streamed with the same answer recovery as a normal finish,
            # mark the cut with a notice posted as this agent (it reaps the
            # working bubble), and hand the queue a `stopped`.
            close = getattr(stream, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:
                    logger.debug("agent %s: closing the stream raised",
                                 self.name, exc_info=True)
            partial = ""
            try:
                final_answer = self._answer_from_reasoning(reasoning_text) \
                    if writer.answer_id is None else None
                partial = writer.finish(final_answer=final_answer).strip()
            except Exception:
                db.session.rollback()
                logger.exception(
                    "agent %s: could not settle rows after stop", self.name)
            elapsed = time.monotonic() - t0
            logger.info(
                "agent %s: stopped by operator after %.1fs (%d reply chars)",
                self.name, elapsed, len(partial),
            )
            try:
                db.post_chat_message(
                    room_uuid, self.agent_uuid,
                    f"⏹ Stopped — after {elapsed:.0f}s "
                    f"(model {model_name or model_uuid})",
                    kind="notice",
                )
            except Exception:
                db.session.rollback()
                logger.exception(
                    "agent %s: could not post stop notice", self.name)
            raise TurnStopped(partial) from None
        except Exception as exc:
            # Close any live rows so the UI doesn't show a stuck cursor. A DB
            # error mid-flush leaves the transaction aborted — roll it back
            # first so these closing writes can land, and keep them
            # best-effort so the original error is what propagates.
            db.session.rollback()
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
        the preferred model-config override, with Ollama first). None when it
        is unset or no longer resolves to a model."""
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
            db.resolve_room_system_prompt(room), history,
            window=room.history_window,
        )
        reply = self._stream_reply(
            room_uuid, model_uuid, messages,
            request_timeout=room.request_timeout, journal_id=journal_id,
        )
        if not reply:
            logger.warning(
                "direct chat agent produced an empty reply in room %s", room_uuid
            )
        return {"ok": True, "reply_content": reply}
