import argparse
import json
import logging
import os
import selectors
import signal
import socket
import sys
import threading
import time
import uuid
from typing import TypedDict
from uuid import UUID

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

from werkzeug.serving import make_server  # noqa: E402

import db  # noqa: E402
from agents.config import AgentConfigEntry, agent_config  # noqa: E402
from webapp import app  # noqa: E402
from webapp.core import sync_models_from_providers  # noqa: E402

# Max time an agent may go without sending a status message before the
# supervisor considers it hung and kills it. An agent only heartbeats between
# inbox items, not during one — so this must exceed the slowest single unit of
# work (an LLM call), which easily takes more than a few seconds.
HEARTBEAT_TIMEOUT: float = 60.0
TICK_TIMEOUT: float = 1.0
CRON_TICK_INTERVAL: float = 5.0  # how often to check for due cron jobs (cron granularity is 1 min)
ROOT_DIR: str = os.path.dirname(os.path.abspath(__file__))


class Agent(TypedDict):
    name: str
    params: AgentConfigEntry
    pid: int
    uuid: UUID
    sock: socket.socket
    buffer: bytes
    last_heartbeat: float
    alive: bool


def spawn(name: str, params: AgentConfigEntry) -> Agent:
    agent_uuid = params["uuid"]
    parent_sock, agent_sock = socket.socketpair()
    os.set_inheritable(agent_sock.fileno(), True)
    argv = [
        sys.executable, "-m", "agents",
        "--socket-fd", str(agent_sock.fileno()),
    ]
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT_DIR + (
        os.pathsep + env["PYTHONPATH"] if "PYTHONPATH" in env else ""
    )
    pid = os.posix_spawn(sys.executable, argv, env)
    agent_sock.close()
    config_msg = {"name": name, **params}
    parent_sock.sendall((json.dumps(config_msg, default=str) + "\n").encode())
    logger.info("spawned pid=%d name=%s uuid=%s", pid, name, agent_uuid)
    return {
        "name": name,
        "params": params,
        "pid": pid,
        "uuid": agent_uuid,
        "sock": parent_sock,
        "buffer": b"",
        "last_heartbeat": time.monotonic(),
        "alive": True,
    }


