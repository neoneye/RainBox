from typing import Literal, NotRequired, TypedDict
from uuid import UUID


class AgentConfigEntry(TypedDict):
    uuid: UUID
    description: str
    next: UUID | None
    # Which Python agent class to run. Defaults to the role name (so existing
    # roles are unchanged). Lets many roles (e.g. persona_egon, persona_benny)
    # share one implementation class (chat_unstructured). agents/__main__.py
    # dispatches on config.get("agent_kind", config["name"]).
    agent_kind: NotRequired[str]
    # True for agents that drive tool/function calls (e.g. ToolDemoAgent). The
    # /agentmodel page only offers groups that require function calling to
    # these. Independent of requires_structured_output — a model may support
    # both, one, or neither.
    requires_function_calling: NotRequired[bool]
    # True for agents that emit structured output (StructuredLLMAgent /
    # as_structured_llm). The /agentmodel page only offers groups that require
    # structured output to these.
    requires_structured_output: NotRequired[bool]
    # True for agents that need structured output turned OFF, e.g.
    # UnstructuredChatAgent (a plain-text completion). The /agentmodel page only
    # offers groups whose structured-output constraint is "must not have" to
    # these. Mutually exclusive with requires_structured_output.
    excludes_structured_output: NotRequired[bool]
    # Kanban board authority (docs/kanban-design.md "Agent permission model").
    # Enforced by tools/kanban_dispatcher.py, NOT the model prompt. Missing →
    # "observe": an unlisted agent can only read and append comment/suggestion
    # events. "work" adds claim/renew/release/progress events/complete.
    # "shape" (move/create/edit/delete) stays human-only — no entry sets it.
    kanban_authority: NotRequired[Literal["observe", "work", "shape"]]
    # True when the agent's ok=true is ground truth (workspace_shell: exit
    # codes). Unverified agents' successful completes route to a Review-named
    # column instead of Done (kanban_complete_task review=). Flipping this is
    # the promotion mechanism for direct-to-Done.
    kanban_verified: NotRequired[bool]


# Role/kind → "module:ClassName". Values are strings so this table imports
# nothing at module load; resolve_agent_class imports only the one it needs.
AGENT_CLASS_PATHS: dict[str, str] = {
    "assistant": "agents.assistant:AssistantAgent",
    "assistant_run_summarizer": "agents.assistant_run_summarizer:AssistantRunSummarizerAgent",
    "chat_structured": "agents.chat_structured:StructuredChatAgent",
    "chat_unstructured": "agents.chat_unstructured:UnstructuredChatAgent",
    "direct_chat": "agents.direct_chat:DirectChatAgent",
    "edit_document_v1": "agents.edit_document_v1:EditDocumentAgentV1",
    "edit_document_v2": "agents.edit_document_v2:EditDocumentAgentV2",
    "edit_document_v3": "agents.edit_document_v3:EditDocumentAgentV3",
    "edit_document_v4": "agents.edit_document_v4:EditDocumentAgentV4",
    "edit_document_v5": "agents.edit_document_v5:EditDocumentAgentV5",
    "edit_document_v6": "agents.edit_document_v6:EditDocumentAgentV6",
    "followup": "agents.followup:FollowUpClassifierAgent",
    "kanban_worker": "agents.kanban_worker:KanbanWorkerAgent",
    "tool_demo": "agents.tool_demo:ToolDemoAgent",
    "workspace_shell": "tools.workspace_shell_chat:WorkspaceShellChatAgent",
    "router": "agents.router:RouterAgent",
    "query": "agents.query:QueryAgent",
    "query_router": "agents.query_router:QueryRouterAgent",
    "query_filter_router": "agents.query_filter_router:QueryFilterRouterAgent",
    "mcp": "agents.mcp:MCPAgent",
    "conversation": "agents.conversation:ConversationManagerAgent",
}


