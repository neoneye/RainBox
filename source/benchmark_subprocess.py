"""Run one benchmark row in a killable child process.

Benchmark trials are blocking LLM calls; in-process there's no way to abort a
runaway model — it pegs CPU/GPU until the provider times out. So each row runs
in a subprocess the runner can SIGKILL: killing the process closes its HTTP
socket to the provider, which makes the provider (e.g. Ollama) stop generating.

`stream_target_subprocess` feeds the worker a JSON request line on stdin and
relays the NDJSON progress events it writes to stdout, calling `on_event` for
each. It watches `stop_event` via a `selectors` timeout (rather than a blocking
read) so a Stop is observed within `poll_interval` even when the model has gone
silent — at which point the child is SIGKILLed.
"""

import json
import os
import selectors
import subprocess
import sys
import threading
from typing import Any, Callable


def stream_target_subprocess(
    worker_script: str,
    request: dict[str, Any],
    on_event: Callable[[dict[str, Any]], None],
    stop_event: threading.Event,
    poll_interval: float = 0.25,
) -> bool:
    """Spawn `worker_script`, send `request` as one JSON line on stdin, and call
    `on_event(event)` for each NDJSON object the worker writes to stdout.

    Returns True if the child was killed because `stop_event` was set, False if
    it ran to completion. The child is always reaped. stderr is discarded so
    library chatter can't deadlock on a full pipe."""
    proc = subprocess.Popen(
        [sys.executable, worker_script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    killed = False
    sel = selectors.DefaultSelector()
    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write((json.dumps(request) + "\n").encode())
        proc.stdin.close()
        sel.register(proc.stdout, selectors.EVENT_READ)
        fd = proc.stdout.fileno()
        buf = b""
        while True:
            if stop_event.is_set():
                killed = True
                break
            if not sel.select(timeout=poll_interval):
                continue  # timed out with no data — loop to re-check stop_event
            chunk = os.read(fd, 65536)
            if not chunk:
                break  # EOF — the worker finished
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    on_event(json.loads(line.decode()))
                except (ValueError, UnicodeDecodeError):
                    pass  # ignore a malformed line rather than abort the row
    finally:
        sel.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    return killed
