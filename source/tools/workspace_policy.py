"""Workspace path confinement — the lowest layer of the workspace shell tools.

Every path argument an allowed command receives must resolve inside SHELL_CWD
(the workspace root). `resolve_workspace_path` also rejects home-relative paths
(`~`), NUL bytes, and a denylist of sensitive basenames/components (`.env`,
`id_rsa`, `.ssh/…`) even when they sit inside the workspace.

`DisallowedCommand` lives here because it is the shared policy-violation signal:
both this module and `command_policy` raise it, and `command_policy` depends on
this module (never the reverse), so the leaf module owns the exception.

SECURITY: this is a guardrail for a LOCAL operator on their own machine, not a
sandbox.
"""

from pathlib import Path

# The workspace the runner is confined to.
SHELL_CWD: str = "/tmp/pp_workspace_shell"
# Resolved once (handles /tmp -> /private/tmp on macOS) so confinement checks
# compare normalized, symlink-free absolute paths.
SHELL_ROOT: Path = Path(SHELL_CWD).resolve()

# Basenames / path components that are never readable even inside the workspace.
SENSITIVE_BASENAMES: frozenset[str] = frozenset(
    {
        ".env",
        ".env.local",
        ".envrc",
        "id_rsa",
        "id_ed25519",
        "credentials",
        "credentials.json",
        "secrets.json",
        ".netrc",
    }
)
SENSITIVE_PARTS: frozenset[str] = frozenset(
    {".ssh", ".aws", ".gcp", ".azure", ".kube", ".gnupg"}
)


class DisallowedCommand(Exception):
    """Raised when a command (or one of its arguments) is not permitted."""


def resolve_workspace_path(value: str, cwd: str) -> Path:
    """Resolve a path argument against `cwd` and confine it to the workspace.
    Rejects home-relative paths, NUL bytes, anything escaping SHELL_ROOT, and
    sensitive basenames/components. Returns the resolved absolute Path."""
    if "\x00" in value:
        raise DisallowedCommand("NUL byte not allowed in path")
    if value.startswith("~"):
        raise DisallowedCommand(f"home-relative paths not allowed: {value!r}")
    p = Path(value)
    base = Path(cwd).resolve()
    resolved = p.resolve() if p.is_absolute() else (base / p).resolve()
    if resolved != SHELL_ROOT and SHELL_ROOT not in resolved.parents:
        raise DisallowedCommand(f"path escapes workspace: {value!r}")
    if resolved.name in SENSITIVE_BASENAMES:
        raise DisallowedCommand(f"sensitive path blocked: {value!r}")
    if set(resolved.parts) & SENSITIVE_PARTS:
        raise DisallowedCommand(f"sensitive path blocked: {value!r}")
    return resolved
