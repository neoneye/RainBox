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
from agents.config import resolve_agent_class

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

    # Dispatch on agent_kind when present, else the role name. This lets many
    # roles (e.g. persona_egon / persona_benny) share one implementation class
    # while existing roles (whose name == implementation key) are unaffected.
    # _resolve_agent_class imports ONLY the selected agent, so this spawned
    # process doesn't pay every agent's import cost (llama_index etc.).
    kind = config.get("agent_kind", config["name"])
    agent_cls = resolve_agent_class(kind)
    agent = agent_cls(agent_uuid=agent_uuid, name=config["name"], send=send)
    agent.run()

    sock.close()


if __name__ == "__main__":
    main()