def supervisor_loop(stop_event: threading.Event) -> None:
    uuid_to_role: dict[UUID, str] = {p["uuid"]: n for n, p in agent_config.items()}
    sel = selectors.DefaultSelector()
    agents: dict[str, Agent] = {}

    last_cron_tick = 0.0

    with app.app_context():
        while not stop_event.is_set():
            # Cron scheduler pass (throttled). Self-guarded: a cron bug must not
            # take down the supervisor thread.
            if time.monotonic() - last_cron_tick >= CRON_TICK_INTERVAL:
                last_cron_tick = time.monotonic()
                try:
                    n = db.cron_tick()
                    if n:
                        logger.info("cron: fired %d due job(s)", n)
                except Exception:
                    logger.exception("cron tick failed")
                    db.db.session.rollback()

            for journal_row in db.fetch_unrouted_terminal():
                src_role = uuid_to_role.get(journal_row["agent_uuid"])
                # Dynamic return address (set by the conversation manager in the
                # turn payload, copied to result["_routing"] by Agent.run) takes
                # precedence over static `next`. Static `next` is success-only;
                # a dynamic address also routes failed turns so a manager can
                # recover. Model output never chooses a routing target.
                result = journal_row["result"] or {}
                dynamic_next = (result.get("_routing") or {}).get("return_to_agent_uuid")
                static_next = agent_config[src_role]["next"] if src_role else None
                if dynamic_next:
                    next_uuid = UUID(dynamic_next)
                elif journal_row["state"] == "completed":
                    next_uuid = static_next
                else:
                    next_uuid = None
                if next_uuid is not None:
                    next_role = uuid_to_role.get(next_uuid, "?")
                    payload = {
                        "from": src_role,
                        "from_journal_id": journal_row["id"],
                        "state": journal_row["state"],
                        "input": journal_row["payload"],
                        "result": journal_row["result"],
                    }
                    db.enqueue(next_uuid, payload)
                    logger.info("routed journal_id=%d %s -> %s", journal_row["id"], src_role, next_role)
                db.mark_routed(journal_row["id"])

            uuids_with_work = db.agent_uuids_with_work()
            for name, params in agent_config.items():
                if params["uuid"] in uuids_with_work and name not in agents:
                    ag = spawn(name, params)
                    agents[name] = ag
                    sel.register(ag["sock"], selectors.EVENT_READ, name)

            for key, _ in sel.select(timeout=TICK_TIMEOUT):
                name = key.data
                ag = agents[name]
                chunk = ag["sock"].recv(4096)
                if not chunk:
                    ag["alive"] = False
                    continue
                ag["buffer"] += chunk
                while b"\n" in ag["buffer"]:
                    line, ag["buffer"] = ag["buffer"].split(b"\n", 1)
                    if not line:
                        continue
                    msg = json.loads(line.decode())
                    # Any message resets the silence-watchdog timer. Heartbeats
                    # exist only to do that during a long handle(); don't log them.
                    ag["last_heartbeat"] = time.monotonic()
                    if msg.get("status") != "heartbeat":
                        logger.info("agent %s -> %s", name, msg)

            now = time.monotonic()
            for name in list(agents):
                ag = agents[name]
                if ag["alive"] and now - ag["last_heartbeat"] > HEARTBEAT_TIMEOUT:
                    logger.warning("agent %s hung (no message in %.1fs); killing", name, HEARTBEAT_TIMEOUT)
                    try:
                        os.kill(ag["pid"], signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    ag["alive"] = False

            for name in list(agents):
                ag = agents[name]
                if not ag["alive"]:
                    try:
                        os.waitpid(ag["pid"], 0)
                    except ChildProcessError:
                        pass
                    try:
                        sel.unregister(ag["sock"])
                    except (KeyError, ValueError):
                        pass
                    ag["sock"].close()
                    del agents[name]

        logger.info("supervisor shutting down; killing %d remaining agent(s)", len(agents))
        for ag in agents.values():
            try:
                os.kill(ag["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass
        for name in list(agents):
            ag = agents[name]
            try:
                os.waitpid(ag["pid"], 0)
            except ChildProcessError:
                pass
            try:
                sel.unregister(ag["sock"])
            except (KeyError, ValueError):
                pass
            ag["sock"].close()


def main() -> None:
    # Record the supervisor's start time in an env var so child agent processes
    # (spawned via os.posix_spawn(..., os.environ)) inherit it and can answer
    # "how long have you been running?" without relying on PPID ps tricks.
    os.environ["PP3_SUPERVISOR_STARTED"] = str(time.time())

    parser = argparse.ArgumentParser(description="rainbox supervisor + webserver")
    parser.add_argument(
        "--force-model-sync",
        action="store_true",
        help="Reconcile model_config rows with every registered provider "
        "(availability, sizes, and the is_function_calling_model capability "
        "flag), updating existing rows' arguments too, then exit without "
        "starting the server.",
    )
    args = parser.parse_args()

    if args.force_model_sync:
        with app.app_context():
            results = sync_models_from_providers(force_update_arguments=True)
        unreachable = [pid for pid, s in results.items() if s is None]
        if unreachable:
            logger.warning("force sync: providers unreachable: %s", unreachable)
        logger.info("force sync complete: %s", results)
        return

    root_uuid: UUID = uuid.uuid4()
    logger.info("uuid: %s", root_uuid)
    logger.info("name: root")

    stop_event = threading.Event()
    thread = threading.Thread(
        target=supervisor_loop, args=(stop_event,), name="supervisor", daemon=False
    )
    thread.start()

    server = make_server("127.0.0.1", 5000, app, threaded=True)
    logger.info("supervisor thread started; webserver on http://127.0.0.1:5000 (Ctrl-C to quit)")

    def shutdown_handler(signum: int, _frame: object) -> None:
        logger.info("received signal %d; shutting down", signum)
        stop_event.set()
        threading.Thread(target=server.shutdown, name="shutdown", daemon=True).start()

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    try:
        server.serve_forever()
    finally:
        stop_event.set()
        thread.join(timeout=HEARTBEAT_TIMEOUT + 2.0)
        if thread.is_alive():
            logger.warning("supervisor did not stop within timeout")
        logger.info("bye")


if __name__ == "__main__":
    main()
