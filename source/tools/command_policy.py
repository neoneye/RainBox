"""Command parsing + allowlist + per-command validators.

A message becomes an explicit argv via `shlex.split` (no shell, so `|`, `>`,
`$()`, `;`, `&&`, `*`, `$VAR` carry no special meaning — they are rejected for
clarity). The program must be in ALLOWED_COMMANDS, and each command runs through
its own validator (options + path arguments), so this is not a single generic
check. Path arguments are confined by `workspace_policy.resolve_workspace_path`.

The allowlist deliberately excludes interpreters (python/bash/…), filesystem
mutation tools (rm/mv/tee/…), and network tools (curl/ssh/…).
"""

import re
import shlex
from pathlib import Path
from typing import Callable

from .workspace_policy import DisallowedCommand, SHELL_ROOT, resolve_workspace_path

ALLOWED_COMMANDS: frozenset[str] = frozenset(
    {"ls", "pwd", "cd", "cat", "head", "tail", "grep", "wc", "date", "find", "stat", "file"}
)

# Shell operators we reject outright (they are literal argv with shell=False, but
# rejecting avoids user confusion about why `ls | wc` "doesn't work").
SHELL_TOKENS: frozenset[str] = frozenset(
    {"|", "||", "&", "&&", ";", ">", ">>", "<", "<<", "<<<", "2>", "2>>"}
)


def parse_argv(command: str) -> list[str]:
    """Split a message into argv with POSIX rules. Rejects unparseable input
    (e.g. an unbalanced quote) and the empty command."""
    try:
        argv = shlex.split(command, posix=True)
    except ValueError as e:
        raise DisallowedCommand(f"could not parse command: {e}") from e
    if not argv:
        raise DisallowedCommand("empty command")
    return argv


def reject_shell_syntax(argv: list[str]) -> None:
    """Reject shell-looking tokens so the no-shell model is unambiguous."""
    for arg in argv:
        if arg in SHELL_TOKENS:
            raise DisallowedCommand(f"shell operator not supported: {arg!r}")
        if "$(" in arg or "`" in arg:
            raise DisallowedCommand("command substitution not supported")
        if "$" in arg:
            raise DisallowedCommand("shell variables not supported")
        if "\n" in arg or "\r" in arg:
            raise DisallowedCommand("newlines not allowed in arguments")


# --- Command-specific validators ---------------------------------------------


def validate_pwd(argv: list[str], cwd: str) -> None:
    if len(argv) != 1:
        raise DisallowedCommand("pwd does not accept arguments")


def validate_date(argv: list[str], cwd: str) -> None:
    if len(argv) != 1:
        raise DisallowedCommand("date does not accept arguments")


def validate_cd(argv: list[str], cwd: str) -> None:
    if len(argv) != 2:
        raise DisallowedCommand("cd requires exactly one path")
    value = argv[1]
    if value in {"-", "~"} or value.startswith("~"):
        raise DisallowedCommand(f"cd target not allowed: {value!r}")
    resolve_workspace_path(value, cwd)


def validate_read_files(argv: list[str], cwd: str) -> None:
    """cat / stat / file: one or more in-workspace paths, no options."""
    if len(argv) < 2:
        raise DisallowedCommand(f"{argv[0]} requires at least one path")
    for arg in argv[1:]:
        if arg.startswith("-"):
            raise DisallowedCommand(f"{argv[0]} options not allowed: {arg}")
        resolve_workspace_path(arg, cwd)


def validate_head_tail(argv: list[str], cwd: str) -> None:
    program = argv[0]
    args = argv[1:]
    if not args:
        raise DisallowedCommand(f"{program} requires a path")
    i = 0
    if args[i] == "-n":
        if i + 1 >= len(args):
            raise DisallowedCommand(f"{program} -n requires a number")
        if not args[i + 1].isdigit():
            raise DisallowedCommand(f"{program} -n value must be numeric")
        if int(args[i + 1]) > 1000:
            raise DisallowedCommand(f"{program} -n too large")
        i += 2
    elif args[i].startswith("-n"):
        n = args[i][2:]
        if not n.isdigit():
            raise DisallowedCommand(f"{program} -n value must be numeric")
        if int(n) > 1000:
            raise DisallowedCommand(f"{program} -n too large")
        i += 1
    for arg in args[i:]:
        if arg.startswith("-"):
            raise DisallowedCommand(f"{program} option not allowed: {arg}")
        resolve_workspace_path(arg, cwd)


def validate_wc(argv: list[str], cwd: str) -> None:
    allowed_opts = {"-l", "-c", "-w", "-m"}
    args = argv[1:]
    if not args:
        raise DisallowedCommand("wc requires a path")
    paths = []
    for arg in args:
        if arg.startswith("-"):
            if arg not in allowed_opts:
                raise DisallowedCommand(f"wc option not allowed: {arg}")
        else:
            paths.append(arg)
    if not paths:
        raise DisallowedCommand("wc requires at least one path")
    for path in paths:
        resolve_workspace_path(path, cwd)


