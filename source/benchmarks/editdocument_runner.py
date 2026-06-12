"""Background orchestration for /benchmark_editdocument.

Owns a single worker thread and a state dict polled by the webapp. Iterates
every available target in the /models tree, runs every EditDocumentTest
against the selected agent (v1, v2, or v3), and streams results into the state.

Mirrors benchmarks.runner.BenchmarkRunner's lifecycle (start / stop /
get_state) so the webapp's polling code looks the same.
"""

import logging
import threading
import time
from typing import Any
from uuid import UUID

from flask import Flask

import db
from benchmarks.subproc import stream_target_subprocess
from agent_config import (
    EDIT_DOCUMENT_V1_UUID,
    EDIT_DOCUMENT_V2_UUID,
    EDIT_DOCUMENT_V3_UUID,
    EDIT_DOCUMENT_V4_UUID,
    EDIT_DOCUMENT_V5_UUID,
    EDIT_DOCUMENT_V6_UUID,
)
from agent_edit_document_v1 import EditDocumentAgentV1
from agent_edit_document_v2 import EditDocumentAgentV2
from agent_edit_document_v3 import EditDocumentAgentV3
from agent_edit_document_v4 import EditDocumentAgentV4
from agent_edit_document_v5 import EditDocumentAgentV5
from agent_edit_document_v6 import EditDocumentAgentV6
from benchmarks.editdocument import EDIT_DOCUMENT_TESTS

logger = logging.getLogger(__name__)

# Each target runs in its own child process so a stuck model can be SIGKILLed.
_EDITDOC_WORKER_MODULE = "benchmarks.editdocument_worker"


# Agent registry: maps the picker's choice string to the (class, role_uuid, role_name)
# triple BenchmarkEditDocument needs. Add a row when a new editor agent ships.
AGENT_REGISTRY: dict[str, tuple[type, UUID, str]] = {
    "v1": (EditDocumentAgentV1, EDIT_DOCUMENT_V1_UUID, "edit_document_v1"),
    "v2": (EditDocumentAgentV2, EDIT_DOCUMENT_V2_UUID, "edit_document_v2"),
    "v3": (EditDocumentAgentV3, EDIT_DOCUMENT_V3_UUID, "edit_document_v3"),
    "v4": (EditDocumentAgentV4, EDIT_DOCUMENT_V4_UUID, "edit_document_v4"),
    "v5": (EditDocumentAgentV5, EDIT_DOCUMENT_V5_UUID, "edit_document_v5"),
    "v6": (EditDocumentAgentV6, EDIT_DOCUMENT_V6_UUID, "edit_document_v6"),
}

DEFAULT_AGENT_CHOICE: str = "v6"


def _empty_trial_entry(name: str) -> dict[str, Any]:
    return {
        "status": "pending",  # pending | running | done | error
        "correct": None,
        "elapsed": None,
        "error": None,
        "applied": None,
        "expected": None,
        "patches": None,
        "agent_status": None,
        "agent_comment": None,
        "thinking_chars": None,
        "content_chars": None,
    }


def _empty_target_entry(target: dict[str, Any]) -> dict[str, Any]:
    return {
        "index": target.get("index"),
        "kind": target["kind"],
        "uuid": str(target["uuid"]),
        "provider": target.get("provider", ""),
        "model_name": target["model_name"],
        "model_display_name": target.get("model_display_name"),
        "display_name": target.get("display_name"),  # None for configs, override's display_name for overrides
        "status": "pending",  # pending | running | done | error
        "trials": {t.name: _empty_trial_entry(t.name) for t in EDIT_DOCUMENT_TESTS},
    }


def _list_available_targets() -> list[dict[str, Any]]:
    """Return one dict per ModelConfigOverride that is (a) under an
    available ModelConfig and (b) resolves to should_use_structured_outputs.

    Source of truth for ordering is db.list_model_configs_with_overrides()
    — the same function /models and /benchmark use — so the three pages
    agree on row order. The edit-document agents emit their edits as
    structured output (not tool calls), so the only requirement is
    structured-output support; bare base configs and non-structured overrides
    aren't useful targets here and are filtered out."""
    targets: list[dict[str, Any]] = []
    index = 0
    for cfg, overrides in db.list_model_configs_with_overrides():
        if not cfg.available:
            continue
        for ov in overrides:
            if not db.member_uses_structured_output(ov.uuid):
                continue
            targets.append({
                "index": index,
                "kind": "override",
                "uuid": ov.uuid,
                "provider": cfg.provider,
                "model_name": cfg.model_name,
                "model_display_name": cfg.effective_display_name,
                # effective_display_name: user-set display_name if any,
                # else the synthesized "t0.5 c32k struct" summary.
                "display_name": ov.effective_display_name,
            })
            index += 1
    return targets