def resolve_agent_class(kind: str):  # -> type[agents.base.Agent]
    """Import and return the agent class for `kind` (a plain ModelGroupAgent as
    the default). Imports ONLY the selected module, so a spawned agent process
    loads its own dependencies (llama_index etc.) — not all 20 agents'. Used by
    agents/__main__.py to run an agent and by /agentmodel to read class-level
    traits (e.g. uses_model_group)."""
    import importlib

    path = AGENT_CLASS_PATHS.get(kind)
    if path is None:
        from agents.base import ModelGroupAgent

        return ModelGroupAgent
    module_name, class_name = path.split(":")
    return getattr(importlib.import_module(module_name), class_name)


DREAMER_UUID: UUID = UUID("f320e597-c571-411b-994d-65c24b62f972")
CRITIC_UUID: UUID = UUID("40c3b4b4-d883-42a9-bacf-6f77a4cd5f94")
VERIFIER_UUID: UUID = UUID("e9999acb-324b-40c1-9ec6-9047e2fb1935")
FOLLOWUP_UUID: UUID = UUID("25aaf4e9-18f2-41a2-979a-1d30d7844c5a")
CHAT_STRUCTURED_UUID: UUID = UUID("392119a9-2555-42d8-82a2-aa69931882ac")
CHAT_UNSTRUCTURED_UUID: UUID = UUID("6f8b1c0a-9d3e-4a72-bd41-2c7e5f0a9b84")
TOOL_DEMO_UUID: UUID = UUID("953cc2d8-3aa3-4ffe-afc2-99f1c18ebea4")
WORKSPACE_SHELL_UUID: UUID = UUID("672547eb-7ef1-4d72-a0ed-1c17fee80b6e")
KANBAN_WORKER_UUID: UUID = UUID("3e8d2c41-9b7a-4f06-8c52-d14a90b7e6f3")
ROUTER_UUID: UUID = UUID("04707c68-cda4-46e4-929a-48b3f53f7270")
QUERY_UUID: UUID = UUID("cb4a4715-2b57-49fd-802c-0a05818f8b1c")
QUERY_ROUTER_UUID: UUID = UUID("c973bca3-aa92-4a12-af20-d3f1087cac5e")
QUERY_FILTER_ROUTER_UUID: UUID = UUID("218bb954-da6b-4712-9206-4f0f72eafcc0")
EDIT_DOCUMENT_V1_UUID: UUID = UUID("9f3b1a8e-2c5d-4d7a-9e3b-5f8a1c2d4e7b")
EDIT_DOCUMENT_V2_UUID: UUID = UUID("d2a7c5e1-6b3f-4e9a-9c1d-7e4b8f2a3c5d")
EDIT_DOCUMENT_V3_UUID: UUID = UUID("8f4d9b2a-7e3c-4a5b-9c8d-1f6e7d2c4b3a")
EDIT_DOCUMENT_V4_UUID: UUID = UUID("3c1e5a9b-2d4f-4e6a-8b7c-9d0e1f2a3b5c")
EDIT_DOCUMENT_V5_UUID: UUID = UUID("a20fff6b-afbc-48cb-b35a-b090f7088b39")
EDIT_DOCUMENT_V6_UUID: UUID = UUID("4bf3271f-a58f-4dd0-a07f-b85dac906cd0")
MCP_UUID: UUID = UUID("828ae65d-a902-4b4e-bcd3-f761afe23d29")
DIRECT_CHAT_UUID: UUID = UUID("7c2f0d5e-8b4a-4c3d-9e6f-1a2b3c4d5e6f")
ASSISTANT_UUID: UUID = UUID("cad11db6-a8e6-4cdd-a37e-a98bbc53e74d")
ASSISTANT_RUN_SUMMARIZER_UUID: UUID = UUID("5d9a8c74-1e2b-4f3a-bc6d-7a0e9f481c25")

