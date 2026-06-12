"""Loader for `mcp.json` (Claude-Desktop-style MCP server config).

Supports two transports per server:
- stdio: `{"command": "...", "args": [...]}` — spawned as a subprocess.
- HTTP/SSE: `{"url": "https://...", "headers": {...}}` — fetched live.

Bare command names like `"python"` are rewritten to `sys.executable`,
which is the venv's interpreter (the one with `mcp` installed). `*.py`
arguments are resolved relative to the repo dir so the spawn works
regardless of the agent's current working directory. URL entries pass
through unchanged; headers are forwarded to the MCP client verbatim.

The operator overlay `<customize.dir>/mcp.json` (the same setting the Q&A
overlay uses) merges over the base file per server name — an overlay entry
replaces the base entry wholesale. Private servers (API keys!) belong in
the overlay; the base file stays publishable. Servers are re-read on every
agent spawn, so overlay edits need no restart and no repopulate step.
"""

import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

_REPO_DIR = Path(__file__).resolve().parent
_CONFIG_PATH = _REPO_DIR / "mcp.json"


@dataclass(frozen=True)
class ServerSpec:
    """A configured MCP server. Exactly one of `command` (stdio) or
    `url` (HTTP/SSE) is set."""
    name: str
    command: str | None = None
    args: list[str] = field(default_factory=list)
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


def _resolve_command(command: str) -> str:
    """A bare `python` / `python3` is rewritten to `sys.executable` so
    spawned servers run with the same interpreter (and thus the same
    `mcp` install) as the caller. Path-looking commands are resolved
    relative to the repo dir."""
    if command in ("python", "python3"):
        return sys.executable
    if "/" in command or command.startswith("."):
        cmd_path = Path(command)
        if not cmd_path.is_absolute():
            cmd_path = (_REPO_DIR / cmd_path).resolve()
        return str(cmd_path)
    return command


def _resolve_arg(arg: str) -> str:
    """Resolve `*.py` args relative to the repo root. Other args pass
    through verbatim (regular CLI flags, literal values)."""
    if not arg.endswith(".py"):
        return arg
    if Path(arg).is_absolute():
        return arg
    return str((_REPO_DIR / arg).resolve())


def _overlay_config_path() -> Path | None:
    """<customize.dir>/mcp.json, or None when the setting is unset OR
    unresolvable. Resolution needs the DB (db.get_setting → app context);
    this module must also work standalone (tests, scripts), so ANY lookup
    failure degrades to base-only with a debug log instead of raising."""
    try:
        import db

        value = db.get_setting("customize.dir")
    except Exception as exc:  # noqa: BLE001 — no app ctx / DB down → no overlay
        logging.getLogger(__name__).debug(
            "customize.dir unresolvable (%s); using base mcp.json only", exc)
        return None
    if not value:
        return None
    return Path(str(value)) / "mcp.json"


def load_mcp_servers(
    path: Path | None = None, overlay_path: Path | None = None,
) -> list[ServerSpec]:
    """Parse `mcp.json` (+ the customize.dir overlay) into ServerSpec
    objects. The two `servers` maps merge by server name, overlay winning
    WHOLESALE per entry. Returns [] when neither file exists. `path` /
    `overlay_path` are for tests; production callers pass neither (base =
    _CONFIG_PATH, overlay resolved from the customize.dir setting — see
    _overlay_config_path for the degrade story).

    A server entry must have either `command` (stdio) or `url` (HTTP);
    entries with neither are silently skipped."""
    config_path = path if path is not None else _CONFIG_PATH
    if overlay_path is None:
        overlay_path = _overlay_config_path()
    servers_raw: dict = {}
    for p in (config_path, overlay_path):
        if p is not None and p.is_file():
            servers_raw.update(json.loads(p.read_text()).get("servers") or {})
    out: list[ServerSpec] = []
    for name, spec in servers_raw.items():
        url = spec.get("url")
        command = spec.get("command")
        if url:
            out.append(ServerSpec(
                name=name,
                url=url,
                headers=dict(spec.get("headers") or {}),
            ))
        elif command:
            out.append(ServerSpec(
                name=name,
                command=_resolve_command(command),
                args=[_resolve_arg(a) for a in (spec.get("args") or [])],
            ))
    return out
