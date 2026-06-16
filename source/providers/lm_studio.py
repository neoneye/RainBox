"""LM Studio provider.

The OpenAI-compatible endpoint at :1234 does not let you change a loaded
model's context length per request — context is baked in at model load
time. To grow it, we call LM Studio's own management surface, the `lms`
CLI. Status comes from LM Studio's REST API; mutations go through the
CLI.
"""

import atexit
import json
import logging
import os
import shutil
import signal
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

import requests

from .base import Provider, ProviderId

_log = logging.getLogger(__name__)

_DEFAULT_BASE_URL: str = "http://127.0.0.1:1234"
_MODELS_TIMEOUT: float = 3.0
_COMPLETION_TIMEOUT: float = 60.0

# Default keepalive: one hour bounds idle resident memory while surviving
# typical interactive gaps.
DEFAULT_TTL_SECONDS: int = 3600

# Standard install location on macOS; fallback when `lms` isn't on PATH.
_DEFAULT_LMS_PATHS = (
    Path.home() / ".cache" / "lm-studio" / "bin" / "lms",
)


def _base_url() -> str:
    return os.environ.get("LM_STUDIO_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")


def _lms_path() -> str:
    env = os.environ.get("LMS")
    if env:
        return env
    on_path = shutil.which("lms")
    if on_path:
        return on_path
    for candidate in _DEFAULT_LMS_PATHS:
        if candidate.is_file():
            return str(candidate)
    raise FileNotFoundError(
        "could not find the `lms` CLI on PATH or in ~/.cache/lm-studio/bin/; "
        "install LM Studio's CLI or set $LMS to its path"
    )


# How long any `lms` subcommand may run before we give up and kill it. The CLI
# normally finishes in seconds (a cold GPU model load is the slow case); without
# a bound, a wedged call — e.g. the LM Studio server dying mid-load — spins
# indefinitely, and if RainBox then exits the child reparents to launchd/init
# and burns CPU forever. That orphan is exactly what this guards against.
_LMS_OP_TIMEOUT: float = 180.0

# Live `lms` children, killed on interpreter exit so a graceful shutdown never
# leaves one behind. SIGKILL of RainBox itself can't be caught, so this is
# best-effort cleanup; the timeout above is the real backstop.
_LIVE_LMS_PROCS: set[subprocess.Popen] = set()


def _kill_lms_group(proc: subprocess.Popen) -> None:
    """SIGKILL the child's whole process group (it is started in its own
    session), falling back to the bare pid if the group is already gone — so a
    timed-out `lms` can't leave grandchildren spinning."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def _cleanup_lms_procs() -> None:
    """atexit hook: take down any `lms` child still running at shutdown."""
    for proc in list(_LIVE_LMS_PROCS):
        if proc.poll() is None:
            _kill_lms_group(proc)
        _LIVE_LMS_PROCS.discard(proc)


atexit.register(_cleanup_lms_procs)


def _run_lms(args: list[str], *, timeout: float = _LMS_OP_TIMEOUT) -> str:
    """Run an `lms` subcommand bounded by `timeout` and return its stdout.

    Mirrors ``subprocess.run(..., check=True)`` (raises CalledProcessError on a
    non-zero exit, TimeoutExpired on overrun) but starts the child in its own
    session so a timeout can SIGKILL the entire process group — subprocess's own
    timeout handling only kills the direct child. Tracks the child so the atexit
    hook can reap it if RainBox exits mid-call."""
    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True,
    )
    _LIVE_LMS_PROCS.add(proc)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _log.warning(
            "lm_studio: `%s` exceeded %.0fs — killing it", " ".join(args), timeout
        )
        _kill_lms_group(proc)
        proc.communicate()  # reap the killed child so it isn't left a zombie
        raise
    finally:
        _LIVE_LMS_PROCS.discard(proc)
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, args, out, err)
    return out


def _list_native_models_via_api() -> list[dict[str, Any]]:
    url = f"{_base_url()}/api/v0/models"
    with urllib.request.urlopen(url, timeout=5) as resp:
        body = json.loads(resp.read())
    return list(body.get("data") or [])


def find_instances(base_id: str, models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """All entries that map to `base_id`. LM Studio appends `:N` to a loaded
    instance's identifier when there's a name collision."""
    return [
        m for m in models
        if m.get("id") == base_id or str(m.get("id", "")).startswith(base_id + ":")
    ]


def _max_loaded_context(instances: list[dict[str, Any]]) -> int:
    """Largest `loaded_context_length` across loaded instances."""
    loaded = [i for i in instances if i.get("state") == "loaded"]
    if not loaded:
        return 0
    return max(int(i.get("loaded_context_length") or 0) for i in loaded)


class _LMStudioProvider:
    id: ProviderId = "lm_studio"
    display_name: str = "LM Studio"

    def base_url(self) -> str:
        return _base_url()

    def list_models(self) -> list[str]:
        resp = requests.get(f"{self.base_url()}/v1/models", timeout=_MODELS_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return [m["id"] for m in data.get("data", [])]

    def fetch_native_models(self) -> list[dict[str, Any]] | None:
        try:
            resp = requests.get(
                f"{self.base_url()}/api/v0/models", timeout=_MODELS_TIMEOUT
            )
            resp.raise_for_status()
            return resp.json().get("data", [])
        except requests.RequestException:
            return None

    def fetch_model_sizes(self) -> dict[str, int]:
        try:
            proc = subprocess.run(
                ["lms", "ls", "--json"],
                check=True, capture_output=True, text=True, timeout=5.0,
            )
            rows = json.loads(proc.stdout)
        except (FileNotFoundError, subprocess.CalledProcessError,
                subprocess.TimeoutExpired, json.JSONDecodeError):
            return {}
        out: dict[str, int] = {}
        for row in rows:
            key = row.get("modelKey")
            size = row.get("sizeBytes")
            if isinstance(key, str) and isinstance(size, int):
                out[key] = size
        return out

    def default_arguments(self) -> dict[str, Any]:
        return {
            "api_base": f"{self.base_url()}/v1",
            "api_key": "lm-studio",
            "is_chat_model": True,
            "is_function_calling_model": False,
            "should_use_structured_outputs": True,
            "timeout": _COMPLETION_TIMEOUT,
        }

    def ensure_loaded(
        self,
        model: str,
        context_window: int,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        """Make sure LM Studio has `model` loaded with at least
        `context_window` tokens of context, blocking until it is."""
        models = _list_native_models_via_api()
        instances = find_instances(model, models)
        if _max_loaded_context(instances) >= context_window:
            _log.debug(
                "lm_studio: %s already loaded with >= %d ctx; skipping reload",
                model, context_window,
            )
            return

        lms = _lms_path()
        for inst in instances:
            if inst.get("state") != "loaded":
                continue
            identifier = inst.get("id")
            if not identifier:
                continue
            _log.info(
                "lm_studio: unloading %s (loaded ctx=%s < required %d)",
                identifier, inst.get("loaded_context_length"), context_window,
            )
            _run_lms([lms, "unload", str(identifier)])
        _log.info(
            "lm_studio: loading %s with context-length=%d ttl=%ds",
            model, context_window, ttl_seconds,
        )
        _run_lms([
            lms, "load", model,
            "--context-length", str(context_window),
            "--gpu", "max",
            "--ttl", str(ttl_seconds),
        ])


PROVIDER: Provider = _LMStudioProvider()
