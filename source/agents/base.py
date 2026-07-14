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
            while not stop.wait(self.HEARTBEAT_INTERVAL):
                try:
                    self._emit_heartbeat()
                except Exception:
                    return  # socket gone; nothing useful to do from this thread
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
        before returning it; if it raises, the model is treated as failed and
        the loop falls back to the next candidate."""
        from llama_index.core.callbacks import CallbackManager, TokenCountingHandler
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
            attempt_started = False
            try:
                _provider_id, model_name, args = db.resolved_model_kwargs(model_uuid)
                timeout_s = float(
                    args.get("request_timeout") or args.get("timeout") or 60.0
                )
                self._last_model_uuid = model_uuid
                self._model_attempt_started(model_uuid, model_name, timeout_s)
                attempt_started = True
                logger.info(
                    "agent %s: calling model %s (this loads it into LM Studio if "
                    "it isn't already; a large cold model may take a while)",
                    self.name,
                    model_name,
                )
                t0 = time.monotonic()
                the_llm = prepare_llm(_provider_id, model_name, args)
                sllm = the_llm.as_structured_llm(
                    response_model, callback_manager=CallbackManager([token_counter])
                )
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
                with capture_reasoning() as tally:
                    try:
                        for last in sllm.stream_chat(messages):
                            response_text = (
                                getattr(getattr(last, "message", None), "content", None)
                                or tally.content_text
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
                # structured call it's an instance of response_model.
                result = cast(BaseModel, last.raw)
                logger.info(
                    "agent %s: model %s responded in %.1fs",
                    self.name,
                    model_name,
                    time.monotonic() - t0,
                )
                if validator is not None:
                    validator(result)
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
                logger.warning(
                    "agent %s: model %s failed (%s); trying next in group",
                    self.name,
                    model_uuid,
                    e,
                )
        raise RuntimeError(
            f"agent {self.name}: all {len(self.candidate_model_uuids)} models "
            f"in the group failed; last error: {last_error}"
        )


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
