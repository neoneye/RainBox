from typing import Literal, NotRequired, TypedDict
from uuid import UUID


class AgentConfigEntry(TypedDict):
    uuid: UUID
    description: str
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
    # Kanban board authority (notes/kanban-design.md "Agent permission model").
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
    "assistant.run_summarizer": "agents.assistant_run_summarizer:AssistantRunSummarizerAgent",
    "chat_structured": "agents.chat_structured:StructuredChatAgent",
    "chat_unstructured": "agents.chat_unstructured:UnstructuredChatAgent",
    "direct_chat": "agents.direct_chat:DirectChatAgent",
    "edit_document_v1": "agents.edit_document_v1:EditDocumentAgentV1",
    "edit_document_v2": "agents.edit_document_v2:EditDocumentAgentV2",
    "edit_document_v3": "agents.edit_document_v3:EditDocumentAgentV3",
    "edit_document_v4": "agents.edit_document_v4:EditDocumentAgentV4",
    "edit_document_v5": "agents.edit_document_v5:EditDocumentAgentV5",
    "edit_document_v6": "agents.edit_document_v6:EditDocumentAgentV6",
    "kanban_worker": "agents.kanban_worker:KanbanWorkerAgent",
    "tool_demo": "agents.tool_demo:ToolDemoAgent",
    "workspace_shell": "tools.workspace_shell_chat:WorkspaceShellChatAgent",
    "mcp": "agents.mcp:MCPAgent",
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


CHAT_STRUCTURED_UUID: UUID = UUID("392119a9-2555-42d8-82a2-aa69931882ac")
CHAT_UNSTRUCTURED_UUID: UUID = UUID("6f8b1c0a-9d3e-4a72-bd41-2c7e5f0a9b84")
TOOL_DEMO_UUID: UUID = UUID("953cc2d8-3aa3-4ffe-afc2-99f1c18ebea4")
WORKSPACE_SHELL_UUID: UUID = UUID("672547eb-7ef1-4d72-a0ed-1c17fee80b6e")
KANBAN_WORKER_UUID: UUID = UUID("3e8d2c41-9b7a-4f06-8c52-d14a90b7e6f3")
EDIT_DOCUMENT_V1_UUID: UUID = UUID("9f3b1a8e-2c5d-4d7a-9e3b-5f8a1c2d4e7b")
EDIT_DOCUMENT_V2_UUID: UUID = UUID("d2a7c5e1-6b3f-4e9a-9c1d-7e4b8f2a3c5d")
EDIT_DOCUMENT_V3_UUID: UUID = UUID("8f4d9b2a-7e3c-4a5b-9c8d-1f6e7d2c4b3a")
EDIT_DOCUMENT_V4_UUID: UUID = UUID("3c1e5a9b-2d4f-4e6a-8b7c-9d0e1f2a3b5c")
EDIT_DOCUMENT_V5_UUID: UUID = UUID("a20fff6b-afbc-48cb-b35a-b090f7088b39")
EDIT_DOCUMENT_V6_UUID: UUID = UUID("4bf3271f-a58f-4dd0-a07f-b85dac906cd0")
MCP_UUID: UUID = UUID("828ae65d-a902-4b4e-bcd3-f761afe23d29")
DIRECT_CHAT_UUID: UUID = UUID("7c2f0d5e-8b4a-4c3d-9e6f-1a2b3c4d5e6f")
ASSISTANT_UUID: UUID = UUID("cad11db6-a8e6-4cdd-a37e-a98bbc53e74d")

# One model slot per model call the assistant makes, named exactly as that call
# labels itself on /activity (agents.base.Agent._caller_tag). /agentmodel and
# /activity therefore share one vocabulary: the row an operator binds is the
# row they later read the cost and latency of.
#
# Every slot resolves through the same two-link chain — the slot itself, then
# ASSISTANT_DEFAULT_UUID — so binding nothing but the default configures the
# whole assistant, and binding one slot moves exactly one call. There is no
# third level: an unbound default means no candidates, which each call site
# already handles (the loop raises; the optional calls record a skipped step).
ASSISTANT_DEFAULT_UUID: UUID = UUID("c4321506-e536-4217-95f3-8801d8b860f9")
ASSISTANT_DECIDE_UUID: UUID = UUID("88c125c3-527a-4875-a361-76d5754dde0f")
ASSISTANT_ACCEPTANCE_CRITERIA_UUID: UUID = UUID(
    "02cd99a0-c073-4f34-aef3-577138f09800")
ASSISTANT_REQUEST_SUMMARY_UUID: UUID = UUID(
    "5c213272-b53f-44db-a68d-d7be2f990e81")
