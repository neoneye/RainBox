"""Background orchestration for /benchmark_basic: iterates targets in /models tree
order, runs every benchmark per target, and maintains a state dict the
webapp polls."""

import logging
import os
import threading
import time
from typing import Any

from flask import Flask

import db
from benchmark import (
    BenchmarkBase64Decode,
    BenchmarkBase64Encode,
    BenchmarkReverseList,
    BenchmarkReverseString,
    BenchmarkToolOrder,
    BenchmarkToolRoute,
)
from benchmark_kanban import BenchmarkKanbanOpStructured, BenchmarkKanbanOpTools
from benchmark_subprocess import stream_target_subprocess

logger = logging.getLogger(__name__)

# Each target runs in this child process so a stuck model can be SIGKILLed.
_BENCHMARK_WORKER = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "benchmark_worker.py"
)

BENCHMARK_SPECS: list[tuple[str, type, dict[str, Any]]] = [
    ("base64_decode", BenchmarkBase64Decode, {"num_trials": 5, "string_length": 6}),
    ("base64_encode", BenchmarkBase64Encode, {"num_trials": 5, "string_length": 6}),
    ("reverse_string", BenchmarkReverseString, {"num_trials": 5, "string_length": 6}),
    ("reverse_list", BenchmarkReverseList, {"num_trials": 5, "num_items": 5, "item_length": 4}),
    ("tool_order", BenchmarkToolOrder, {"num_trials": 5}),
    ("tool_route", BenchmarkToolRoute, {"num_trials": 5}),
]

# The kanban "first slice" (docs/kanban-design.md roadmap item 1): the 2×2
# decision matrix — board context format × invocation mechanism — whose
# results pick the defaults for the first LLM kanban worker. Its own page
# (/benchmark_kanban) so the general suite stays fast and the matrix reads
# as one comparison.
KANBAN_BENCHMARK_SPECS: list[tuple[str, type, dict[str, Any]]] = [
    ("kanban_md_struct", BenchmarkKanbanOpStructured,
     {"num_trials": 5, "context_format": "markdown"}),
    ("kanban_json_struct", BenchmarkKanbanOpStructured,
     {"num_trials": 5, "context_format": "json"}),
    ("kanban_md_tools", BenchmarkKanbanOpTools,
     {"num_trials": 5, "context_format": "markdown"}),
    ("kanban_json_tools", BenchmarkKanbanOpTools,
     {"num_trials": 5, "context_format": "json"}),
]

# Spec sets by name: each BenchmarkRunner instance (and its worker child)
# runs exactly one set; the name travels in the worker request JSON.
SPEC_SETS: dict[str, list[tuple[str, type, dict[str, Any]]]] = {
    "general": BENCHMARK_SPECS,
    "kanban": KANBAN_BENCHMARK_SPECS,
}


def _empty_benchmark_entry(name: str, total: int) -> dict[str, Any]:
    return {
        "name": name,
        "status": "pending",  # pending | running | done | error
        "trials_done": 0,
        "trials_total": total,
        "correct": 0,
        "mistakes": 0,
        "failures": 0,
        "total_elapsed": 0.0,
        "error": None,
        "reasoning_chars": None,
        "content_chars": None,
    }


