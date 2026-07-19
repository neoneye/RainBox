"""Run small Python programs in a Pyodide (WebAssembly) sandbox.

`run_python(code)` spawns a fresh `node runner.mjs` per job (clean state, ~1.5s
startup) and enforces resource limits from out here, where the sandbox can't
interfere:

- CPU: RLIMIT_CPU on the child — the kernel kills it after `cpu_seconds` of
  actual CPU time (i.e. "100% CPU for longer than 30s").
- Memory: Pyodide needs ~220 MB RSS just to load, so the budget is growth
  *above* the baseline sampled when the runner prints READY; the child's RSS is
  polled via `ps` and the whole process group is SIGKILLed past baseline+budget.
- Wall clock: catches sleeps/deadlocks that consume no CPU.

What the sandbox blocks (see runner.mjs for the how): package installs beyond
the curated allowlist (numpy/sympy/mpmath, preloaded only when imported),
network, the host filesystem, and the host environment — the child also gets a
minimal env (PATH+LANG, no secrets) as defense in depth.
"""

import json
import os
import resource
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

SANDBOX_DIR = Path(__file__).resolve().parent
RUNNER = SANDBOX_DIR / "runner.mjs"
PYODIDE_DIR = SANDBOX_DIR / "node_modules" / "pyodide"

CPU_SECONDS = 30
WALL_SECONDS = 60.0
MEMORY_BUDGET_MB = 100
POLL_INTERVAL_SECONDS = 0.2
MAX_CAPTURE_CHARS = 10000


class SandboxUnavailable(Exception):
    """The sandbox cannot run at all (missing node / missing npm install)."""


@dataclass
class SandboxResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    result_repr: str | None = None  # repr of a trailing expression, if any
    error: str | None = None        # traceback or "killed: ..." limit message
    duration_seconds: float = 0.0


def _truncate(text: str, limit: int = MAX_CAPTURE_CHARS) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    dropped = len(text) - 2 * half
    return f"{text[:half]}\n...[truncated {dropped} chars]...\n{text[-half:]}"


def _rss_mb(pid: int) -> float | None:
    """Resident set size of `pid` in MB via `ps` (portable, no psutil)."""
    try:
        out = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        )
        return int(out.stdout.strip()) / 1024.0
    except (ValueError, subprocess.TimeoutExpired, OSError):
        return None


def run_python(
    code: str,
    *,
    cpu_seconds: int = CPU_SECONDS,
    wall_seconds: float = WALL_SECONDS,
    memory_budget_mb: float = MEMORY_BUDGET_MB,
) -> SandboxResult:
    """Run one Python program in a fresh sandbox and return its outcome.

    Never raises for anything the program itself does — errors and limit kills
    come back in `SandboxResult.error`. Raises SandboxUnavailable only when the
    sandbox itself is missing (no node, or npm install not run).
    """
    node = shutil.which("node")
    if node is None:
        raise SandboxUnavailable("node not found on PATH — install Node.js")
    if not PYODIDE_DIR.is_dir():
        raise SandboxUnavailable(f"pyodide not installed — run `npm install` in {SANDBOX_DIR}")

    def set_limits() -> None:
        # Hard ceiling a little above soft so SIGXCPU (soft) is what we see.
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 5))

    start = time.monotonic()
    proc = subprocess.Popen(
        [node, str(RUNNER)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=SANDBOX_DIR,
        env={
            "PATH": f"{os.path.dirname(node)}:/usr/bin:/bin",
            "LANG": "C.UTF-8",
        },
        text=True,
        errors="replace",
        start_new_session=True,  # own process group: killpg reaps node + helpers
        preexec_fn=set_limits,
    )

    stdout_lines: list[str] = []
    stderr_chunks: list[str] = []
    ready = threading.Event()

    def read_stdout() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            if line.strip() == "READY":
                ready.set()
            else:
                stdout_lines.append(line)

    def read_stderr() -> None:
        assert proc.stderr is not None
        stderr_chunks.append(proc.stderr.read())

    threads = [threading.Thread(target=read_stdout, daemon=True),
               threading.Thread(target=read_stderr, daemon=True)]
    for t in threads:
        t.start()

    assert proc.stdin is not None
    try:
        proc.stdin.write(json.dumps({"code": code}))
        proc.stdin.close()
    except BrokenPipeError:
        pass  # child died at startup; the poll loop reports it

    kill_reason: str | None = None
    baseline_mb: float | None = None
    while proc.poll() is None:
        elapsed = time.monotonic() - start
        if elapsed > wall_seconds:
            kill_reason = f"exceeded {wall_seconds:g}s wall clock"
        elif ready.is_set():
            rss = _rss_mb(proc.pid)
            if rss is not None:
                if baseline_mb is None:
                    baseline_mb = rss
                elif rss - baseline_mb > memory_budget_mb:
                    kill_reason = f"exceeded {memory_budget_mb:g} MB memory budget"
        if kill_reason is not None:
            os.killpg(proc.pid, signal.SIGKILL)
            break
        time.sleep(POLL_INTERVAL_SECONDS)

    proc.wait()
    for t in threads:
        t.join(timeout=5)
    duration = time.monotonic() - start

    if kill_reason is not None:
        return SandboxResult(ok=False, error=f"killed: {kill_reason}", duration_seconds=duration)
    if proc.returncode == -signal.SIGXCPU:
        return SandboxResult(
            ok=False, error=f"killed: exceeded {cpu_seconds}s CPU", duration_seconds=duration
        )

    result_line = next(
        (l for l in reversed(stdout_lines) if l.startswith("RESULT ")), None
    )
    if result_line is None:
        stderr_tail = _truncate("".join(stderr_chunks).strip(), 2000)
        return SandboxResult(
            ok=False,
            error=f"sandbox crashed (exit code {proc.returncode})"
                  + (f":\n{stderr_tail}" if stderr_tail else ""),
            duration_seconds=duration,
        )

    payload = json.loads(result_line[len("RESULT "):])
    error = payload.get("error")
    return SandboxResult(
        ok=error is None,
        stdout=_truncate(payload.get("stdout") or ""),
        stderr=_truncate(payload.get("stderr") or ""),
        result_repr=payload.get("result_repr"),
        error=error,
        duration_seconds=duration,
    )