ASSISTANT_MEMORY_FILTER_UUID: UUID = UUID("b4809a3f-12d6-4725-ab23-4808cec2d5d7")
ASSISTANT_SECOND_OPINION_UUID: UUID = UUID("7a1d4c3e-5b2f-4e8a-9c6d-0f3b8a51e274")
ASSISTANT_RESPONSE_LANGUAGE_CLASSIFIER_UUID: UUID = UUID(
    "6d4ef68c-8b63-4f55-b704-b3a2b416d9a7")
ASSISTANT_REPLY_AUDIT_UUID: UUID = UUID("5c8e3a17-4d92-4b6f-8a30-1e7c9f2b45d8")
ASSISTANT_RUN_SUMMARIZER_UUID: UUID = UUID("5d9a8c74-1e2b-4f3a-bc6d-7a0e9f481c25")

# The in-flight progress bubble, posted at enqueue time (the moment a human
# message triggers a turn) so it appears immediately — before the agent process
# has spawned and imported its stack. kind="progress", so it is reaped when the
# real reply lands and never enters the model transcript.
#
# It carries no text: /chat counts the row's age up inside the bubble ("Worked
# for 21s"), which says everything a "Working on it…" line said and also says
# how long. The bubble is replaced by real status once a run has some — the
# assistant rewrites this row with its step and cost (see _publish_progress).
ASSISTANT_WORKING_NOTICE: str = ""