class BenchmarkRunner:
    """Single-instance orchestrator. Owns the worker thread + state dict.

    The webapp instantiates one of these at import time and routes wire
    /benchmark_basic/start, /benchmark_basic/stop, /benchmark_basic/state to it."""

    def __init__(self, spec_set: str = "general") -> None:
        self.spec_set = spec_set
        self.specs = SPEC_SETS[spec_set]
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = {
            "running": False,
            "started_at": None,
            "ended_at": None,
            "aborted": False,
            "current_target_index": -1,
            "total_targets": 0,
            "targets": [],
        }

    def get_state(self) -> dict[str, Any]:
        with self._lock:
            # Shallow copy is enough — callers will JSON-serialize and not
            # mutate. The targets list/dicts are not externally edited.
            return dict(self._state)

    def ensure_targets_populated(self) -> None:
        """Refresh `targets` from the /models tree if no run is in progress.

        Lets the page render all available targets (with per-row Start
        buttons) before the first benchmark click. Existing per-target
        state (results from prior runs) is preserved across the refresh;
        newly-added targets show up as `pending`. While a run is in
        progress this is a no-op so we don't mutate mid-run state."""
        with self._lock:
            if self._state["running"]:
                return
            all_targets = self._collect_targets()
            existing = {t["uuid"]: t for t in self._state.get("targets", [])}
            refreshed: list[dict[str, Any]] = []
            for i, t in enumerate(all_targets):
                cached = existing.get(t["uuid"])
                if cached is None:
                    refreshed.append(
                        {
                            "index": i,
                            "kind": t["kind"],
                            "uuid": t["uuid"],
                            "provider": t["provider"],
                            "model_name": t["model_name"],
                            "model_display_name": t["model_display_name"],
                            "display_name": t["display_name"],
                            "status": "pending",
                            "warmup_elapsed": None,
                            "warmup_started_at": None,
                            "benchmarks": [
                                _empty_benchmark_entry(name, kwargs.get("num_trials", 5))
                                for name, _cls, kwargs in self.specs
                            ],
                        }
                    )
                else:
                    cached = dict(cached)
                    cached["index"] = i
                    refreshed.append(cached)
            self._state["targets"] = refreshed

    def start(self, app: Flask, target_uuids: list[str] | None = None) -> bool:
        """Kick off a run in the background.

        target_uuids=None means run every target in the /models tree.
        If a list is given, only those targets are run; other targets in the
        state keep their previous values (so the page shows accumulated
        results across multiple per-target Start clicks).

        Returns False if a run is already in progress."""
        with self._lock:
            if self._state["running"]:
                return False
            all_targets = self._collect_targets()
            run_set: set[str] | None = (
                set(target_uuids) if target_uuids is not None else None
            )
            existing = {t["uuid"]: t for t in self._state.get("targets", [])}

            def _fresh_entry(i: int, t: dict[str, Any]) -> dict[str, Any]:
                return {
                    "index": i,
                    "kind": t["kind"],
                    "uuid": t["uuid"],
                    "provider": t["provider"],
                    "model_name": t["model_name"],
                    "model_display_name": t["model_display_name"],
                    "display_name": t["display_name"],
                    "status": "pending",
                    "warmup_elapsed": None,
                    "warmup_started_at": None,
                    "benchmarks": [
                        _empty_benchmark_entry(name, kwargs.get("num_trials", 5))
                        for name, _cls, kwargs in self.specs
                    ],
                }

            new_targets_state: list[dict[str, Any]] = []
            run_targets: list[dict[str, Any]] = []
            for i, t in enumerate(all_targets):
                uuid_str = t["uuid"]
                should_run = run_set is None or uuid_str in run_set
                if should_run:
                    new_targets_state.append(_fresh_entry(i, t))
                    run_targets.append({**t, "state_index": i})
                else:
                    cached = existing.get(uuid_str)
                    if cached is None:
                        new_targets_state.append(_fresh_entry(i, t))
                    else:
                        cached = dict(cached)
                        cached["index"] = i
                        new_targets_state.append(cached)

            self._state = {
                "running": True,
                "started_at": time.time(),
                "ended_at": None,
                "aborted": False,
                "current_target_index": -1,
                "total_targets": len(run_targets),
                "targets": new_targets_state,
            }
            self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, args=(app, run_targets), name="benchmark-runner", daemon=True
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop_event.set()

    def _collect_targets(self) -> list[dict[str, Any]]:
        """Walk the /models tree (available-first, alphabetical) and return
        a flat list of ModelConfigOverride rows that (a) belong to an
        available ModelConfig and (b) resolve to a capability the spec set
        exercises: the general suite wants should_use_structured_outputs=True;
        the kanban 2×2 matrix compares structured output AGAINST function
        calling, so it accepts either capability — filtering kanban targets
        to structured-capable overrides would bias the tools columns toward
        the structured-capable set. A target lacking the capability a given
        cell needs fails that cell explicitly per trial (the error is
        recorded), it is not silently skipped.

        Base ModelConfig rows are deliberately skipped — they're
        unconfigured (no tuned temperature / reasoning / tool flags); the
        operator only wants benchmarks on configurations they've actually
        dialed in. Overrides with neither relevant capability are skipped
        for the same reason."""
        tree = db.list_model_configs_with_overrides()
        targets: list[dict[str, Any]] = []
        for cfg, overrides in tree:
            if not cfg.available:
                continue
            for ov in overrides:
                eligible = db.member_uses_structured_output(ov.uuid)
                if not eligible and self.spec_set == "kanban":
                    eligible = db.member_is_function_calling(ov.uuid)
                if not eligible:
                    continue
                targets.append(
                    {
                        "kind": "override",
                        "uuid": str(ov.uuid),
                        "provider": cfg.provider,
                        "model_name": cfg.model_name,
                        "model_display_name": cfg.effective_display_name,
                        # effective_display_name: user-set display_name if any,
                        # else the synthesized "t0.5 c32k struct" summary so
                        # unnamed overrides still convey what differs.
                        "display_name": ov.effective_display_name or "(no name)",
                        "uuid_obj": ov.uuid,
                    }
                )
        return targets

    def _set_target_status(self, target_index: int, status: str) -> None:
        with self._lock:
            self._state["targets"][target_index]["status"] = status
            if status in ("warming_up", "running"):
                self._state["current_target_index"] = target_index
            if status == "warming_up":
                # Stamp the start so the UI can show a live, ticking elapsed
                # counter while warmup blocks (an embedding model can hang on
                # this for the full provider timeout).
                self._state["targets"][target_index]["warmup_started_at"] = time.time()

    def _set_warmup_elapsed(self, target_index: int, elapsed: float) -> None:
        with self._lock:
            self._state["targets"][target_index]["warmup_elapsed"] = elapsed

    def _set_benchmark_status(
        self,
        target_index: int,
        bench_index: int,
        status: str,
        error: str | None = None,
        reasoning_chars: int | None = None,
        content_chars: int | None = None,
    ) -> None:
        with self._lock:
            entry = self._state["targets"][target_index]["benchmarks"][bench_index]
            entry["status"] = status
            if error is not None:
                entry["error"] = error
            if reasoning_chars is not None:
                entry["reasoning_chars"] = reasoning_chars
            if content_chars is not None:
                entry["content_chars"] = content_chars

    def _record_trial(
        self,
        target_index: int,
        bench_index: int,
        correct: bool,
        had_error: bool,
        elapsed: float,
    ) -> None:
        with self._lock:
            entry = self._state["targets"][target_index]["benchmarks"][bench_index]
            entry["trials_done"] += 1
            entry["total_elapsed"] += elapsed
            if had_error:
                entry["failures"] += 1
            elif correct:
                entry["correct"] += 1
            else:
                entry["mistakes"] += 1

    def _finish(self, aborted: bool) -> None:
        with self._lock:
            self._state["running"] = False
            self._state["ended_at"] = time.time()
            self._state["aborted"] = aborted
            if aborted:
                # A target SIGKILLed mid-warmup/mid-trial never emits its
                # terminal status event, so its status would stay stuck at
                # "warming_up"/"running" forever — a yellow row with "warming
                # up…" and a frozen progress bar that polling won't clear
                # (polling stops once running flips false). Reset any
                # in-progress target/benchmark back to pending so the row
                # clears and its Start button works again.
                for t in self._state["targets"]:
                    if t["status"] in ("warming_up", "running"):
                        t["status"] = "pending"
                    for b in t["benchmarks"]:
                        if b["status"] == "running":
                            b["status"] = "pending"

    def _apply_event(self, ti: int, ev: dict[str, Any]) -> None:
        """Map one NDJSON progress event from the per-target child process onto
        the state setters the polling UI reads."""
        kind = ev.get("t")
        if kind == "target_status":
            self._set_target_status(ti, ev["status"])
        elif kind == "warmup_elapsed":
            self._set_warmup_elapsed(ti, ev["elapsed"])
        elif kind == "warmup_failed":
            logger.warning("benchmark: warmup failed on target %d: %s", ti, ev.get("error"))
        elif kind == "bench_status":
            self._set_benchmark_status(
                ti, ev["bi"], ev["status"], ev.get("error"),
                reasoning_chars=ev.get("reasoning_chars"),
                content_chars=ev.get("content_chars"),
            )
        elif kind == "trial":
            self._record_trial(
                ti, ev["bi"], ev["correct"], ev["had_error"], ev["elapsed"]
            )

    def _run(self, app: Flask, targets: list[dict[str, Any]]) -> None:
        # Each target runs in its own child process (benchmark_worker.py). The
        # child streams progress events back; stop() sets _stop_event, which
        # makes stream_target_subprocess SIGKILL the active child — closing its
        # provider socket so a runaway model stops pegging CPU/GPU.
        prev_model_name: str | None = None
        try:
            # app_context is not needed for the state dict, but keep it so any
            # future DB-touching setter is safe; the child does its own DB work.
            with app.app_context():
                for run_idx, target in enumerate(targets):
                    if self._stop_event.is_set():
                        break
                    ti = target["state_index"]
                    logger.info(
                        "benchmark: target %d/%d %s%s",
                        run_idx + 1,
                        len(targets),
                        target["model_name"],
                        f" / {target['display_name']}" if target["display_name"] else "",
                    )
                    # The model already lives in memory after the previous
                    # target on the same model, so skip the child's warmup.
                    skip_warmup = target["model_name"] == prev_model_name
                    prev_model_name = target["model_name"]
                    request = {"target_uuid": target["uuid"],
                               "skip_warmup": skip_warmup,
                               "spec_set": self.spec_set}
                    killed = stream_target_subprocess(
                        _BENCHMARK_WORKER,
                        request,
                        lambda ev, _ti=ti: self._apply_event(_ti, ev),
                        self._stop_event,
                    )
                    if killed:
                        break
        finally:
            self._finish(aborted=self._stop_event.is_set())
