# KNOWN ISSUES (verified, deferred — fix another day)
# (The config-read socket-remainder issue lives atop agents/__main__.py.)
#
# 1. The streaming wall-clock `deadline` is a SOFT bound. The
#    `if time.monotonic() > deadline` check in _structured_call (and in
#    agents/chat_unstructured._stream_reply) sits between generator yields, so
#    it cannot fire while a single `next()` is blocked on a network read. It is
#    not unbounded, though — a stalled read is bounded by the httpx read
#    timeout (OpenAILike.timeout, default 60s; llm.py), and a wedged process is
#    bounded by the supervisor's heartbeat SIGKILL (HEARTBEAT_TIMEOUT in
#    main.py). So a strict in-process bound (asyncio/signal/thread) is
#    redundant here. Two real follow-ups remain:
#      - OpenAILike.max_retries defaults to 3 and prepare_llm doesn't override
#        it on the agent path, so a flaky connection retries 3x before
#        surfacing, diluting the deadline (the /models probes already pass
#        max_retries=0). Consider max_retries=0 for agent calls and let the
#        model-group fallback own retries.

import json
import logging
import threading
import time
from typing import Any, Callable, cast
from uuid import UUID

# NOTE: llama_index is imported lazily inside `_structured_completion` (~0.6s to
# load). Keeping it out of module scope lets a freshly spawned agent process post
# its "working on it" progress row before paying that cost — the import happens on
# the first actual LLM call, which is after the progress row.
from pydantic import BaseModel

import db
# NOTE: `prepare_llm` (and the llm package it lives in) pulls in llama_index
# (~0.6s). Imported lazily inside `_structured_completion` so a freshly spawned
# agent process can post progress before paying that cost.

logger = logging.getLogger(__name__)

StatusSender = Callable[[dict[str, Any]], None]


class RejectedResponse(ValueError):
    """A response that arrived whole and was then rejected — the schema said
    no, or the caller's validator did.

    Distinct from a call that never produced a response (timeout, transport
    error, empty stream) because the two want opposite handling: a rejected
    response is something the model can fix once it is told what was wrong,
    while nothing said to a model that timed out makes the next call faster.
    Only this one earns retries against the same model."""


