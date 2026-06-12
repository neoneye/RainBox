# KNOWN ISSUES (verified, deferred — fix another day)
#
# 1. Config-read discards the socket remainder. main() reads the config line
#    with `config_line, _ = buf.split("\n", 1)` and drops `_`. This is safe
#    *today* only because (a) the supervisor sends exactly one newline-
#    terminated config message and never writes to the agent again
#    (main.py:56), and (b) the agent never reads `sock` again — run() pulls
#    work from Postgres (db.take_item), and the socket is used only for
#    outbound status (sock.sendall). If either ever changes (supervisor sends
#    follow-up commands, or the agent starts reading the socket), bytes that
#    arrived in the same recv() as the config line would be lost; keep the
#    remainder then.
#
# 2. The streaming wall-clock `deadline` is a SOFT bound. The
#    `if time.monotonic() > deadline` check in _structured_call (and in
#    agent_chat_unstructured._stream_reply) sits between generator yields, so
#    it cannot fire while a single `next()` is blocked on a network read. It is
#    not unbounded, though — a stalled read is bounded by the httpx read
#    timeout (OpenAILike.timeout, default 60s; llm.py), and a wedged process is
#    bounded by the supervisor's heartbeat SIGKILL (HEARTBEAT_TIMEOUT=60s,
#    main.py:118). So a strict in-process bound (asyncio/signal/thread) is
#    redundant here. Two real follow-ups remain:
#      - OpenAILike.max_retries defaults to 3 and prepare_llm doesn't override
#        it on the agent path, so a flaky connection retries 3x before
#        surfacing, diluting the deadline (the /models probes already pass
#        max_retries=0). Consider max_retries=0 for agent calls and let the
#        model-group fallback own retries.
#      - The agent emits NO heartbeat during an LLM call (main.py:26-28), so a
#        reasoning model that streams for >60s is SIGKILLed mid-reply.
#        _stream_reply already flushes to the DB ~every 150ms; those flushes
#        could double as heartbeats to keep slow reasoning models alive.

import argparse
import json
import logging
import socket
import sys
import threading
import time
from typing import Any, Callable, cast
from uuid import UUID

from llama_index.core.llms import ChatMessage, MessageRole
from pydantic import BaseModel