class BenchmarkEditDocumentRunner:
    """Single-instance orchestrator. The webapp instantiates one of these
    at import time and routes /benchmark_editdocument/{start,stop,state} to
    it."""

    def __init__(self, app: Flask) -> None:
        self._app = app
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = {
            "running": False,
            "started_at": None,
            "ended_at": None,
            "aborted": False,
            "agent_choice": DEFAULT_AGENT_CHOICE,
            "current_target_index": -1,
            "total_targets": 0,
            "targets": [],
            "test_names": [t.name for t in EDIT_DOCUMENT_TESTS],
            "test_descriptions": {t.name: t.description for t in EDIT_DOCUMENT_TESTS},
        }

    def get_state(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def ensure_targets_populated(self) -> None:
        """Refresh the targets list when idle so the page can render an
        empty grid before the first Start."""
        with self._lock:
            if self._state["running"]:
                return
        with self._app.app_context():
            available = _list_available_targets()
        with self._lock:
            self._state["targets"] = [_empty_target_entry(t) for t in available]
            self._state["total_targets"] = len(available)

    def start(self, agent_choice: str, target_uuids: list[str] | None = None) -> bool:
        """Kick off a run in the background. Returns False if already running.

        target_uuids=None runs every available target. A list runs only those;
        cached state for the other targets is preserved so per-row Start
        clicks accumulate results across multiple runs (same pattern as
        BenchmarkRunner.start)."""
        if agent_choice not in AGENT_REGISTRY:
            raise ValueError(f"unknown agent_choice {agent_choice!r}")
        with self._lock:
            if self._state["running"]:
                return False
            self._stop_event.clear()
            self._state["running"] = True
            self._state["started_at"] = time.time()
            self._state["ended_at"] = None
            self._state["aborted"] = False
            self._state["agent_choice"] = agent_choice
            self._state["current_target_index"] = -1
        self._thread = threading.Thread(
            target=self._run, args=(agent_choice, target_uuids),
            name="benchmark-editdoc", daemon=True,
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        with self._lock:
            if not self._state["running"]:
                return
            self._state["aborted"] = True
        self._stop_event.set()

    def _set_trial(self, target_index: int, test_name: str, **fields: Any) -> None:
        with self._lock:
            entry = self._state["targets"][target_index]["trials"][test_name]
            entry.update(fields)

    def _set_target(self, target_index: int, **fields: Any) -> None:
        with self._lock:
            self._state["targets"][target_index].update(fields)

    def _apply_event(self, ti: int, ev: dict[str, Any]) -> None:
        """Map one NDJSON event from the per-target child onto the state
        setters the polling UI reads."""
        kind = ev.get("t")
        if kind == "target_status":
            self._set_target(ti, status=ev["status"])
        elif kind == "trial_start":
            self._set_trial(ti, ev["test_name"], status="running")
        elif kind == "trial":
            self._set_trial(
                ti, ev["test_name"],
                status="done",
                correct=ev["correct"],
                elapsed=ev["elapsed"],
                error=ev.get("error"),
                applied=ev.get("applied"),
                patches=ev.get("patches"),
                agent_status=ev.get("agent_status"),
                agent_comment=ev.get("agent_comment"),
                thinking_chars=ev.get("thinking_chars"),
                content_chars=ev.get("content_chars"),
            )

    def _run(self, agent_choice: str, target_uuids: list[str] | None) -> None:
        with self._app.app_context():
            try:
                available = _list_available_targets()
            except Exception:
                logger.exception("benchmark_editdocument: failed to list targets")
                with self._lock:
                    self._state["running"] = False
                    self._state["ended_at"] = time.time()
                return

            # Filter set for per-row Start clicks. None = run everything.
            run_set: set[str] | None = (
                set(target_uuids) if target_uuids is not None else None
            )
            existing = {t["uuid"]: t for t in self._state.get("targets", [])}

            # Build the new targets list: reset only the rows we're about to
            # run; keep cached entries for the others so accumulated results
            # from earlier per-row Start clicks stay visible.
            new_targets: list[dict[str, Any]] = []
            for t in available:
                uuid_str = str(t["uuid"])
                if run_set is None or uuid_str in run_set:
                    entry = _empty_target_entry(t)
                    for test in EDIT_DOCUMENT_TESTS:
                        entry["trials"][test.name]["expected"] = test.expected
                    new_targets.append(entry)
                else:
                    cached = existing.get(uuid_str)
                    new_targets.append(dict(cached) if cached is not None else _empty_target_entry(t))
            with self._lock:
                self._state["targets"] = new_targets
                self._state["total_targets"] = len(new_targets)

            for ti, t in enumerate(available):
                if self._stop_event.is_set():
                    break
                if run_set is not None and str(t["uuid"]) not in run_set:
                    continue
                with self._lock:
                    self._state["current_target_index"] = ti
                self._set_target(ti, status="running")
                # Run this target in its own child process so stop() can
                # SIGKILL it (closing the provider socket) if the model hangs.
                request = {"target_uuid": str(t["uuid"]), "agent_choice": agent_choice}
                killed = stream_target_subprocess(
                    _EDITDOC_WORKER_MODULE,
                    request,
                    lambda ev, _ti=ti: self._apply_event(_ti, ev),
                    self._stop_event,
                )
                if killed:
                    break

            with self._lock:
                self._state["running"] = False
                self._state["ended_at"] = time.time()
                self._state["current_target_index"] = -1
