"""Tests for tools/workspace_policy.py — path confinement + sensitive blocking."""

import pytest

from tools.workspace_policy import (
    SHELL_CWD,
    DisallowedCommand,
    resolve_workspace_path,
)


def test_allows_in_workspace_relative():
    p = resolve_workspace_path("docs/readme.txt", SHELL_CWD)
    assert str(p).endswith("/docs/readme.txt")


def test_allows_workspace_root_itself():
    p = resolve_workspace_path(".", SHELL_CWD)
    assert str(p) == str(resolve_workspace_path("docs/..", SHELL_CWD))


@pytest.mark.parametrize(
    "value",
    [
        "../outside.txt",
        "../../etc/passwd",
        "/etc/passwd",
        "~/secret",
        "~",
    ],
)
def test_blocks_escapes_and_home(value: str):
    with pytest.raises(DisallowedCommand):
        resolve_workspace_path(value, SHELL_CWD)


@pytest.mark.parametrize(
    "value",
    [
        ".env",
        "config/.env",
        "id_rsa",
        ".ssh/known_hosts",
        ".aws/credentials",
        "sub/.gnupg/key",
    ],
)
def test_blocks_sensitive(value: str):
    with pytest.raises(DisallowedCommand):
        resolve_workspace_path(value, SHELL_CWD)


def test_blocks_nul_byte():
    with pytest.raises(DisallowedCommand):
        resolve_workspace_path("a\x00b", SHELL_CWD)
