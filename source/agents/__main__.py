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
import signal
import socket
import sys
from types import FrameType
from typing import Any
from uuid import UUID

import db
from agents.config import resolve_agent_class

logger = logging.getLogger(__name__)


class SupervisorTerminate(SystemExit):
    """Raised in the worker when the supervisor asks it to stop."""


def _on_sigterm(_signum: int, _frame: FrameType | None) -> None:
    """Turn the supervisor's SIGTERM into an exception in the main thread.

    Default SIGTERM ends the process where it stands, running no `finally`
    blocks — which leaves the HTTP connection to the inference server open.
    The server never sees a disconnect, so it keeps generating and holding the
    GPU long after the run has been marked failed. Raising instead unwinds the
    stack: the LLM stream generator is closed, its connection torn down, and
    the backend stops.

    Signal handlers run in the main thread, so this fires wherever the worker
    is — including inside a blocking socket read, because PEP 475 propagates a
    handler's exception rather than retrying the syscall. The supervisor still
    SIGKILLs after TERM_GRACE if the unwind does not finish.
    """
    logger.warning("agent: SIGTERM from supervisor; unwinding")
    raise SupervisorTerminate("terminated by supervisor")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, _on_sigterm)

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

    # Record every LLM call this worker makes to `llm_call` (the /activity
    # page). Installed after the app context exists, because the recorder's
    # sink writes through db.session.
    from llm.activity import install_activity_recorder

    install_activity_recorder()

    def send(msg: dict[str, Any]) -> None:
        sock.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    # The role name IS the implementation key. resolve_agent_class imports
    # ONLY the selected agent, so this spawned process doesn't pay every
    # agent's import cost (llama_index etc.).
    agent_cls = resolve_agent_class(config["name"])
    agent = agent_cls(agent_uuid=agent_uuid, name=config["name"], send=send)
    agent.run()

    sock.close()


if __name__ == "__main__":
    main()
