"""Background orchestration for /benchmark_basic: iterates targets in /models tree
order, runs every benchmark per target, and maintains a state dict the
webapp polls."""

import logging
import threading
import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from flask import Flask

import db
from benchmarks.basic import (
    BenchmarkBase64Decode,
    BenchmarkBase64Encode,
    BenchmarkReverseList,
    BenchmarkReverseString,
    BenchmarkToolOrder,
    BenchmarkToolRoute,
)
from benchmarks.kanban import BenchmarkKanbanOpStructured, BenchmarkKanbanOpTools
from benchmarks.story import (
    BenchmarkStoryStruct,
    BenchmarkStoryStructTool,
    BenchmarkStoryText,
    BenchmarkStoryTextTool,
)
from benchmarks.exclusion import SLOT
from benchmarks.subproc import stream_target_subprocess

logger = logging.getLogger(__name__)

# Each target runs in its own child process so a stuck model can be SIGKILLed.
_BENCHMARK_WORKER_MODULE = "benchmarks.worker"

BENCHMARK_SPECS: list[tuple[str, type, dict[str, Any]]] = [
    ("base64_decode", BenchmarkBase64Decode, {"num_trials": 5, "string_length": 6}),
    ("base64_encode", BenchmarkBase64Encode, {"num_trials": 5, "string_length": 6}),
    ("reverse_string", BenchmarkReverseString, {"num_trials": 5, "string_length": 6}),
    ("reverse_list", BenchmarkReverseList, {"num_trials": 5, "num_items": 5, "item_length": 4}),
    ("tool_order", BenchmarkToolOrder, {"num_trials": 5}),
    ("tool_route", BenchmarkToolRoute, {"num_trials": 5}),
]

# The kanban "first slice" (notes/kanban-design.md roadmap item 1): the 2×2
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

# Ten-turn conversations across the text/structured × no-tools/tools matrix
# (docs/superpowers/specs/2026-08-10-story-benchmarks-design.md). Three trials
# rather than the usual five: each trial is ten LLM calls, so this set already
# costs 120 calls per target.
STORY_BENCHMARK_SPECS: list[tuple[str, type, dict[str, Any]]] = [
    ("story_text", BenchmarkStoryText, {"num_trials": 3}),
    ("story_struct", BenchmarkStoryStruct, {"num_trials": 3}),
    ("story_text_tool", BenchmarkStoryTextTool, {"num_trials": 3}),
    ("story_struct_tool", BenchmarkStoryStructTool, {"num_trials": 3}),
]

# Spec sets by name: each BenchmarkRunner instance (and its worker child)
# runs exactly one set; the name travels in the worker request JSON.
SPEC_SETS: dict[str, list[tuple[str, type, dict[str, Any]]]] = {
    "general": BENCHMARK_SPECS,
    "kanban": KANBAN_BENCHMARK_SPECS,
    "story": STORY_BENCHMARK_SPECS,
}

# What a page says when another suite holds the machine. Named per spec set
# rather than per page so the message points at the suite, which is what the
# operator recognizes.
SPEC_SET_LABELS: dict[str, str] = {
    "general": "General benchmark",
    "kanban": "Kanban benchmark",
    "story": "Story benchmark",
}


def _empty_benchmark_entry(name: str, total: int) -> dict[str, Any]:
    return {
        "name": name,
        "status": "pending",  # pending | running | done | error
        # Wall-clock start of this cell, stamped when it goes running. The
        # stored result carries it so a history entry says when it was taken.
        "started_at": None,
        "trials_done": 0,
        "trials_total": total,
        "correct": 0,
        "mistakes": 0,
        "failures": 0,
        "total_elapsed": 0.0,
        "error": None,
        "reasoning_chars": None,
        "content_chars": None,
        # Per-trial artifacts the page can hand to the operator — the story
        # benchmarks put their assembled markdown here. Empty for spec sets
        # whose trials produce nothing worth reading.
        "stories": [],
    }


