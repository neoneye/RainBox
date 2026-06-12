"""Workspace shell tools — a no-LLM, no-`bash` command runner, split by concern.

The package is organized into focused modules:

- `workspace_policy`         — path confinement to a workspace + sensitive-file
                               blocking (and the shared `DisallowedCommand`).
- `command_policy`           — argv parsing, the command allowlist, and the
                               per-command validators.
- `workspace_command_runner` — deterministic execution via
                               `subprocess.run(shell=False)` + `cd`/`pwd` builtins.
- `workspace_shell_chat`     — the chatroom agent that wires the three together.

Dependency direction is strictly one way:
  workspace_policy <- command_policy
  workspace_policy <- workspace_command_runner
  (command_policy, workspace_command_runner) <- workspace_shell_chat
"""
