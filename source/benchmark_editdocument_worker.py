"""Child process that runs the `/benchmark_editdocument` suite for ONE target.

Spawned per row by BenchmarkEditDocumentRunner via benchmark_subprocess.
stream_target_subprocess so the row can be SIGKILLed mid-trial (a runaway
model otherwise pegs CPU/GPU until the provider times out).

Protocol — read one JSON request on stdin:
    {"target_uuid": str, "agent_choice": str}
and write NDJSON progress events to stdout, one per line:
    {"t": "target_status", "status": "running" | "done" | "error"}
    {"t": "trial", "test_name": str, "correct": bool, "elapsed": float,
     "error": str|null, "applied": str|null, "patches": list|null,
     "agent_status": str|null, "agent_comment": str|null}

stdout carries only those events: the agent's own output and library chatter is
redirected to stderr (discarded by the parent) so it can't corrupt the stream.
"""

import json
import os
import sys


def main() -> None:
    req = json.load(sys.stdin)

    event_fd = os.dup(1)
    os.dup2(2, 1)

    def emit(ev: dict) -> None:
        os.write(event_fd, (json.dumps(ev) + "\n").encode())

    from uuid import UUID

    from benchmark_editdocument import BenchmarkEditDocument
    from benchmark_editdocument_runner import AGENT_REGISTRY
    from db import make_app

    target_uuid = UUID(req["target_uuid"])
    agent_class, agent_uuid, agent_name = AGENT_REGISTRY[req["agent_choice"]]

    app = make_app()
    with app.app_context():
        emit({"t": "target_status", "status": "running"})

        def on_trial_start(test_name: str) -> None:
            emit({"t": "trial_start", "test_name": test_name})

        def on_trial(trial) -> None:
            emit({
                "t": "trial",
                "test_name": trial.test_name,
                "correct": trial.correct,
                "elapsed": trial.elapsed,
                "error": trial.error,
                "applied": trial.applied,
                "patches": trial.patches,
                "agent_status": trial.agent_status,
                "agent_comment": trial.agent_comment,
                "thinking_chars": trial.thinking_chars,
                "content_chars": trial.content_chars,
            })

        try:
            bench = BenchmarkEditDocument(
                target_uuid=target_uuid,
                agent_class=agent_class,
                agent_uuid=agent_uuid,
                agent_name=agent_name,
            )
            bench.run(on_trial=on_trial, on_trial_start=on_trial_start)
            emit({"t": "target_status", "status": "done"})
        except Exception as e:
            emit({"t": "target_status", "status": "error",
                  "error": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    main()