class Agent:
    """Base class for an inbox-draining agent.

    Owns the lifecycle so subclasses don't have to: pop each inbox item,
    journal it `processing -> completed` (or `failed` on exception), emit a
    status message over the supervisor socket for each transition, and exit
    once the inbox is empty.

    Subclasses customize two hooks:
      - `setup()`  — one-time initialization before draining (load state, look
                     things up in the database, etc.).
      - `handle()` — the actual per-item work; returns a JSON-serializable
                     result dict.

    The base `handle()` is a no-op placeholder (the original `time.sleep(1)` +
    `{"ok": True}` demo behavior), so a plain `Agent` still runs the pipeline.
    """

    # How often the background heartbeat fires while handle() runs. Must stay
    # well under the supervisor's HEARTBEAT_TIMEOUT in main.py (60s) so a slow
    # turn (e.g. a reasoning model thinking for >60s) isn't SIGKILLed. Class
    # attribute so tests can shrink it.
    HEARTBEAT_INTERVAL: float = 20.0

    # Whether the agent consumes the model-group binding chosen on the
    # /agentmodel page. True by default; a subclass that sources its model
    # elsewhere (direct_chat: the room's own settings) or runs no LLM at all
    # (workspace_shell, conversation manager) opts out with False, which
    # hides it from that page.
    uses_model_group: bool = True

    # What this agent's LLM calls are attributed to on /activity, when the
    # agent's own name isn't the right label. Default None = use `self.name`.
    # An agent that exists to serve another one names itself after that one
    # ("assistant.run_summarizer"), so every call an operator thinks of as the
    # assistant's sorts together under one prefix.
    caller_name: str | None = None

    def __init__(self, agent_uuid: UUID, name: str, send: StatusSender) -> None:
        self.agent_uuid = agent_uuid
        self.name = name
        self._send = send
        # Serializes socket writes: the heartbeat thread and the main loop both
        # emit status messages, and a raw sendall from two threads can interleave.
        self._send_lock = threading.Lock()
        self._active_journal_id: UUID | None = None

    def _emit(self, msg: dict[str, Any]) -> None:
        """Thread-safe status send to the supervisor."""
        with self._send_lock:
            self._send(msg)

    def _handle_with_heartbeat(
        self, journal_id: UUID, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Run handle() while a background thread emits periodic heartbeat status
        messages, so the supervisor's silence-watchdog doesn't SIGKILL a long
        (but healthy) turn. The heartbeat carries no work — it only resets the
        supervisor's last-message timer (any message does)."""
        stop = threading.Event()
        self._active_journal_id = journal_id

        def _beat() -> None:
            # Keep beating through a failed send. Returning on the first
            # exception meant one transient hiccup on the status socket
            # silenced the rest of the turn, and the supervisor SIGKILLed a
            # healthy run HEARTBEAT_TIMEOUT later — the failure and the kill
            # far enough apart to look unrelated. A genuinely dead socket
            # raises every time and the watchdog still fires; the difference
            # is that a recoverable one no longer costs the turn.
            while not stop.wait(self.HEARTBEAT_INTERVAL):
                try:
                    self._emit_heartbeat()
                except Exception:
                    logger.warning(
                        "agent %s: heartbeat send failed; still beating",
                        self.name, exc_info=True)
        hb = threading.Thread(target=_beat, name=f"hb-{self.name}", daemon=True)
        hb.start()
        try:
            return self.handle(journal_id, payload)
        finally:
            stop.set()
            hb.join(timeout=2.0)
            self._active_journal_id = None

    def _emit_heartbeat(self) -> None:
        """Emit liveness immediately, used by the timer and step boundaries."""
        if self._active_journal_id is None:
            return
        msg = {
            "status": "heartbeat",
            "journal_id": str(self._active_journal_id),
        }
        msg.update(self._heartbeat_extra())
        self._emit(msg)

    def _heartbeat_extra(self) -> dict[str, Any]:
        """Extra fields merged into each heartbeat. Default empty; agents that do
        multi-step work override this to make heartbeats progress-aware (e.g. the
        current step/activity) so the watchdog can tell a slow-but-working run
        from a hung one."""
        return {}

    def setup(self) -> None:
        """One-time initialization before the drain loop. Override as needed."""

    def handle(self, journal_id: UUID, payload: dict[str, Any]) -> dict[str, Any]:
        """Process one inbox item; return a JSON-serializable result dict.

        INTENTIONAL TEST STUB — do NOT make this abstract / raise
        NotImplementedError. This functional default is what lets the no-LLM
        pipeline run end-to-end: ModelGroupAgent (below) inherits and extends it,
        and roles with no specialized class (dreamer/critic/verifier — not in
        agents/__main__.py's agent_classes dict) dispatch straight to that default.
        """
        time.sleep(1)  # stub: stand-in for real work, exercises the drain loop
        return {"ok": True}

    def run(self) -> None:
        """Drain the inbox to empty, then send a final idle and return."""
        self.setup()
        while True:
            item = db.take_item(self.agent_uuid)
            if item is None:
                self._emit({"status": "idle"})
                time.sleep(1)
                return
            journal_id, payload = item
            self._emit(
                {"status": "processing", "journal_id": str(journal_id), "payload": payload}
            )
            routing = self._routing_from_payload(payload)
            try:
                # Heartbeat keeps the supervisor from killing a slow-but-healthy
                # handle() (reasoning models can think for >60s with no output).
                result = self._handle_with_heartbeat(journal_id, payload)
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                logger.exception("agent %s: handle failed for journal %s", self.name, journal_id)
                # A DB error inside handle() leaves the session's transaction
                # aborted; without a rollback the journal_update below would
                # raise PendingRollbackError and kill the whole supervisor,
                # stranding the item at 'processing' (and any streaming rows
                # open). Clear it so the failure is always journaled.
                db.db.session.rollback()
                failed_result: dict[str, Any] = {"error": msg}
                # Preserve the dynamic return address on failure too, so a
                # conversation turn that errors still routes back to its manager.
                if routing is not None:
                    failed_result["_routing"] = routing
                db.journal_update(journal_id, "failed", result=failed_result)
                self._emit({"status": "failed", "journal_id": str(journal_id), "error": msg})
                continue
            if routing is not None and isinstance(result, dict):
                result = {**result, "_routing": routing}
            db.journal_update(journal_id, "completed", result=result)
            self._emit({"status": "completed", "journal_id": str(journal_id)})

    @staticmethod
    def _routing_from_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
        """Pull the manager-authored dynamic return address out of the INBOX
        payload (never from model output) so the supervisor's routing pass can
        send this completion back to the conversation manager. None for ordinary
        agents whose payloads carry no return address."""
        if not isinstance(payload, dict):
            return None
        return_to = payload.get("return_to_agent_uuid")
        return {"return_to_agent_uuid": return_to} if return_to else None


class ModelGroupAgent(Agent):
    """An agent bound to a model group — a prioritized fallback list of models
    (try the first, fall back to the next on failure).

    Resolves its group from `agent_model_binding` during `setup()`. The real
    LLM call isn't wired in yet, so `handle()` records which models it *would*
    try, in priority order, on the journal result.
    """

    # Extra calls a model gets after a RejectedResponse, before the group
    # falls back to the next candidate. Three because the corrections are
    # cumulative: the second try sees one rejected response, the third sees
    # two, the fourth sees three — past that, a model that has read three of
    # its own failures and repeated them is not one more nudge from getting it
    # right, and the operator is paying a full call per nudge.
    REJECTED_RESPONSE_RETRIES = 3

    # How much of a rejected response is quoted back to the model. Enough to
    # carry any plausible structured answer whole, and a bound on the runaway
    # generation that gets rejected precisely because it never stopped.
    REJECTED_RESPONSE_ECHO_CHARS = 4000

    def __init__(self, agent_uuid: UUID, name: str, send: StatusSender) -> None:
        super().__init__(agent_uuid=agent_uuid, name=name, send=send)
        # Safe defaults so the instance is well-formed before setup() resolves
        # the binding from the database — handle() paths that don't need a
        # model group (e.g. memory commands) must work on a bare instance.
        self.model_group_uuid: UUID | None = None
        self.candidate_model_uuids: list[UUID] = []
        # Input/output token counts + the model uuid of the most recent
        # _structured_completion call (None until one runs). The assistant reads
        # these to record per-step metrics; other agents ignore them.
        self._last_usage: dict[str, int] | None = None
        self._last_model_uuid: UUID | None = None
        # The exact system/user prompt of the most recent decide call, captured
        # at the live-model seam so the assistant can persist the "model request"
        # alongside the step it produced (None until one runs).
        self._last_system_prompt: str | None = None
        self._last_user_prompt: str | None = None
        # The reasoning ("thinking") text the most recent _structured_completion
        # call streamed, captured via instrumentation because the structured
        # wrapper drops it from the parsed result. None for a non-reasoning
        # model (no reasoning channel) or before any call runs; on a failed
        # call it holds the last attempt's partial reasoning (useful when a
        # reasoning model times out mid-think).
        self._last_reasoning: str | None = None
        # Raw provider content from the latest structured call. On an
        # interrupted stream this is the latest partial JSON/text received.
        self._last_response_text: str | None = None

    def setup(self) -> None:
        self.model_group_uuid: UUID | None = None
        self.candidate_model_uuids: list[UUID] = []
        binding = db.get_agent_model_binding(self.agent_uuid)
        if binding is not None and binding.model_group_uuid is not None:
            self.model_group_uuid = binding.model_group_uuid
            self.candidate_model_uuids = db.get_model_group_member_uuids(
                self.model_group_uuid
            )
        logger.info(
            "agent %s bound to model group %s (%d candidate models)",
            self.name,
            self.model_group_uuid,
            len(self.candidate_model_uuids),
        )

    def _model_attempt_started(
        self, model_uuid: UUID, model_name: str, timeout_seconds: float
    ) -> None:
        """Hook for agents that durably track an in-flight model attempt."""

    def _model_attempt_failed(
        self, model_uuid: UUID, model_name: str, error: Exception
    ) -> None:
        """Hook for agents that durably track a failed model attempt."""

    def _model_attempt_progress(
        self,
        model_uuid: UUID,
        model_name: str,
        reasoning: str | None,
        response_text: str | None,
    ) -> None:
        """Hook for throttled persistence of an in-flight model stream."""

    def _caller_tag(self, purpose: str | None = None) -> str:
        """How this call labels itself on /activity: `name` or `name.purpose`.

        Deliberately unprefixed. A blanket `agent.` in front of every row said
        nothing — every row had it — while costing the reader the leading
        characters that actually distinguish one call from the next. What the
        first segment carries instead is the owner: the assistant's calls all
        read `assistant.<something>`, whether the assistant itself made them
        (`assistant.decide`) or an agent working on its behalf did
        (`assistant.run_summarizer`, via `caller_name`).
        """
        base = self.caller_name or self.name
        return f"{base}.{purpose}" if purpose else base

    def handle(self, journal_id: UUID, payload: dict[str, Any]) -> dict[str, Any]:
        # INTENTIONAL STUB — keep functional, do NOT make abstract. This is the
        # *default* dispatch for any role without a specialized class, including
        # the dreamer/critic/verifier demo pipeline; raising here would break
        # them. It isn't a pure mock either — it resolves and reports the real
        # model-group candidates (set in setup), so binding can be verified
        # without an LLM. Real subclasses (StructuredLLMAgent, the chat agents)
        # override this with the actual call.
        time.sleep(1)  # stub: stand-in for the real (fallback) LLM call
        return {
            "ok": True,
            "input": payload,
            "model_group_uuid": str(self.model_group_uuid) if self.model_group_uuid else None,
            "candidate_models": [str(u) for u in self.candidate_model_uuids],
        }

    def _structured_completion(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_model: type[BaseModel],
        validator: Callable[[BaseModel], None] | None = None,
        purpose: str | None = None,
    ) -> BaseModel:
        """Run one structured-output call (system + user message -> a parsed
        `response_model`), falling back through the model group's members in
        priority order. Returns the parsed Pydantic instance. Raises if there
        are no candidates or all of them fail.

        Lives on ModelGroupAgent (not StructuredLLMAgent) so any model-group
        agent that needs *several* structured calls in one handle() — e.g. the
        ReAct AssistantAgent deciding a step at a time — can reuse it with a
        different system prompt / schema per call. StructuredLLMAgent's
        one-shot `_structured_call` is a thin wrapper over this.

        An optional `validator` callable is invoked on each successful response
        before returning it; if it raises, the response is treated as rejected,
        exactly as a schema violation is.

        A rejected response — one that arrived whole and failed the schema or
        the validator — buys the SAME model up to REJECTED_RESPONSE_RETRIES
        more calls before the group falls back to the next candidate, each one
        carrying every response rejected so far and the reason it was rejected
        (see `_rejection_note`). A model that answers `{"reason": null,
        "action": null}` after a page of correct reasoning is one sentence of
        feedback away from a usable answer; the next model in the group, told
        nothing, is more likely to make the same mistake than to fix it. Calls
        that never produced a response (timeout, transport error, empty
        stream) fall through immediately as before — feedback cannot make a
        timed-out model faster.

        `purpose` names what this particular call is for, when an agent makes
        several different ones (the assistant decides a step, asks for a second
        opinion, audits its own reply). It only affects attribution on the
        /activity page — "assistant.decide" is actionable where a bare
        "assistant" is not."""
        from llama_index.core.callbacks import CallbackManager, TokenCountingHandler
        from llama_index.core.instrumentation.dispatcher import instrument_tags
        from llama_index.core.llms import ChatMessage, MessageRole
        from llm import capture_reasoning, prepare_llm

        if not self.candidate_model_uuids:
            raise RuntimeError(
                f"agent {self.name} has no model group / candidate models bound"
            )
        messages = [
            ChatMessage(role=MessageRole.SYSTEM, content=system_prompt),
            ChatMessage(role=MessageRole.USER, content=user_prompt),
        ]
        # Per-call token accounting (PlanExe's structured-LLM pattern): a
        # TokenCountingHandler on the structured LLM captures input/output tokens
        # even though `.raw` is the parsed model, not the usage dict. Reset here so
        # a caller reading self._last_usage after a failed call sees None.
        self._last_usage = None
        self._last_model_uuid = None
        self._last_reasoning = None
        self._last_response_text = None
        token_counter = TokenCountingHandler()
        last_error: Exception | None = None
        for model_uuid in self.candidate_model_uuids:
            model_name = str(model_uuid)
            # The corrective turns this model has earned, appended after the
            # user prompt: each rejected response, then why it was rejected.
            # They accumulate, so the third try sees every earlier mistake and
            # not just the last. Empty on the first call of each model, which
            # is what keeps the prompt — and the provider's cache of its
            # prefix — byte-identical to what a group without retries sends.
            corrections: list[ChatMessage] = []
            rejections = 0
            while True:
                attempt_started = False
                # Per attempt, not per model: a retry that streams nothing
                # must not inherit the rejected text of the one before it, or
                # the trace attributes a response to the call that never
                # produced it.
                self._last_reasoning = None
                self._last_response_text = None
                try:
                    provider_id, model_name, args = db.resolved_model_kwargs(model_uuid)
                    timeout_s = float(
                        args.get("request_timeout") or args.get("timeout") or 60.0
                    )
                    self._last_model_uuid = model_uuid
                    self._model_attempt_started(model_uuid, model_name, timeout_s)
                    attempt_started = True
                    logger.info(
                        "agent %s: calling model %s (provider %s; a cold model may "
                        "take a while)",
                        self.name,
                        model_name,
                        provider_id,
                    )
                    t0 = time.monotonic()
                    the_llm = prepare_llm(provider_id, model_name, args)
                    sllm = the_llm.as_structured_llm(
                        response_model, callback_manager=CallbackManager([token_counter])
                    )
                    # Attribute this call on /activity. The tag rides the
                    # instrumentation events the activity recorder reads, so no
                    # row has to be threaded through by hand.
                    caller_tag = self._caller_tag(purpose)
                    # Consume the structured output as a *stream* (same parsed
                    # result as .chat()) so the underlying tokens are received
                    # incrementally — this is what lets a caller see how much a
                    # reasoning model produced before a timeout, and fires the
                    # per-chunk instrumentation events the reasoning tally reads.
                    #
                    # request_timeout is a per-read timeout, but a streamed response
                    # delivers tokens continuously, so it never trips on a runaway
                    # generation. Bound the whole stream with a wall-clock deadline
                    # instead; abandoning the generator closes the provider stream.
                    deadline = time.monotonic() + timeout_s
                    last = None
                    # Capture the reasoning ("thinking") channel while the stream is
                    # consumed — the structured wrapper drops it from the parsed
                    # result, so instrumentation is the only place it's visible.
                    # Recorded per attempt, even on failure (the partial reasoning
                    # of a timed-out call is exactly what one wants to inspect).
                    with instrument_tags({"caller": caller_tag}), capture_reasoning() as tally:
                        try:
                            for last in sllm.stream_chat(messages + corrections):
                                # Prefer the instrumentation capture: it holds the
                                # provider's true streamed text. A structured
                                # stream's message.content is a dump of the
                                # PARTIALLY PARSED object, which has been seen
                                # dropping free-form dict contents (args: {}).
                                response_text = (
                                    tally.content_text
                                    or getattr(getattr(last, "message", None), "content", None)
                                    or ""
                                )
                                self._last_reasoning = tally.reasoning_text.strip() or None
                                self._last_response_text = response_text.strip() or None
                                self._model_attempt_progress(
                                    model_uuid,
                                    model_name,
                                    self._last_reasoning,
                                    self._last_response_text,
                                )
                                if time.monotonic() > deadline:
                                    raise TimeoutError(
                                        f"structured stream exceeded {timeout_s:.0f}s "
                                        "(model still generating)"
                                    )
                        finally:
                            self._last_reasoning = tally.reasoning_text.strip() or None
                            if not self._last_response_text:
                                self._last_response_text = tally.content_text.strip() or None
                    if last is None:
                        raise RuntimeError("structured stream produced no response")
                    # .raw is typed Any | None by LlamaIndex; on a successful
                    # structured call it's an instance of response_model — but the
                    # streaming partial-parser corrupts it (see
                    # _settle_structured_result), so the provider's true text is
                    # re-validated and wins when it parses.
                    try:
                        result = self._settle_structured_result(
                            response_model,
                            cast(BaseModel, last.raw),
                            self._last_response_text,
                        )
                    except Exception as e:
                        raise RejectedResponse(str(e)) from e
                    logger.info(
                        "agent %s: model %s responded in %.1fs",
                        self.name,
                        model_name,
                        time.monotonic() - t0,
                    )
                    if validator is not None:
                        # A validator raising means the response parsed and is
                        # still unusable — the same kind of failure as a schema
                        # violation, and fixable by the same feedback, so it
                        # earns the same retries.
                        try:
                            validator(result)
                        except Exception as e:
                            raise RejectedResponse(
                                f"the response is valid {response_model.__name__} "
                                f"but was rejected: {type(e).__name__}: {e}"
                            ) from e
                    self._last_usage = {
                        "input": token_counter.prompt_llm_token_count,
                        "output": token_counter.completion_llm_token_count,
                        "ms": int((time.monotonic() - t0) * 1000),
                    }
                    return result
                except Exception as e:
                    last_error = e
                    if attempt_started:
                        self._model_attempt_failed(model_uuid, model_name, e)
                    retries_left = self.REJECTED_RESPONSE_RETRIES - rejections
                    if not isinstance(e, RejectedResponse) or retries_left < 1:
                        logger.warning(
                            "agent %s: model %s failed (%s); trying next in group",
                            self.name,
                            model_uuid,
                            e,
                        )
                        break
                    rejections += 1
                    logger.warning(
                        "agent %s: model %s returned an unusable response (%s); "
                        "asking it again with the reason (%d retr%s left)",
                        self.name,
                        model_uuid,
                        e,
                        retries_left,
                        "y" if retries_left == 1 else "ies",
                    )
                    # The rejected text goes back as the model's own turn and
                    # the reason as the next user turn, which is the shape a
                    # chat model already knows how to act on. Read before the
                    # next attempt overwrites it.
                    echo = self._rejected_response_echo(self._last_response_text)
                    if echo is not None:
                        corrections.append(
                            ChatMessage(role=MessageRole.ASSISTANT, content=echo)
                        )
                    corrections.append(ChatMessage(
                        role=MessageRole.USER,
                        content=self._rejection_note(
                            e, retries_left=retries_left - 1
                        ),
                    ))
        raise RuntimeError(
            f"agent {self.name}: all {len(self.candidate_model_uuids)} models "
            f"in the group failed; last error: {last_error}"
        )

    @classmethod
    def _rejected_response_echo(cls, response_text: str | None) -> str | None:
        """The rejected response, as the assistant turn it is replayed in.

        None when nothing was captured — an attempt that streamed no text has
        no mistake to show, and an empty assistant turn would only invite the
        model to explain what it "said". The middle is what gets dropped from
        an over-long one: a runaway generation goes wrong at the end, and the
        opening is what says which shape it was aiming for."""
        text = (response_text or "").strip()
        if not text:
            return None
        limit = cls.REJECTED_RESPONSE_ECHO_CHARS
        if len(text) <= limit:
            return text
        head = text[: limit // 2].rstrip()
        tail = text[-(limit // 2):].lstrip()
        dropped = len(text) - len(head) - len(tail)
        return f"{head}\n[... {dropped} characters dropped ...]\n{tail}"

    @staticmethod
    def _rejection_note(error: Exception, *, retries_left: int) -> str:
        """The user turn that follows a replayed rejected response: what was
        wrong with it, and that the next answer is not a fresh start.

        A bare tag, with no authority marking. The marking belongs to the
        sections of the one built prompt, where a reader has to be told which
        of several sections binds; this is a message of its own, the newest
        turn in the conversation, arriving after the model's own turn — the
        position a chat model already reads as the live instruction. Built
        through ElementTree so the error text, which quotes the model's own
        rejected output back, cannot close the section or forge another.

        A validator's message is app-owned prose and a schema violation is
        pydantic's; both name the field and what was wrong with it, which is
        the whole content of the feedback. Nothing here restates the schema —
        it is already in the structured-output request the model gets on every
        call, and repeating it invites the model to answer about the schema
        instead of with it."""
        from xml.etree import ElementTree as ET

        last = retries_left < 1
        node = ET.Element("rejected_response")
        node.text = (
            "\nYour last response was rejected and is not part of this "
            "conversation's answer. Why it was rejected:\n\n"
            f"{error}\n\n"
            "Answer the same request again, as one structured response that "
            "does not repeat the fault above. Every required field carries a "
            "real value; none is null, empty, or a placeholder. Fix only what "
            "the rejection names — the rest of what you decided still holds. "
            "Return the structured output alone: no apology, no explanation "
            "of the mistake, no commentary.\n"
        )
        if last:
            node.text += (
                "This is the last attempt. If this response is rejected too, "
                "the request fails with no answer at all.\n"
            )
        else:
            node.text += (
                f"Attempts remaining after this one: {retries_left}.\n"
            )
        return ET.tostring(node, encoding="unicode")

    @staticmethod
    def _settle_structured_result(
        response_model: type[BaseModel],
        stream_parsed: BaseModel,
        final_text: str | None,
    ) -> BaseModel:
        """Pick the final parsed object for a structured stream.

        llama-index's streaming partial-parser has been caught corrupting
        the final object (observed live during the typed-reply trial: a
        free-form args dict came back `{}` in `last.raw` while the
        provider's actual text carried the arguments — the assistant then
        rejected its own python_run six times for a missing `code` it had
        in fact written). The instrumentation capture holds the true
        provider text, so re-validate that — it wins whenever it parses;
        the stream's object is only the fallback for unparseable text.

        The text is tried verbatim first, then as the slice between its
        first '{' and last '}': providers wrap the object in markdown-fence
        remnants ("json\\n{...}" — a live run lost its reply-language call
        to exactly that) or prose, and discarding an otherwise valid
        payload hands the win to the corruptible stream object."""
        text = (final_text or "").strip()
        candidates = [text] if text else []
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            sliced = text[start:end + 1]
            if sliced != text:
                candidates.append(sliced)
        for candidate in candidates:
            try:
                return response_model.model_validate_json(candidate)
            except Exception:
                continue
        if text:
            logger.warning(
                "structured stream: final text did not re-validate; "
                "falling back to the stream-parsed object"
            )
        # The stream object is the fallback, but only when it actually
        # satisfies the schema: the partial parser BUILDS IT WITHOUT
        # VALIDATION, so a required field can arrive as None — pydantic
        # would never produce that. Returning it hands the caller a
        # schema-violating "parsed" object, and the failure then surfaces
        # far away as nonsense (a live run died on "unusable language tag
        # None" inside the classifier code). Failing here is honest: the
        # model-group loop falls through to the next candidate.
        try:
            response_model.model_validate(
                stream_parsed.model_dump(warnings=False))
            return stream_parsed
        except Exception as e:
            raise ValueError(
                f"model did not return a valid {response_model.__name__}: "
                f"the response text did not parse and the streamed object "
                f"violates the schema ({e})"
            ) from e


class StructuredLLMAgent(ModelGroupAgent):
    """A stateless (no conversation history) agent that makes one structured
    LLM call per inbox item.

    Each item produces exactly two messages — the fixed `system_prompt` given
    at construction, and a user prompt derived from the payload — and the
    model's reply must parse against the Pydantic `response_model` via
    `as_structured_llm` (the same path as benchmark.py). Nothing is carried
    between items: every call starts fresh from just these two messages.

    The model comes from the agent's bound model group (resolved by
    ModelGroupAgent.setup). The group is a priority list, so each candidate is
    tried in order until one returns a schema-valid response; if all fail,
    handle() raises and the item is journaled `failed`.
    """

    def __init__(
        self,
        agent_uuid: UUID,
        name: str,
        send: StatusSender,
        system_prompt: str,
        response_model: type[BaseModel],
    ) -> None:
        super().__init__(agent_uuid, name, send)
        self.system_prompt = system_prompt
        self.response_model = response_model

    def user_prompt(self, payload: dict[str, Any]) -> str:
        """Build the user message from the inbox payload. Default: the payload's
        `prompt` field if it's a string, else the payload as compact JSON.
        Override to customize how a task becomes a prompt."""
        prompt = payload.get("prompt")
        if isinstance(prompt, str):
            return prompt
        return json.dumps(payload)

    def _structured_call(
        self,
        user_prompt: str,
        validator: Callable[[BaseModel], None] | None = None,
    ) -> BaseModel:
        """One structured-output call against this agent's fixed `system_prompt`
        and `response_model`. Thin wrapper over
        `ModelGroupAgent._structured_completion` (shared model-group fallback);
        kept as the stable one-shot entry point that subclasses and tests call
        and monkeypatch."""
        return self._structured_completion(
            system_prompt=self.system_prompt,
            user_prompt=user_prompt,
            response_model=self.response_model,
            validator=validator,
        )

    def handle(self, journal_id: UUID, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._structured_call(self.user_prompt(payload))
        return {"ok": True, "response": response.model_dump()}
