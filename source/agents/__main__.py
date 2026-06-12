"""Agent child-process entrypoint: spawned by the supervisor as
`python -m agents --socket-fd N`.

KNOWN ISSUES (point 1):
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
    from agents.chat_structured import StructuredChatAgent
    from agents.chat_unstructured import UnstructuredChatAgent
    from agents.edit_document_v1 import EditDocumentAgentV1
    from agents.edit_document_v2 import EditDocumentAgentV2
    from agents.edit_document_v3 import EditDocumentAgentV3
    from agents.edit_document_v4 import EditDocumentAgentV4
    from agents.edit_document_v5 import EditDocumentAgentV5
    from agents.edit_document_v6 import EditDocumentAgentV6
    from agents.conversation import ConversationManagerAgent
    from agents.followup import FollowUpClassifierAgent
    from agents.kanban_worker import KanbanWorkerAgent
    from agents.mcp import MCPAgent
    from agents.tool_demo import ToolDemoAgent
    from agents.query import QueryAgent
    from agents.query_filter_router import QueryFilterRouterAgent
    from agents.query_router import QueryRouterAgent
    from agents.router import RouterAgent
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