def validate_ls(argv: list[str], cwd: str) -> None:
    allowed_opts = {
        "-l", "-a", "-la", "-al",
        "-h", "-lh", "-hl",
        "-t", "-lt", "-tl",
        "-R",
    }
    paths = []
    for arg in argv[1:]:
        if arg.startswith("-"):
            if arg not in allowed_opts:
                raise DisallowedCommand(f"ls option not allowed: {arg}")
        else:
            paths.append(arg)
    for path in paths:
        resolve_workspace_path(path, cwd)


def validate_grep(argv: list[str], cwd: str) -> None:
    allowed_opts = {"-n", "-i", "-R", "-r", "-H"}
    args = argv[1:]
    if len(args) < 2:
        raise DisallowedCommand("grep requires a pattern and at least one path")
    i = 0
    recursive = False
    while i < len(args) and args[i].startswith("-"):
        opt = args[i]
        if opt not in allowed_opts:
            raise DisallowedCommand(f"grep option not allowed: {opt}")
        if opt in {"-R", "-r"}:
            recursive = True
        i += 1
    if i >= len(args):
        raise DisallowedCommand("grep requires a pattern")
    pattern = args[i]
    i += 1
    if pattern.startswith("-"):
        raise DisallowedCommand("grep pattern must not start with '-'")
    paths = args[i:]
    if not paths:
        raise DisallowedCommand("grep requires at least one path")
    for path in paths:
        resolved = resolve_workspace_path(path, cwd)
        # A recursive grep at the workspace root is too broad: huge output and an
        # easy way to stumble across secrets. Recurse a subdirectory instead.
        if recursive and resolved == SHELL_ROOT:
            raise DisallowedCommand("recursive grep at workspace root is too broad")


FIND_FORBIDDEN_OPTIONS: frozenset[str] = frozenset(
    {
        "-delete",
        "-exec",
        "-execdir",
        "-ok",
        "-okdir",
        "-printf",
        "-fprintf",
        "-fprint",
        "-fprint0",
        "-fls",
        "-ls",
    }
)
FIND_ALLOWED_OPTIONS: frozenset[str] = frozenset(
    {"-maxdepth", "-mindepth", "-type", "-name", "-iname", "-size", "-mtime"}
)


def validate_find(argv: list[str], cwd: str) -> None:
    args = argv[1:]
    if not args:
        raise DisallowedCommand("find requires an explicit start path")
    start = args[0]
    if Path(start).is_absolute():
        raise DisallowedCommand("find absolute start paths not allowed")
    resolve_workspace_path(start, cwd)
    i = 1
    while i < len(args):
        token = args[i]
        if token in FIND_FORBIDDEN_OPTIONS:
            raise DisallowedCommand(f"find option not allowed: {token}")
        if token not in FIND_ALLOWED_OPTIONS:
            raise DisallowedCommand(f"find token not allowed: {token!r}")
        if i + 1 >= len(args):
            raise DisallowedCommand(f"find option requires value: {token}")
        value = args[i + 1]
        if token in {"-maxdepth", "-mindepth"}:
            if not value.isdigit():
                raise DisallowedCommand(f"find depth must be numeric: {value!r}")
            if int(value) > 8:
                raise DisallowedCommand("find depth too large")
        elif token == "-type":
            if value not in {"f", "d"}:
                raise DisallowedCommand(f"find type not allowed: {value!r}")
        elif token in {"-name", "-iname"}:
            if "/" in value or "\x00" in value:
                raise DisallowedCommand(f"find name pattern not allowed: {value!r}")
            if value in {"*", ".*"}:
                raise DisallowedCommand("find name pattern too broad")
        elif token == "-size":
            if not re.fullmatch(r"[+-]?\d+[ckMG]?", value):
                raise DisallowedCommand(f"find size value not allowed: {value!r}")
        elif token == "-mtime":
            if not re.fullmatch(r"[+-]?\d+", value):
                raise DisallowedCommand(f"find mtime value not allowed: {value!r}")
        i += 2


_VALIDATORS: dict[str, Callable[[list[str], str], None]] = {
    "ls": validate_ls,
    "pwd": validate_pwd,
    "cd": validate_cd,
    "cat": validate_read_files,
    "head": validate_head_tail,
    "tail": validate_head_tail,
    "grep": validate_grep,
    "wc": validate_wc,
    "date": validate_date,
    "find": validate_find,
    "stat": validate_read_files,
    "file": validate_read_files,
}


def validate_command(command: str, cwd: str) -> list[str]:
    """Parse `command` into argv and run its command-specific validator against
    `cwd`. Returns the validated argv. Raises DisallowedCommand on any policy
    violation (unknown command, disallowed option, path escape, …)."""
    argv = parse_argv(command)
    reject_shell_syntax(argv)
    program = argv[0]
    if program not in ALLOWED_COMMANDS:
        raise DisallowedCommand(f"command not allowed: {program!r}")
    _VALIDATORS[program](argv, cwd)
    return argv
