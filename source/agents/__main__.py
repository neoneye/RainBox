"""Agent child-process entrypoint: spawned by the supervisor as
`python -m agents --socket-fd N`.

KNOWN ISSUE (verified, deferred):
Config-read discards the socket remainder. main() reads the config line
with `config_line, _ = buf.split("\n", 1)` and drops `_`. This is safe
*today* only because (a) the supervisor sends exactly one newline-
terminated config message and never writes to the agent again
(the config sendall in main.py spawn()), and (b) the agent never reads
`sock` again — run() pulls work from Postgres (db.take_item), and the
socket is used only for outbound status (sock.sendall). If either ever
changes (supervisor sends follow-up commands, or the agent starts reading
the socket), bytes that arrived in the same recv() as the config line
would be lost; keep the remainder then.

(Further known issues about the streaming deadline live atop agents/base.py.)
"""
import argparse
import json
import logging
import socket
import sys
from typing import Any
from uuid import UUID

import db
from agents.base import Agent, ModelGroupAgent

logger = logging.getLogger(__name__)


# Role/kind → "module:ClassName". Values are strings so this table imports
# nothing at module load; _resolve_agent_class imports only the one it needs.
_AGENT_CLASS_PATHS: dict[str, str] = {
    "assistant": "agents.assistant:AssistantAgent",
    "assistant_run_summarizer": "agents.assistant_run_summarizer:AssistantRunSummarizerAgent",
    "chat_structured": "agents.chat_structured:StructuredChatAgent",
    "chat_unstructured": "agents.chat_unstructured:UnstructuredChatAgent",
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


def _resolve_agent_class(kind: str) -> type[Agent]:
    """Import and return the agent class for `kind` (a plain ModelGroupAgent as
    the default). Imports ONLY the selected module, so a spawned agent process
    loads its own dependencies (llama_index etc.) — not all 20 agents'."""
    import importlib

    path = _AGENT_CLASS_PATHS.get(kind)
    if path is None:
        return ModelGroupAgent
    module_name, class_name = path.split(":")
    return getattr(importlib.import_module(module_name), class_name)


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

    # Dispatch on agent_kind when present, else the role name. This lets many
    # roles (e.g. persona_egon / persona_benny) share one implementation class
    # while existing roles (whose name == implementation key) are unaffected.
    # _resolve_agent_class imports ONLY the selected agent, so this spawned
    # process doesn't pay every agent's import cost (llama_index etc.).
    kind = config.get("agent_kind", config["name"])
    agent_cls = _resolve_agent_class(kind)
    agent = agent_cls(agent_uuid=agent_uuid, name=config["name"], send=send)
    agent.run()

    sock.close()


if __name__ == "__main__":
    main()