agent_config: dict[str, AgentConfigEntry] = {
    "chat_structured": {
        "uuid": CHAT_STRUCTURED_UUID,
        "requires_structured_output": True,
        "description": "reads a chatroom's history and decides whether to reply",
    },
    "chat_unstructured": {
        "uuid": CHAT_UNSTRUCTURED_UUID,
        "excludes_structured_output": True,
        "description": "plain-text sibling of chat: replies with one non-structured completion; needs a model group with 'structured output: must not have'",
    },
    "tool_demo": {
        "uuid": TOOL_DEMO_UUID,
        "description": "replies in a chatroom using a FunctionAgent with a multiply tool",
        "requires_function_calling": True,
    },
    "workspace_shell": {
        "uuid": WORKSPACE_SHELL_UUID,
        "description": "runs a chatroom's commands as non-shell argv (no LLM, no bash, workspace-confined)",
        "kanban_authority": "work",
        "kanban_verified": True,
    },
    "kanban_worker": {
        "uuid": KANBAN_WORKER_UUID,
        "requires_structured_output": True,
        "kanban_authority": "work",
        "description": "LLM kanban worker: claims one card, produces a text deliverable into the event trail via one structured call (status done/unclear/failed), completes into Review (unverified)",
    },
    "edit_document_v1": {
        "uuid": EDIT_DOCUMENT_V1_UUID,
        "requires_structured_output": True,
        "description": "given a document and an instruction, returns non-overlapping replace_lines patches in the journal result (does not apply them)",
    },
    "edit_document_v2": {
        "uuid": EDIT_DOCUMENT_V2_UUID,
        "requires_structured_output": True,
        "description": "planner sibling of edit_document that also returns a status (done/partial/unclear) and a required non-empty comment for the orchestrator",
    },
    "edit_document_v3": {
        "uuid": EDIT_DOCUMENT_V3_UUID,
        "requires_structured_output": True,
        "description": "third sibling of edit_document: LLM emits one of four high-level patch ops (replace_lines / insert_before / append_text / append_newline) that normalize internally to the v2 replace_lines form for validation and application",
    },
    "edit_document_v4": {
        "uuid": EDIT_DOCUMENT_V4_UUID,
        "requires_structured_output": True,
        "description": "fourth sibling of edit_document: same four high-level patch ops as v3, but renders the document with a 'logical line' view (trailing newline folded into EOF) and bakes EOF normalization into the returned patches",
    },
    "edit_document_v5": {
        "uuid": EDIT_DOCUMENT_V5_UUID,
        "requires_structured_output": True,
        "description": "fifth sibling of edit_document: duplicate of v4 reserved for further experimentation",
    },
    "edit_document_v6": {
        "uuid": EDIT_DOCUMENT_V6_UUID,
        "requires_structured_output": True,
        "description": "sixth sibling of edit_document: same two-op schema as v5 plus a leading `reasoning` field that asks the model to think out loud (10-20 words) before emitting patches",
    },
    "direct_chat": {
        "uuid": DIRECT_CHAT_UUID,
        "description": "one-to-one operator<->model chat for room_type='direct' rooms: full history as chat messages, one plain-text completion, model + system prompt from the room's own settings (no model group)",
    },
    "mcp": {
        "uuid": MCP_UUID,
        "description": "chat agent that runs a FunctionAgent with tools sourced from MCP servers (configured in mcp.json + the customize.dir overlay)",
        "requires_function_calling": True,
    },
    "assistant": {
        "uuid": ASSISTANT_UUID,
        "requires_structured_output": True,
        "description": "rainbox-owned ReAct loop: decides one bounded action per step via structured output, observes, and repeats until a terminal reply or the step cap. Its models come from the assistant.* slots below, not from a binding of its own (AssistantAgent.uses_model_group is False), so it has no row on /agentmodel.",
    },
    # The assistant's model slots, one per model call it makes. Each resolves
    # through `<slot> -> assistant.default`, so the default alone configures
    # the whole assistant and a single slot moves exactly one call — which is
    # what makes "does this step still work on a non-reasoning model" a
    # question that can be asked one step at a time.
    #
    # All binding-only: they name a model, never a worker. Nothing is ever
    # enqueued to these uuids and no class is registered for them, so the
    # supervisor never spawns them.
    "assistant.default": {
        "uuid": ASSISTANT_DEFAULT_UUID,
        "requires_structured_output": True,
        "description": "binding-only: the model every other assistant.* slot falls back to when it is unbound. With nothing but this bound, one group runs the whole assistant. Never receives journal work.",
    },
    "assistant.decide": {
        "uuid": ASSISTANT_DECIDE_UUID,
        "requires_structured_output": True,
        "description": "binding-only: the model that DECIDES each step of the ReAct loop — the assistant's hot path, called once per step (up to the step cap) where every other slot is called once. The slot to move first when a turn is too slow, and the one whose quality the turn's shape depends on.",
    },
    "assistant.acceptance_criteria": {
        "uuid": ASSISTANT_ACCEPTANCE_CRITERIA_UUID,
        "requires_structured_output": True,
        "description": "binding-only: the model that ESTABLISHES what a good reply must satisfy, before step 0, and revises it when the loop asks. Unbound or failing, the turn runs with no criteria section rather than failing.",
    },
    "assistant.request_summary": {
        "uuid": ASSISTANT_REQUEST_SUMMARY_UUID,
        "requires_structured_output": True,
        "description": "binding-only: the model that DESCRIBES a request too long to ride into the prompts whole. Runs only for an over-long request, and reads far more of it than any other call.",
    },
    "assistant.memory_filter": {
        "uuid": ASSISTANT_MEMORY_FILTER_UUID,
        "requires_structured_output": True,
        "description": "binding-only: the model that SCORES what memory_query recalled (Likert relevance filter), one call per memory_query. Narrow, repetitive work over candidate text.",
    },
    "assistant.second_opinion": {
        "uuid": ASSISTANT_SECOND_OPINION_UUID,
        "requires_structured_output": True,
        "description": "binding-only: the model that REVIEWS a gated assistant action (currently python_run) before it executes — checks the stated reason, the model's reasoning, and the program against the request and operator profile. Fails open: unbound or failing, the action runs.",
    },
    "assistant.reply_audit": {
        "uuid": ASSISTANT_REPLY_AUDIT_UUID,
        "requires_structured_output": True,
        "description": "binding-only: the model that AUDITS a finished reply message before it is sent — checks it against the request (every sub-question answered), the turn's constraints, the operator settings and the turn's observations, and returns it with problems when it is not sound. Fails open: unbound or failing, the message is sent.",
    },
    "assistant.response_language_classifier": {
        "uuid": ASSISTANT_RESPONSE_LANGUAGE_CLASSIFIER_UUID,
        "requires_structured_output": True,
        "description": "binding-only: narrow scorer that predicts which language(s) the assistant's next reply should use before step 0; records reason and per-language Likert confidence, then supplies later assistant calls with score-free ranked Markdown. Unbound, the run records a skipped step.",
    },
    "assistant.run_summarizer": {
        "uuid": ASSISTANT_RUN_SUMMARIZER_UUID,
        "requires_structured_output": True,
        "description": "summarizes a completed assistant run (trigger + obstacles + outcome) via one structured call; enqueued by the assistant at every terminal state. The one assistant.* slot that is also a real agent — it runs off the critical path, so a slow model here costs the operator nothing.",
    },
}


def role_name(agent_uuid: UUID) -> str | None:
    """The `agent_config` key for `agent_uuid`, or None for a uuid this table
    does not know. The key is the one name a call is known by — its /agentmodel
    row, its /activity caller tag, its label in the assistant's trace — so a
    caller that has the uuid never has to carry the name alongside it."""
    for name, entry in agent_config.items():
        if entry["uuid"] == agent_uuid:
            return name
    return None
