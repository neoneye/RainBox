"""Child process that runs the /benchmark_vision cases for ONE target.

Spawned by VisionBenchmarkRunner through benchmarks.subproc so a Stop can
SIGKILL it mid-request (closing the socket makes Ollama stop generating).

It needs no database: the parent resolves the target and sends it whole.

Protocol — one JSON request on stdin:
    {"target": Target.to_json(), "cases": [case name, …], "trials": int,
     "warmup": bool}
NDJSON events on stdout, one per line:
    {"t": "warming_up"}
    {"t": "warmup", "elapsed": float}  |  {"t": "warmup_failed", "error": str}
    {"t": "trial_start", "case": str, "trial": int}
    {"t": "trial", "trial": Trial.to_json()}
    {"t": "done"}
"""

import json
import os
import sys


def main() -> None:
    req = json.load(sys.stdin)

    # Reserve fd 1 for events, then point stdout at stderr so library chatter
    # cannot land on the channel the parent parses.
    event_fd = os.dup(1)
    os.dup2(2, 1)

    def emit(ev: dict) -> None:
        os.write(event_fd, (json.dumps(ev) + "\n").encode())

    from benchmarks.vision_ocr import CASES_BY_NAME, IMAGE_PATH, Target, run_trial, warm_up

    target = Target.from_json(req["target"])
    image_png = IMAGE_PATH.read_bytes()
    if req.get("warmup", True):
        emit({"t": "warming_up"})
        try:
            emit({"t": "warmup", "elapsed": warm_up(target)})
        except Exception as e:                            # noqa: BLE001
            emit({"t": "warmup_failed", "error": f"{type(e).__name__}: {e}"})
    for name in req["cases"]:
        case = CASES_BY_NAME[name]
        for i in range(int(req["trials"])):
            emit({"t": "trial_start", "case": name, "trial": i})
            emit({"t": "trial", "trial": run_trial(target, case, i, image_png).to_json()})
    emit({"t": "done"})


if __name__ == "__main__":
    main()
