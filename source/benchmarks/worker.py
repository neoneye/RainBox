"""Child process that runs the `/benchmark_basic` suite for ONE target.

Spawned per row by BenchmarkRunner via benchmarks.subproc.stream_target_
subprocess so the row can be SIGKILLed mid-trial (a runaway model otherwise
pegs CPU/GPU until the provider times out).

Protocol — read one JSON request on stdin:
    {"target_uuid": str, "skip_warmup": bool}
and write NDJSON progress events to stdout, one per line:
    {"t": "target_status", "status": "warming_up" | "running" | "done"}
    {"t": "warmup_elapsed", "elapsed": float}
    {"t": "warmup_failed", "error": str}
    {"t": "bench_status", "bi": int, "status": "running" | "done" | "error", "error"?: str}
    {"t": "trial", "bi": int, "correct": bool, "had_error": bool, "elapsed": float}

stdout carries only those events: the test's own output and library chatter is
redirected to stderr (discarded by the parent) so it can't corrupt the stream.
"""

import json
import os
import sys
import time


def main() -> None:
    req = json.load(sys.stdin)

    # Reserve fd 1 for events, then point stdout at stderr so nothing else can
    # land on the channel the parent parses.
    event_fd = os.dup(1)
    os.dup2(2, 1)

    def emit(ev: dict) -> None:
        os.write(event_fd, (json.dumps(ev) + "\n").encode())

    from uuid import UUID

    import benchmarks.basic as B
    import llm
    from benchmarks.runner import SPEC_SETS
    from db import make_app

    target_uuid = UUID(req["target_uuid"])
    skip_warmup = bool(req.get("skip_warmup", False))
    specs = SPEC_SETS[req.get("spec_set", "general")]

    app = make_app()
    with app.app_context():
        if not skip_warmup:
            emit({"t": "target_status", "status": "warming_up"})
            t0 = time.monotonic()
            try:
                elapsed = B.warmup(target_uuid)
                emit({"t": "warmup_elapsed", "elapsed": elapsed})
            except Exception as e:
                emit({"t": "warmup_elapsed", "elapsed": time.monotonic() - t0})
                emit({"t": "warmup_failed", "error": f"{type(e).__name__}: {e}"})

        emit({"t": "target_status", "status": "running"})
        for bi, (_name, cls, kwargs) in enumerate(specs):
            emit({"t": "bench_status", "bi": bi, "status": "running"})
            status = "done"
            err: str | None = None
            # Tally reasoning vs content chars across this benchmark's trials so
            # the UI can show what a slow benchmark spent its time generating.
            with llm.capture_reasoning() as native:
                try:
                    bench = cls(target_uuid, **kwargs)

                    def on_trial(trial, _bi=bi) -> None:
                        emit({
                            "t": "trial",
                            "bi": _bi,
                            "correct": bool(trial.correct),
                            "had_error": trial.error is not None,
                            "elapsed": trial.elapsed,
                        })

                    result = bench.run(on_trial=on_trial)
                    if result.aborted:
                        status, err = "error", (result.abort_reason or "aborted")
                except Exception as e:
                    status, err = "error", f"{type(e).__name__}: {e}"
            event = {
                "t": "bench_status",
                "bi": bi,
                "status": status,
                "reasoning_chars": native.reasoning_chars,
                "content_chars": native.content_chars,
            }
            if err is not None:
                event["error"] = err
            emit(event)

        emit({"t": "target_status", "status": "done"})


if __name__ == "__main__":
    main()