# The assistant's in-flight progress bubble. Posted at enqueue time (the moment a
# human message triggers the assistant) so it appears immediately — before the
# agent process has spawned and imported its stack. kind="progress", so it is
# reaped when the real reply lands and never enters the model transcript.
ASSISTANT_WORKING_NOTICE: str = "💭 Working on it…"
# Persona conversation feature (see docs/proposals/2026-06-08-persona-prompts-...).
# Persona runnable UUIDs MUST match agent_profiles/personas.jsonl. These roles
# are deliberately NOT in webapp.chat_api.CHAT_RESPONDER_UUIDS, so a human post
# never triggers them — only the conversation manager drives them.
PERSONA_EGON_UUID: UUID = UUID("c9e2669f-2d7d-4e7d-827e-e6c7eaf3c2fb")
PERSONA_BENNY_UUID: UUID = UUID("20bcb996-771c-4d87-86e3-28421c0a866b")
CONVERSATION_MANAGER_UUID: UUID = UUID("b0a1c2d3-4e5f-4a6b-8c7d-9e0f1a2b3c4d")

agent_config: dict[str, AgentConfigEntry] = {
    "dreamer": {"uuid": DREAMER_UUID, "description": "generates ideas", "next": CRITIC_UUID},
    "critic": {"uuid": CRITIC_UUID, "description": "evaluates ideas", "next": VERIFIER_UUID},
    "verifier": {"uuid": VERIFIER_UUID, "description": "confirms correctness", "next": None},
    "followup": {
        "uuid": FOLLOWUP_UUID,
        "requires_structured_output": True,
        "description": "classifies whether a chat message needs a follow-up",
        "next": None,
    },
    "chat_structured": {
        "uuid": CHAT_STRUCTURED_UUID,
        "requires_structured_output": True,
        "description": "reads a chatroom's history and decides whether to reply",
        "next": None,
    },
    "chat_unstructured": {
        "uuid": CHAT_UNSTRUCTURED_UUID,
        "excludes_structured_output": True,
        "description": "plain-text sibling of chat: replies with one non-structured completion; needs a model group with 'structured output: must not have'",
        "next": None,
    },
    "tool_demo": {
        "uuid": TOOL_DEMO_UUID,
        "description": "replies in a chatroom using a FunctionAgent with a multiply tool",
        "next": None,
        "requires_function_calling": True,
    },
    "workspace_shell": {
        "uuid": WORKSPACE_SHELL_UUID,
        "description": "runs a chatroom's commands as non-shell argv (no LLM, no bash, workspace-confined)",
        "next": None,
        "kanban_authority": "work",
        "kanban_verified": True,
    },
    "kanban_worker": {
        "uuid": KANBAN_WORKER_UUID,
        "requires_structured_output": True,
        "kanban_authority": "work",
        "description": "LLM kanban worker: claims one card, produces a text deliverable into the event trail via one structured call (status done/unclear/failed), completes into Review (unverified)",
        "next": None,
    },
    "router": {
        "uuid": ROUTER_UUID,
        "requires_structured_output": True,
        "description": "triages a chat message via structured output: a subject summary + whether it needs an action (no LLM tools)",
        "next": None,
    },
    "query": {
        "uuid": QUERY_UUID,
        "description": "answers chat questions from data/question_answer.jsonl via pgvector similarity (no LLM completion, only embeddings)",
        "next": None,
    },
    "query_router": {
        "uuid": QUERY_ROUTER_UUID,
        "requires_structured_output": True,
        "description": "crossover of QueryAgent + RouterAgent: exact alias hits skip the LLM; otherwise the top semantic candidate (with handler output) is fed to the router LLM as a hint",
        "next": None,
    },
    "query_filter_router": {
        "uuid": QUERY_FILTER_ROUTER_UUID,
        "requires_structured_output": True,
        "description": "two-stage LLM: filter top-K candidates for relevance, then a simpler router LLM produces the reply using only the kept candidates",
        "next": None,
    },
    "edit_document_v1": {
        "uuid": EDIT_DOCUMENT_V1_UUID,
        "requires_structured_output": True,
        "description": "given a document and an instruction, returns non-overlapping replace_lines patches in the journal result (does not apply them)",
        "next": None,
    },
    "edit_document_v2": {
        "uuid": EDIT_DOCUMENT_V2_UUID,
        "requires_structured_output": True,
        "description": "planner sibling of edit_document that also returns a status (done/partial/unclear) and a required non-empty comment for the orchestrator",
        "next": None,
    },
    "edit_document_v3": {
        "uuid": EDIT_DOCUMENT_V3_UUID,
        "requires_structured_output": True,
        "description": "third sibling of edit_document: LLM emits one of four high-level patch ops (replace_lines / insert_before / append_text / append_newline) that normalize internally to the v2 replace_lines form for validation and application",
        "next": None,
    },
    "edit_document_v4": {
        "uuid": EDIT_DOCUMENT_V4_UUID,
        "requires_structured_output": True,
        "description": "fourth sibling of edit_document: same four high-level patch ops as v3, but renders the document with a 'logical line' view (trailing newline folded into EOF) and bakes EOF normalization into the returned patches",
        "next": None,
    },
    "edit_document_v5": {
        "uuid": EDIT_DOCUMENT_V5_UUID,
        "requires_structured_output": True,
        "description": "fifth sibling of edit_document: duplicate of v4 reserved for further experimentation",
        "next": None,
    },
    "edit_document_v6": {
        "uuid": EDIT_DOCUMENT_V6_UUID,
        "requires_structured_output": True,
        "description": "sixth sibling of edit_document: same two-op schema as v5 plus a leading `reasoning` field that asks the model to think out loud (10-20 words) before emitting patches",
        "next": None,
    },
    "direct_chat": {
        "uuid": DIRECT_CHAT_UUID,
        "description": "one-to-one operator<->model chat for room_type='direct' rooms: full history as chat messages, one plain-text completion, model + system prompt from the room's own settings (no model group)",
        "next": None,
    },
    "mcp": {
        "uuid": MCP_UUID,
        "description": "chat agent that runs a FunctionAgent with tools sourced from MCP servers (configured in mcp.json + the customize.dir overlay)",
        "next": None,
        "requires_function_calling": True,
    },
    "assistant": {
        "uuid": ASSISTANT_UUID,
        "requires_structured_output": True,
        "description": "rainbox-owned ReAct loop: decides one bounded action per step via structured output, observes, and repeats until a terminal reply or the step cap",
        "next": None,
    },
    "assistant_run_summarizer": {
        "uuid": ASSISTANT_RUN_SUMMARIZER_UUID,
        "requires_structured_output": True,
        "description": "summarizes a completed assistant run (trigger + obstacles + outcome) off the critical path, via one structured call; enqueued by the assistant at every terminal state",
        "next": None,
    },
    # --- persona conversation feature (Phase 0 walking skeleton) ---------------
    # Two personas that run the plain-text chat agent (agent_kind) but carry their
    # own identity + system prompt (resolved from agent_profiles/personas.jsonl).
    # next=None: they return to the manager only via the dynamic return address
    # the manager puts in each turn payload, so a persona stays usable standalone.
    "persona_egon": {
        "uuid": PERSONA_EGON_UUID,
        "agent_kind": "chat_unstructured",
        "excludes_structured_output": True,
        "description": "persona 'Egon' (planner) — runs the plain-text chat agent with a persona system prompt; driven by the conversation manager",
        "next": None,
    },
    "persona_benny": {
        "uuid": PERSONA_BENNY_UUID,
        "agent_kind": "chat_unstructured",
        "excludes_structured_output": True,
        "description": "persona 'Benny' (pragmatic sidekick) — runs the plain-text chat agent with a persona system prompt; driven by the conversation manager",
        "next": None,
    },
    # The bounded conversation turn scheduler. Does no LLM work; needs no model
    # group. role name == implementation key, so no agent_kind is needed.
    "conversation": {
        "uuid": CONVERSATION_MANAGER_UUID,
        "description": "conversation manager: schedules bounded persona-to-persona turns (no LLM); driven by conversation_run state",
        "next": None,
    },
}
