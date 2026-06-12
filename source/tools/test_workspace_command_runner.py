"""Tests for tools/workspace_command_runner.py — deterministic argv execution.

Uses the `workspace` fixture from conftest.py (real files in SHELL_CWD).
"""

import pytest

from tools import workspace_command_runner
from tools.command_policy import validate_command
from tools.workspace_command_runner import (
    SHELL_ENV,
    CommandTimeout,
    run_command_once,
)
from tools.workspace_policy import SHELL_CWD, DisallowedCommand


def test_exec_cat_reads_file(workspace):
    root, make_file, _ = workspace
    make_file("README.md", "hello workspace")
    argv = validate_command("cat README.md", str(root))
    r = run_command_once(argv, str(root), dict(SHELL_ENV))
    assert r.output == "hello workspace"
    assert r.exit_code == 0


def test_exec_pwd_builtin():
    argv = validate_command("pwd", SHELL_CWD)
    r = run_command_once(argv, SHELL_CWD, dict(SHELL_ENV))
    assert r.output == SHELL_CWD
    assert r.exit_code == 0


def test_exec_wc_lines(workspace):
    root, make_file, _ = workspace
    make_file("lines.txt", "a\nb\nc\n")
    argv = validate_command("wc -l lines.txt", str(root))
    r = run_command_once(argv, str(root), dict(SHELL_ENV))
    assert r.exit_code == 0
    assert "3" in r.output


def test_exec_cd_persists_across_calls(workspace):
    root, _, make_dir = workspace
    make_dir("sub")
    argv1 = validate_command("cd sub", str(root))
    r1 = run_command_once(argv1, str(root), dict(SHELL_ENV))
    assert r1.exit_code == 0
    assert r1.cwd.endswith("/sub")
    argv2 = validate_command("pwd", r1.cwd)
    r2 = run_command_once(argv2, r1.cwd, dict(SHELL_ENV))
    assert r2.output == r1.cwd


def test_exec_cd_missing_dir_is_error(workspace):
    root, _, _ = workspace
    argv = validate_command("cd nope_xyz", str(root))
    r = run_command_once(argv, str(root), dict(SHELL_ENV))
    assert r.exit_code == 1
    assert "no such file" in r.output


def test_exec_no_glob_expansion(workspace):
    # shell=False: *.py is a literal argv string, NOT a glob, so cat tries to
    # open a file literally named "*.py" and fails — it must NOT print a.py/b.py.
    root, make_file, _ = workspace
    make_file("a.py", "AAA")
    make_file("b.py", "BBB")
    argv = validate_command("cat *.py", str(root))
    assert argv == ["cat", "*.py"]
    r = run_command_once(argv, str(root), dict(SHELL_ENV))
    assert r.exit_code != 0
    assert "AAA" not in r.output and "BBB" not in r.output


def test_exec_truncates_large_output(workspace):
    root, make_file, _ = workspace
    make_file("big.txt", "a" * 10000)
    argv = validate_command("cat big.txt", str(root))
    r = run_command_once(argv, str(root), dict(SHELL_ENV))
    assert "truncated" in r.output
    assert len(r.output) < 9000


def test_exec_missing_binary_raises():
    # Bypasses validate_command (no allowed program is missing); checks the
    # FileNotFoundError -> DisallowedCommand mapping in run_command_once.
    with pytest.raises(DisallowedCommand):
        run_command_once(["definitely_not_a_real_binary_xyz"], SHELL_CWD, dict(SHELL_ENV))


def test_exec_timeout(monkeypatch):
    monkeypatch.setattr(workspace_command_runner, "COMMAND_TIMEOUT", 0.3)
    with pytest.raises(CommandTimeout):
        run_command_once(["sleep", "5"], SHELL_CWD, dict(SHELL_ENV))
