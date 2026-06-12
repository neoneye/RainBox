"""Tests for tools/command_policy.py — parsing, allowlist, per-command validators."""

import pytest

from tools.command_policy import parse_argv, validate_command
from tools.workspace_policy import SHELL_CWD, DisallowedCommand


@pytest.mark.parametrize(
    "command",
    [
        "pwd",
        "ls",
        "ls -la",
        "cd .",
        "cd subdir",
        "cat README.md",
        "head -n 50 README.md",
        "tail -n 50 README.md",
        "wc -l README.md",
        "grep -n TODO README.md",
        "grep -R -n TODO subdir",  # recursive but not at workspace root
        "find . -maxdepth 3 -type f",
        'find . -name "*.py"',
        "date",
    ],
)
def test_validate_allows(command: str):
    validate_command(command, SHELL_CWD)  # must not raise


@pytest.mark.parametrize(
    "command",
    [
        "cat .env",
        "cat ~/.ssh/id_rsa",
        "cat /etc/passwd",
        "cd /",
        "cd ~",
        "cd -",
        "echo hello",
        "echo hello > file.txt",
        "cat README.md > copy.txt",
        "grep TODO $(cat secret)",
        "ls ; cat .env",
        "cd src && ls",
        "grep TODO README.md | head",
        "grep -R TODO .",
        "find . -delete",
        "find . -exec rm {} \\;",
        "find . -ok rm {} \\;",
        "find / -type f",
        'find . -printf "%p\\n"',
        "find . -fprint out.txt",
        "tail -f README.md",
        'python -c "print(123)"',
        'bash -c "ls"',
        'sh -c "ls"',
        "ls $HOME",
        "cat ../../../etc/passwd",
    ],
)
def test_validate_blocks(command: str):
    with pytest.raises(DisallowedCommand):
        validate_command(command, SHELL_CWD)


def test_parse_argv_rejects_unbalanced_quote():
    with pytest.raises(DisallowedCommand):
        parse_argv('cat "oops')


def test_parse_argv_rejects_empty():
    with pytest.raises(DisallowedCommand):
        parse_argv("   ")


def test_validate_returns_argv_without_glob_expansion():
    # validate_command never expands globs: *.py stays a single literal argv,
    # confirming there is no shell in the parse path.
    argv = validate_command("cat *.py", SHELL_CWD)
    assert argv == ["cat", "*.py"]