import db
from llm import prepare_llm

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
    # well under the supervisor's HEARTBEAT_TIMEOUT (60s, main.py) so a slow turn
    # (e.g. a reasoning model thinking for >60s) isn't SIGKILLed. Class attribute
    # so tests can shrink it.
    HEARTBEAT_INTERVAL: float = 20.0

    def __init__(self, agent_uuid: UUID, name: str, send: StatusSender) -> None:
        self.agent_uuid = agent_uuid
        self.name = name
        self._send = send
        # Serializes socket writes: the heartbeat thread and the main loop both
        # emit status messages, and a raw sendall from two threads can interleave.
        self._send_lock = threading.Lock()

    def _emit(self, msg: dict[str, Any]) -> None:
        """Thread-safe status send to the supervisor."""
        with self._send_lock:
            self._send(msg)

    def _handle_with_heartbeat(
        self, journal_id: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Run handle() while a background thread emits periodic heartbeat status
        messages, so the supervisor's silence-watchdog doesn't SIGKILL a long
        (but healthy) turn. The heartbeat carries no work — it only resets the
        supervisor's last-message timer (any message does)."""
        stop = threading.Event()

        def _beat() -> None:
            while not stop.wait(self.HEARTBEAT_INTERVAL):
                try:
                    self._emit({"status": "heartbeat", "journal_id": journal_id})
                except Exception:
                    return  # socket gone; nothing useful to do from this thread
        hb = threading.Thread(target=_beat, name=f"hb-{self.name}", daemon=True)
        hb.start()
        try:
            return self.handle(journal_id, payload)
        finally:
            stop.set()
            hb.join(timeout=2.0)

    def setup(self) -> None:
        """One-time initialization before the drain loop. Override as needed."""

    def handle(self, journal_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """Process one inbox item; return a JSON-serializable result dict.

        INTENTIONAL TEST STUB — do NOT make this abstract / raise
        NotImplementedError. This functional default is what lets the no-LLM
        pipeline run end-to-end: ModelGroupAgent (below) inherits and extends it,
        and roles with no specialized class (dreamer/critic/verifier — not in
        agent.py's agent_classes map) dispatch straight to that default.
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
                {"status": "processing", "journal_id": journal_id, "payload": payload}
            )
            routing = self._routing_from_payload(payload)
            try:
                # Heartbeat keeps the supervisor from killing a slow-but-healthy
                # handle() (reasoning models can think for >60s with no output).
                result = self._handle_with_heartbeat(journal_id, payload)
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                logger.exception("agent %s: handle failed for journal %s", self.name, journal_id)
                failed_result: dict[str, Any] = {"error": msg}
                # Preserve the dynamic return address on failure too, so a
                # conversation turn that errors still routes back to its manager.
                if routing is not None:
                    failed_result["_routing"] = routing
                db.journal_update(journal_id, "failed", result=failed_result)
                self._emit({"status": "failed", "journal_id": journal_id, "error": msg})
                continue
            if routing is not None and isinstance(result, dict):
                result = {**result, "_routing": routing}
            db.journal_update(journal_id, "completed", result=result)
            self._emit({"status": "completed", "journal_id": journal_id})

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

    def handle(self, journal_id: int, payload: dict[str, Any]) -> dict[str, Any]:
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
        """Run one structured-output call, falling back through the model
        group's members in priority order. Returns the parsed Pydantic
        instance. Raises if there are no candidates or all of them fail.

        An optional `validator` callable is invoked on each successful
        response before returning it; if it raises, the model is treated as
        failed and the loop falls back to the next candidate."""
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
            try:
                _provider_id, model_name, args = db.resolved_model_kwargs(model_uuid)
                logger.info(
                    "agent %s: calling model %s (this loads it into LM Studio if "
                    "it isn't already; a large cold model may take a while)",
                    self.name,
                    model_name,
                )
                t0 = time.monotonic()
                the_llm = prepare_llm(_provider_id, model_name, args)
                sllm = the_llm.as_structured_llm(self.response_model)
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
                timeout_s = float(
                    args.get("request_timeout") or args.get("timeout") or 60.0
                )
                deadline = time.monotonic() + timeout_s
                last = None
                for last in sllm.stream_chat(messages):
                    if time.monotonic() > deadline:
                        raise TimeoutError(
                            f"structured stream exceeded {timeout_s:.0f}s "
                            "(model still generating)"
                        )
                if last is None:
                    raise RuntimeError("structured stream produced no response")
                # .raw is typed Any | None by LlamaIndex; on a successful
                # structured call it's an instance of self.response_model.
                result = cast(BaseModel, last.raw)
                logger.info(
                    "agent %s: model %s responded in %.1fs",
                    self.name,
                    model_name,
                    time.monotonic() - t0,
                )
                if validator is not None:
                    validator(result)
                return result
            except Exception as e:
                last_error = e
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

    def handle(self, journal_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._structured_call(self.user_prompt(payload))
        return {"ok": True, "response": response.model_dump()}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser()
    parser.add_argument("--socket-fd", type=int, required=True)
    args = parser.parse_args()

    sock: socket.socket = socket.socket(fileno=args.socket_fd)

    buf: bytes = b""
    while b"\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            sys.exit("agent: socket closed before config arrived")
        buf += chunk
    config_line, _ = buf.split(b"\n", 1)
    config: dict[str, Any] = json.loads(config_line.decode("utf-8"))
    logger.info("uuid: %s", config["uuid"])
    logger.info("name: %s", config["name"])
    logger.info("description: %s", config.get("description"))

    agent_uuid: UUID = UUID(config["uuid"])

    app = db.make_app()
    app.app_context().push()

    def send(msg: dict[str, Any]) -> None:
        sock.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    # Pick the agent subclass for this role: specialized agents run as their own
    # class, everything else as a plain ModelGroupAgent. Imported here (not at
    # module top) so those modules can import the base classes from this one.
    from agent_chat_structured import StructuredChatAgent
    from agent_chat_unstructured import UnstructuredChatAgent
    from agent_edit_document_v1 import EditDocumentAgentV1
    from agent_edit_document_v2 import EditDocumentAgentV2
    from agent_edit_document_v3 import EditDocumentAgentV3
    from agent_edit_document_v4 import EditDocumentAgentV4
    from agent_edit_document_v5 import EditDocumentAgentV5
    from agent_edit_document_v6 import EditDocumentAgentV6
    from agent_conversation import ConversationManagerAgent
    from agent_followup import FollowUpClassifierAgent
    from agent_kanban_worker import KanbanWorkerAgent
    from agent_mcp import MCPAgent
    from agent_tool_demo import ToolDemoAgent
    from query_agent import QueryAgent
    from query_filter_router_agent import QueryFilterRouterAgent
    from query_router_agent import QueryRouterAgent
    from router_agent import RouterAgent
    from tools.workspace_shell_chat import WorkspaceShellChatAgent

    agent_classes: dict[str, type[Agent]] = {
        "chat_structured": StructuredChatAgent,
        "chat_unstructured": UnstructuredChatAgent,
        "edit_document_v1": EditDocumentAgentV1,
        "edit_document_v2": EditDocumentAgentV2,
        "edit_document_v3": EditDocumentAgentV3,
        "edit_document_v4": EditDocumentAgentV4,
        "edit_document_v5": EditDocumentAgentV5,
        "edit_document_v6": EditDocumentAgentV6,
        "followup": FollowUpClassifierAgent,
        "kanban_worker": KanbanWorkerAgent,
        "tool_demo": ToolDemoAgent,
        "workspace_shell": WorkspaceShellChatAgent,
        "router": RouterAgent,
        "query": QueryAgent,
        "query_router": QueryRouterAgent,
        "query_filter_router": QueryFilterRouterAgent,
        "mcp": MCPAgent,
        "conversation": ConversationManagerAgent,
    }
    # Dispatch on agent_kind when present, else the role name. This lets many
    # roles (e.g. persona_egon / persona_benny) share one implementation class
    # while existing roles (whose name == implementation key) are unaffected.
    kind = config.get("agent_kind", config["name"])
    agent_cls = agent_classes.get(kind, ModelGroupAgent)
    agent = agent_cls(agent_uuid=agent_uuid, name=config["name"], send=send)
    agent.run()

    sock.close()


if __name__ == "__main__":
    main()