class BenchmarkRunner:
    """Single-instance orchestrator. Owns the worker thread + state dict.

    The webapp instantiates one of these at import time and routes wire
    /benchmark_basic/start, /benchmark_basic/stop, /benchmark_basic/state to it."""

    def __init__(self, spec_set: str = "general") -> None:
        self.spec_set = spec_set
        self.specs = SPEC_SETS[spec_set]
        # Identifies this runner in the shared one-at-a-time slot.
        self.label = SPEC_SET_LABELS[spec_set]
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        # Per-trial artifacts, keyed (target_index, bench_index, trial).
        # Deliberately NOT in `_state`: the page polls that once a second,
        # and a few hundred KB of story text per target would ride along
        # every time. The state carries a descriptor; the text is fetched
        # on demand.
        self._artifacts: dict[tuple[int, int, int], dict[str, Any]] = {}
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
            state = dict(self._state)
        # `blocked_by` names the suite currently holding the machine, so a page
        # that can't start says why instead of just disabling its buttons.
        # Keyed on the holder rather than on `running`, because start() takes
        # the slot a moment before it sets running — in that window this runner
        # would otherwise report itself as the thing blocking it.
        holder = SLOT.holder()
        state["blocked_by"] = None if holder in (None, self.label) else holder
        return state

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

    def _worker_request(
        self, target_uuid: str, skip_warmup: bool,
        bench_indices: list[int] | None,
    ) -> dict[str, Any]:
        """The JSON the child process is fed. `bench_indices` is None for a
        whole row, or the positions of the cells to run."""
        return {
            "target_uuid": target_uuid,
            "skip_warmup": skip_warmup,
            "spec_set": self.spec_set,
            "bench_indices": bench_indices,
        }

    def _validate_bench_indices(
        self, bench_indices: list[int] | None
    ) -> list[int] | None:
        """Indices pick a spec by position and arrive from a query string."""
        if bench_indices is None:
            return None
        for i in bench_indices:
            if not 0 <= i < len(self.specs):
                raise ValueError(
                    f"benchmark index {i} out of range for {self.spec_set}"
                )
        return sorted(set(bench_indices))

    def _reset_for_run(
        self, target_index: int, bench_indices: list[int] | None
    ) -> None:
        """Clear the cells about to be re-run, and only those.

        Re-running one cell must not wipe the results beside it: the operator
        paid for those in minutes of model time and did not ask to lose them.
        """
        chosen = self._validate_bench_indices(bench_indices)
        target = self._state["targets"][target_index]
        for i, (name, _cls, kwargs) in enumerate(self.specs):
            if chosen is None or i in chosen:
                target["benchmarks"][i] = _empty_benchmark_entry(
                    name, kwargs.get("num_trials", 5)
                )

    def start(
        self,
        app: Flask,
        target_uuids: list[str] | None = None,
        bench_indices: list[int] | None = None,
        warmup: bool = True,
    ) -> bool:
        """Kick off a run in the background.

        target_uuids=None means run every target in the /models tree.
        If a list is given, only those targets are run; other targets in the
        state keep their previous values (so the page shows accumulated
        results across multiple per-target Start clicks).

        bench_indices=None runs every benchmark for those targets; a list runs
        only those cells, leaving the rest of the row as it was. The story
        suite runs four benchmarks in order and the interesting one is often
        last, so waiting through the others to reach it is minutes of nothing.

        warmup=False skips the pre-trial "hi" call on every target. That call
        exists so a cold model's load time doesn't land inside the first
        benchmark's average; skipping it is for when the run is being read for
        cache behaviour rather than for timings, where a warm model before the
        first trial is the thing being measured.

        Returns False if a run is already in progress, or if another benchmark
        suite holds the shared slot (see benchmarks/exclusion.py — the models
        run on one local machine and cannot share it)."""
        with self._lock:
            if self._state["running"]:
                return False
        # Take the machine before touching any state, so a suite that loses the
        # race leaves the winner's run completely untouched. The slot also
        # covers this runner's own re-entry, since it already holds it.
        if not SLOT.acquire(self.label):
            return False
        try:
            self._begin_run(app, target_uuids, bench_indices, warmup)
        except BaseException:
            # _validate_bench_indices raises on a bad index and _collect_targets
            # touches the DB; either way no worker thread exists yet to hand the
            # slot back, so it must be released here or every page stays locked
            # out until restart.
            SLOT.release(self.label)
            raise
        return True

    def _begin_run(
        self,
        app: Flask,
        target_uuids: list[str] | None,
        bench_indices: list[int] | None,
        warmup: bool = True,
    ) -> None:
        """Build the run state and hand it to the worker thread. Called only by
        start(), with the shared slot already held; _finish() releases it."""
        with self._lock:
            chosen = self._validate_bench_indices(bench_indices)
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
                    cached = existing.get(uuid_str)
                    if chosen is None or cached is None:
                        new_targets_state.append(_fresh_entry(i, t))
                    else:
                        # Keep the untouched cells; blank only what re-runs.
                        kept = dict(cached)
                        kept["index"] = i
                        kept["benchmarks"] = list(cached["benchmarks"])
                        kept["status"] = "pending"
                        for bi, (name, _c, kw) in enumerate(self.specs):
                            if bi in chosen:
                                kept["benchmarks"][bi] = _empty_benchmark_entry(
                                    name, kw.get("num_trials", 5)
                                )
                        new_targets_state.append(kept)
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
            target=self._run, args=(app, run_targets, chosen, warmup),
            name="benchmark-runner", daemon=True,
        )
        self._thread.start()

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
            if status == "running" and not entry.get("started_at"):
                entry["started_at"] = time.time()
            if error is not None:
                entry["error"] = error
            if reasoning_chars is not None:
                entry["reasoning_chars"] = reasoning_chars
            if content_chars is not None:
                entry["content_chars"] = content_chars
        # Outside the lock: _persist_cell takes it again for its own snapshot.
        if status in ("done", "error"):
            self._persist_cell(target_index, bench_index, status)

    def _persist_cell(
        self, target_index: int, bench_index: int, status: str
    ) -> None:
        """Store one cell's terminal result, so it outlives this process.

        Snapshots under the lock and writes outside it: the page polls
        get_state() on that same lock once a second, and a DB round-trip
        inside it would stall every benchmark page on the box.

        A stop before the first trial is not recorded — it measured nothing,
        and a row of zeroes would put a fake result into the baseline. An
        error IS recorded whatever the trial count, because "this target
        cannot do this benchmark" is itself the finding.

        Logs and swallows on failure. A run costs real model time; losing one
        to a telemetry bug is far worse than losing the telemetry (the posture
        llm/activity.py takes for the same reason).
        """
        try:
            with self._lock:
                target = self._state["targets"][target_index]
                entry = dict(target["benchmarks"][bench_index])
                target_uuid = target["uuid"]
                target_label = target.get("display_name") or ""
                model_name = target.get("model_name") or ""
                provider = target.get("provider") or ""
            if status != "error" and entry.get("trials_done", 0) <= 0:
                return

            name, _cls, params = self.specs[bench_index]
            try:
                resolved = db.resolved_model_kwargs(UUID(target_uuid))
            except Exception:
                # A row deleted mid-run still deserves its result stored; it
                # just cannot report what it was configured with.
                resolved = None

            db.record_benchmark_result(
                spec_set=self.spec_set,
                benchmark_name=name,
                target_uuid=UUID(target_uuid),
                target_label=target_label,
                model_name=model_name,
                provider=provider,
                status=status,
                trials_done=entry.get("trials_done", 0),
                trials_total=entry.get("trials_total", 0),
                correct=entry.get("correct", 0),
                mistakes=entry.get("mistakes", 0),
                failures=entry.get("failures", 0),
                total_elapsed=entry.get("total_elapsed", 0.0),
                reasoning_chars=entry.get("reasoning_chars"),
                content_chars=entry.get("content_chars"),
                error=entry.get("error"),
                config_fingerprint=(
                    db.benchmark_fingerprint(resolved) if resolved else ""
                ),
                spec_fingerprint=db.benchmark_fingerprint(params),
                started_at=(
                    datetime.fromtimestamp(entry["started_at"], UTC)
                    if entry.get("started_at") else None
                ),
                ended_at=datetime.now(UTC),
            )
        except Exception:
            logger.warning(
                "benchmark: could not store result for target %d bench %d",
                target_index, bench_index, exc_info=True,
            )

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

    def _record_story(
        self, target_index: int, bench_index: int, trial: int, text: str,
        correct: bool, topic: str = "", transcript: dict | None = None,
    ) -> None:
        """Store a trial's artifacts and put a small descriptor on the state.

        Half the value of a benchmark that writes fiction is reading the
        fiction; the other half is the JSON transcript, which is what tells
        you why a trial failed. Both are held here rather than in the polled
        state so the page stays cheap to refresh.
        """
        with self._lock:
            self._artifacts[(target_index, bench_index, trial)] = {
                "markdown": text,
                "transcript": transcript,
            }
            entry = self._state["targets"][target_index]["benchmarks"][bench_index]
            entry.setdefault("stories", []).append(
                {"trial": trial, "correct": correct, "topic": topic}
            )

    def get_artifact(
        self, target_index: int, bench_index: int, trial: int
    ) -> dict[str, Any] | None:
        """One trial's stored artifacts, or None if it was never recorded."""
        with self._lock:
            return self._artifacts.get((target_index, bench_index, trial))

    def _finish(self, aborted: bool) -> None:
        # Hand the machine back first: this runs in the worker thread's finally,
        # so it is the one place reached whether the run ended, was stopped, or
        # raised. Releasing before taking self._lock keeps the slot from
        # outliving a failure in the state bookkeeping below.
        SLOT.release(self.label)
        # (target_index, bench_index) of cells the stop caught mid-flight.
        # Collected under the lock, written after it — the DB round-trip must
        # not block the once-a-second get_state() poll.
        interrupted: list[tuple[int, int]] = []
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
                for ti, t in enumerate(self._state["targets"]):
                    if t["status"] in ("warming_up", "running"):
                        t["status"] = "pending"
                    for bi, b in enumerate(t["benchmarks"]):
                        if b["status"] == "running":
                            # Noted before the reset erases the evidence that
                            # this cell was the one interrupted.
                            interrupted.append((ti, bi))
                            b["status"] = "pending"
        for ti, bi in interrupted:
            # _persist_cell drops the ones with no trials done, so a cell
            # killed before its first trial stores nothing.
            self._persist_cell(ti, bi, "stopped")

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
        elif kind == "story":
            self._record_story(
                ti, ev["bi"], ev["trial"], ev["text"], ev.get("correct", False),
                ev.get("topic", ""), ev.get("transcript"),
            )

    def _run(
        self, app: Flask, targets: list[dict[str, Any]],
        bench_indices: list[int] | None = None,
        warmup: bool = True,
    ) -> None:
        # Each target runs in its own child process (benchmarks.worker). The
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
                    # Skipped either because the operator turned warmup off
                    # for this run, or because the model already lives in
                    # memory after the previous target on the same model.
                    skip_warmup = (
                        not warmup or target["model_name"] == prev_model_name
                    )
                    prev_model_name = target["model_name"]
                    request = self._worker_request(
                        target["uuid"], skip_warmup, bench_indices
                    )
                    killed = stream_target_subprocess(
                        _BENCHMARK_WORKER_MODULE,
                        request,
                        lambda ev, _ti=ti: self._apply_event(_ti, ev),
                        self._stop_event,
                    )
                    if killed:
                        break
        finally:
            # Its own context: the one above closed when the try block exited,
            # and _finish now stores the results a stop interrupted.
            with app.app_context():
                self._finish(aborted=self._stop_event.is_set())
