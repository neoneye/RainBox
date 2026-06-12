"""Deterministic execution of a validated argv — no shell, no interpretation.

`run_command_once` runs a (already validated) argv with
`subprocess.run(argv, shell=False, ...)` and returns its combined output, exit
code, and resulting cwd. `cd`/`pwd` are handled in Python (a subprocess `cd`
can't change the parent's cwd, and `pwd` is trivial), everything else is a real
non-shell subprocess. Only the working directory persists between calls; the
environment is always the fixed SHELL_ENV baseline.
"""

import os
import subprocess
from dataclasses import dataclass

from .workspace_policy import DisallowedCommand, SHELL_CWD, resolve_workspace_path

# Fixed environment (NOT inherited from os.environ) so the runner never sees host
# secrets. HOME points at the workspace.
SHELL_ENV: dict[str, str] = {
    "PATH": "/usr/bin:/bin:/usr/local/bin",
    "HOME": SHELL_CWD,
    "LANG": "C.UTF-8",
}
COMMAND_TIMEOUT: float = 10.0
MAX_OUTPUT_CHARS: int = 4000


class CommandTimeout(Exception):
    """Raised by run_command_once when a command exceeds COMMAND_TIMEOUT."""


def _truncate(text: str, limit: int) -> str:
    """Cap text at `limit` chars, keeping a head and tail with an elision note."""
    if len(text) <= limit:
        return text
    half = limit // 2
    dropped = len(text) - 2 * half
    return f"{text[:half]}\n...[truncated {dropped} chars]...\n{text[-half:]}"


@dataclass
class ExecResult:
    output: str      # combined stdout+stderr (truncated), trailing newline trimmed
    exit_code: int
    cwd: str         # working directory after the command (only `cd` changes it)


def run_cd(argv: list[str], cwd: str) -> ExecResult:
    """The `cd` builtin: validate the (already-confined) target exists and is a
    directory, and return it as the new cwd. Never spawns a subprocess."""
    target = resolve_workspace_path(argv[1], cwd)
    if not target.exists():
        return ExecResult(f"cd: no such file or directory: {argv[1]}", 1, cwd)
    if not target.is_dir():
        return ExecResult(f"cd: not a directory: {argv[1]}", 1, cwd)
    return ExecResult("", 0, str(target))


def run_command_once(argv: list[str], cwd: str, env: dict[str, str]) -> ExecResult:
    """Run one validated argv directly (no shell) and return its output + exit
    code and the resulting cwd. Deterministic: no interpretation. `cd`/`pwd` are
    handled in Python. Raises CommandTimeout on timeout, DisallowedCommand if the
    executable isn't found."""
    os.makedirs(SHELL_CWD, exist_ok=True)
    start_cwd = cwd if os.path.isdir(cwd) else SHELL_CWD

    if argv[0] == "cd":
        return run_cd(argv, start_cwd)
    if argv[0] == "pwd":
        return ExecResult(output=start_cwd, exit_code=0, cwd=start_cwd)

    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=start_cwd,
            env=env,
            timeout=COMMAND_TIMEOUT,
            text=True,
            errors="replace",
            shell=False,
        )
    except subprocess.TimeoutExpired as e:
        raise CommandTimeout(f"command exceeded {COMMAND_TIMEOUT}s") from e
    except FileNotFoundError as e:
        raise DisallowedCommand(f"command executable not found: {argv[0]!r}") from e

    return ExecResult(
        output=_truncate((proc.stdout or "").rstrip("\n"), MAX_OUTPUT_CHARS),
        exit_code=proc.returncode,
        cwd=start_cwd,
    )
