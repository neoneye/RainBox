"""Pure unit tests for lm_studio.py helpers — no HTTP, no LM Studio.

The full ensure_loaded() integration requires a running LM Studio; that
is exercised by hand via the chat UI (the MCP agent calls ensure_loaded
before each LLM construction). The _run_lms tests below use stock unix
binaries (printf/sh/sleep) as stand-ins for the `lms` CLI — they verify the
timeout/process-group plumbing, not LM Studio itself.
"""

import os
import subprocess
import time

import pytest

from providers import lm_studio
from providers.lm_studio import _max_loaded_context, _run_lms, find_instances


# Sample matching the real /api/v0/models response shape.
_SAMPLE = [
    {
        "id": "ibm/granite-4-h-tiny",
        "state": "loaded",
        "loaded_context_length": 4096,
        "max_context_length": 1048576,
    },
    {
        "id": "ibm/granite-4-h-tiny:2",
        "state": "loaded",
        "loaded_context_length": 8192,
        "max_context_length": 1048576,
    },
    {
        "id": "hermes-2-pro-mistral-7b",
        "state": "not-loaded",
        "max_context_length": 32768,
    },
    {
        "id": "microsoft/phi-4-mini-reasoning",
        "state": "loaded",
        "loaded_context_length": 4096,
    },
]


def test_find_instances_matches_bare_and_suffixed():
    found = find_instances("ibm/granite-4-h-tiny", _SAMPLE)
    ids = {m["id"] for m in found}
    assert ids == {"ibm/granite-4-h-tiny", "ibm/granite-4-h-tiny:2"}


def test_find_instances_does_not_match_unrelated_prefix():
    # "phi-4-mini-reasoning" starts the same as "phi-4-mini" but we shouldn't
    # confuse a longer model name as a `:N` suffix of a shorter one.
    found = find_instances("microsoft/phi-4-mini", _SAMPLE)
    assert found == []


def test_find_instances_returns_empty_for_unknown():
    assert find_instances("does/not-exist", _SAMPLE) == []


def test_max_loaded_context_picks_largest():
    # Two granite instances loaded at 4096 and 8192 → 8192 wins.
    insts = find_instances("ibm/granite-4-h-tiny", _SAMPLE)
    assert _max_loaded_context(insts) == 8192


def test_max_loaded_context_ignores_not_loaded():
    insts = find_instances("hermes-2-pro-mistral-7b", _SAMPLE)
    assert _max_loaded_context(insts) == 0


def test_max_loaded_context_zero_when_no_instances():
    assert _max_loaded_context([]) == 0


# --- _run_lms: bounded, self-cleaning subprocess plumbing -------------------

def test_run_lms_returns_stdout_on_success():
    assert _run_lms(["printf", "hello world"]) == "hello world"
    assert not lm_studio._LIVE_LMS_PROCS  # untracked once it completes


def test_run_lms_raises_on_nonzero_exit():
    with pytest.raises(subprocess.CalledProcessError):
        _run_lms(["sh", "-c", "exit 3"])
    assert not lm_studio._LIVE_LMS_PROCS


def test_run_lms_times_out_and_is_reaped():
    # A wedged call must raise rather than block forever, and leave nothing
    # tracked behind (the runaway-orphan we were burned by).
    with pytest.raises(subprocess.TimeoutExpired):
        _run_lms(["sleep", "5"], timeout=0.2)
    assert not lm_studio._LIVE_LMS_PROCS


def test_run_lms_timeout_kills_whole_process_group(tmp_path):
    # `lms` may spawn its own children; a timeout must take down the entire
    # group, not just the direct child (which is all subprocess's own timeout
    # handling reaps). Background a grandchild, record its pid, and assert the
    # timeout kills it too.
    pidfile = tmp_path / "grandchild.pid"
    script = f"sleep 30 & echo $! > {pidfile}; wait"
    with pytest.raises(subprocess.TimeoutExpired):
        _run_lms(["sh", "-c", script], timeout=1.0)
    gc_pid = int(pidfile.read_text())
    for _ in range(30):  # give the group SIGKILL a moment to land
        try:
            os.kill(gc_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    with pytest.raises(ProcessLookupError):
        os.kill(gc_pid, 0)  # grandchild is gone — the whole group died


def test_cleanup_lms_procs_kills_tracked_children():
    # The atexit hook must take down any keepalive still running at shutdown.
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    lm_studio._LIVE_LMS_PROCS.add(proc)
    try:
        lm_studio._cleanup_lms_procs()
        proc.wait(timeout=3)
        assert proc.returncode is not None
        assert not lm_studio._LIVE_LMS_PROCS
    finally:
        if proc.poll() is None:
            proc.kill()
        lm_studio._LIVE_LMS_PROCS.discard(proc)
