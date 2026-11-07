"""Background orchestration for /benchmark_vision.

Targets run one after another, each in its own killable child process
(benchmarks.vision_worker). Every trial is stored as it arrives, so the page
reads results from the database and the in-memory state carries only what is
happening right now — which target, which case, how far along.

Shares the one-suite-at-a-time slot with the other benchmark pages
(benchmarks/exclusion.py): the models run on one local machine.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import requests
from flask import Flask

import db
from benchmarks.exclusion import SLOT
from benchmarks.subproc import stream_target_subprocess
from benchmarks.vision_ocr import CASES, Target, resolve_target

logger = logging.getLogger(__name__)

_WORKER_MODULE = "benchmarks.vision_worker"
LABEL = "Vision benchmark"
MAX_TRIALS = 10

# (Ollama base URL, model name) -> has the "vision" capability. Asked once per
# process: a model's capabilities do not change while it is installed.
_vision_cache: dict[tuple[str, str], bool] = {}


def ollama_has_vision(base_url: str, model_name: str) -> bool | None:
    """Whether Ollama lists "vision" among the model's capabilities, or None
    when Ollama could not be asked (not cached, so a later page load retries)."""
    key = (base_url, model_name)
    if key in _vision_cache:
        return _vision_cache[key]
    try:
        resp = requests.post(f"{base_url.rstrip('/')}/api/show",
                             json={"model": model_name}, timeout=5)
        resp.raise_for_status()
        caps = resp.json().get("capabilities") or []
    except (requests.RequestException, ValueError):
        return None
    _vision_cache[key] = "vision" in caps
    return _vision_cache[key]


def lm_studio_vision_models() -> set[str]:
    """LM Studio model ids its native API marks as vision models
    (`type: "vlm"`); empty when LM Studio is not running."""
    import providers

    rows = providers.get("lm_studio").fetch_native_models() or []
    return {r["id"] for r in rows if r.get("type") == "vlm" and isinstance(r.get("id"), str)}


def openrouter_vision_models() -> set[str]:
    """OpenRouter model ids whose catalog entry takes images as input; empty
    when no key is set or the catalog cannot be fetched."""
    from providers import openrouter

    rows = openrouter.fetch_catalog() or []
    return {r["id"] for r in rows
            if "image" in ((r.get("architecture") or {}).get("input_modalities") or [])}


class _VisionCheck:
    """Says whether a model takes images, asking each provider at most once
    per page load. A provider that does not report it (Jan) answers no: the
    page lists only models known to be vision-capable."""

    def __init__(self) -> None:
        self._lists: dict[str, set[str]] = {}

    def __call__(self, provider: str, model_name: str, arguments: dict[str, Any]) -> bool:
        if provider == "ollama":
            import providers
            base = str(arguments.get("base_url") or "") or providers.get("ollama").base_url()
            return ollama_has_vision(base, model_name) is True
        listers = {"lm_studio": lm_studio_vision_models,
                   "openrouter": openrouter_vision_models}
        if provider not in listers:
            return False
        if provider not in self._lists:
            self._lists[provider] = listers[provider]()
        return model_name in self._lists[provider]


def _settings_summary(arguments: dict[str, Any]) -> str:
    """The settings that change a vision answer, for the row label."""
    parts: list[str] = []
    if isinstance(arguments.get("thinking"), bool):
        parts.append("thinking on" if arguments["thinking"] else "thinking off")
    else:
        parts.append("thinking: model default")
    if arguments.get("temperature") is not None:
        parts.append(f"t{arguments['temperature']}")
    if arguments.get("context_window"):
        parts.append(f"ctx {arguments['context_window']}")
    return " · ".join(parts)


def vision_targets() -> list[dict[str, Any]]:
    """Every available model config and override whose model takes images,
    in /models tree order. Needs an app context.

    Ollama (`/api/show` capabilities), LM Studio (`type: "vlm"`) and
    OpenRouter (the catalog's input modalities) say which models have
    vision. A model whose provider does not say, or could not be asked, is
    left out."""
    has_vision = _VisionCheck()
    out: list[dict[str, Any]] = []
    for cfg, overrides in db.list_model_configs_with_overrides():
        if not cfg.available or not has_vision(cfg.provider, cfg.model_name, cfg.arguments):
            continue
        rows = [(cfg.uuid, "config", cfg.effective_display_name)]
        rows += [(ov.uuid, "override", ov.effective_display_name) for ov in overrides]
        for uid, kind, label in rows:
            try:
                _provider, _model, args = db.resolved_model_kwargs(uid)
            except LookupError:
                continue
            out.append({
                "uuid": str(uid),
                "kind": kind,
                "label": label,
                "model_name": cfg.model_name,
                "provider": cfg.provider,
                "settings": _settings_summary(args),
            })
    return out


class VisionBenchmarkRunner:
    """One per process; the webapp creates it and routes the page's
    start/stop/state endpoints to it."""

    def __init__(self) -> None:
        self.label = LABEL
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = self._idle_state()

    @staticmethod
    def _idle_state() -> dict[str, Any]:
        return {
            "running": False,
            "queue": [],          # target uuids still to run, current first
            "current": None,      # {uuid, label, phase, case, trial, since}
            "trials_done": 0,
            "trials_total": 0,
            # Bumped on every stored trial; the page refetches results when
            # it moves instead of carrying them on this once-a-second poll.
            "revision": 0,
            "errors": [],         # [{uuid, label, error}] from this run
            "started_at": None,
            "ended_at": None,
        }

    def get_state(self) -> dict[str, Any]:
        with self._lock:
            # Copies of the nested parts too: the caller serializes outside
            # the lock while the worker thread keeps updating them.
            state = dict(self._state)
            state["queue"] = list(self._state["queue"])
            state["errors"] = list(self._state["errors"])
            if self._state["current"] is not None:
                state["current"] = dict(self._state["current"])
        holder = SLOT.holder()
        state["blocked_by"] = None if holder in (None, self.label) else holder
        return state

    def start(self, app: Flask, target_uuids: list[str], trials: int = 3,
              warmup: bool = True) -> bool:
        """Run every case `trials` times on each target, in the order given.
        False when a run is already going or another suite holds the slot.
        Raises ValueError for an empty target list or a bad trial count."""
        if not target_uuids:
            raise ValueError("no targets selected")
        if not 1 <= trials <= MAX_TRIALS:
            raise ValueError(f"trials must be between 1 and {MAX_TRIALS}")
        uuids = [str(UUID(u)) for u in target_uuids]
        with self._lock:
            if self._state["running"]:
                return False
        if not SLOT.acquire(self.label):
            return False
        try:
            with self._lock:
                revision = self._state["revision"]
                self._state = self._idle_state()
                self._state.update({
                    "running": True,
                    "queue": list(uuids),
                    "trials_total": len(uuids) * len(CASES) * trials,
                    "revision": revision,
                    "started_at": time.time(),
                })
                self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run, args=(app, uuids, trials, warmup),
                name="benchmark-vision", daemon=True)
            self._thread.start()
        except BaseException:
            SLOT.release(self.label)
            raise
        return True

    def stop(self) -> None:
        self._stop_event.set()

    # --- worker thread --------------------------------------------------------

    def _run(self, app: Flask, uuids: list[str], trials: int, warmup: bool) -> None:
        prev_model: str | None = None
        try:
            with app.app_context():
                for uid in uuids:
                    if self._stop_event.is_set():
                        break
                    try:
                        target = resolve_target(UUID(uid))
                        _p, _m, args = db.resolved_model_kwargs(UUID(uid))
                    except Exception as exc:                  # noqa: BLE001
                        self._note_error(uid, uid, f"{type(exc).__name__}: {exc}")
                        self._dequeue(uid)
                        continue
                    run = {
                        "run_uuid": uuid4(),
                        "started_at": datetime.now(UTC),
                        "fingerprint": db.benchmark_fingerprint(args),
                    }
                    with self._lock:
                        self._state["current"] = {
                            "uuid": uid, "label": target.label, "phase": "starting",
                            "case": None, "trial": None, "since": time.time(),
                        }
                    request = {
                        "target": target.to_json(),
                        "cases": [c.name for c in CASES],
                        "trials": trials,
                        # A model the previous target already loaded is warm.
                        "warmup": warmup and target.model_name != prev_model,
                    }
                    prev_model = target.model_name
                    killed = stream_target_subprocess(
                        _WORKER_MODULE, request,
                        lambda ev, _t=target, _r=run: self._apply_event(_t, _r, ev),
                        self._stop_event,
                    )
                    self._prune(target)
                    self._dequeue(uid)
                    if killed:
                        break
        finally:
            self._finish()

    def _apply_event(self, target: Target, run: dict[str, Any], ev: dict[str, Any]) -> None:
        kind = ev.get("t")
        if kind == "warming_up":
            self._set_current(phase="warming_up", since=time.time())
        elif kind == "warmup_failed":
            logger.warning("benchmark_vision: warmup failed on %s: %s",
                           target.label, ev.get("error"))
        elif kind == "trial_start":
            self._set_current(phase="running", case=ev["case"], trial=ev["trial"],
                              since=time.time())
        elif kind == "trial":
            self._store(target, run, ev["trial"])

    def _set_current(self, **fields: Any) -> None:
        with self._lock:
            if self._state["current"] is not None:
                self._state["current"].update(fields)

    def _store(self, target: Target, run: dict[str, Any], trial: dict[str, Any]) -> None:
        """Write one trial. Logs and swallows a failure: a run costs real model
        time, and losing it to a storage bug is worse than losing one row."""
        try:
            db.record_vision_trial(
                run_uuid=run["run_uuid"],
                run_started_at=run["started_at"],
                target_uuid=UUID(target.uuid),
                target_label=target.label,
                model_name=target.model_name,
                provider=target.provider,
                config_fingerprint=run["fingerprint"],
                trial=trial,
            )
        except Exception:
            logger.warning("benchmark_vision: could not store a trial for %s",
                           target.label, exc_info=True)
            db.session.rollback()
        with self._lock:
            self._state["trials_done"] += 1
            self._state["revision"] += 1

    def _prune(self, target: Target) -> None:
        try:
            db.prune_vision_runs(UUID(target.uuid))
        except Exception:
            logger.warning("benchmark_vision: could not prune runs for %s",
                           target.label, exc_info=True)
            db.session.rollback()

    def _note_error(self, uid: str, label: str, error: str) -> None:
        with self._lock:
            self._state["errors"].append({"uuid": uid, "label": label, "error": error})

    def _dequeue(self, uid: str) -> None:
        with self._lock:
            if uid in self._state["queue"]:
                self._state["queue"].remove(uid)
            self._state["current"] = None

    def _finish(self) -> None:
        SLOT.release(self.label)
        with self._lock:
            self._state["running"] = False
            self._state["current"] = None
            self._state["queue"] = []
            self._state["ended_at"] = time.time()
            self._state["aborted"] = self._stop_event.is_set()
