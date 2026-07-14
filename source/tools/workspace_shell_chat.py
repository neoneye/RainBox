"""The chatroom agent that wires the workspace shell tools together.

WorkspaceShellChatAgent is a no-LLM agent that runs the triggering human message
as a validated, non-shell argv (parsed with shlex, executed with
subprocess.run(shell=False)) in the room's persisted working directory and posts
the raw output back. It owns no policy or execution logic itself — it composes
`command_policy.validate_command` and `workspace_command_runner.run_command_once`,
and persists only the cwd in the `workspace_shell_state` table (the env is always the
fixed SHELL_ENV baseline).

It is registered as the `workspace_shell` role (agent_config + the supervisor
registry), so it is selectable as a room member on /chat.
"""

import logging
import shlex
from typing import Any
from uuid import UUID

import db
from agents.base import Agent

from .command_policy import validate_command
from .workspace_command_runner import (
    COMMAND_TIMEOUT,
    SHELL_ENV,
    CommandTimeout,
    ExecResult,
    run_command_once,
)
from .workspace_policy import DisallowedCommand, SHELL_CWD

logger = logging.getLogger(__name__)


class WorkspaceShellChatAgent(Agent):
    """A no-LLM chatroom agent. Runs the triggering human message as an explicit
    argv (validated per-command, executed with shell=False) in the room's
    persisted cwd, and posts the raw output back into the room."""

    # No LLM at all, so an /agentmodel binding would be dead weight; keep
    # this agent off that page.
    uses_model_group = False

    @staticmethod
    def _room_uuid(payload: dict[str, Any]) -> UUID:
        raw = payload.get("room_uuid")
        if not raw:
            raise ValueError("workspace shell agent payload missing 'room_uuid'")
        return raw if isinstance(raw, UUID) else UUID(str(raw))

    @staticmethod
    def _command_from_payload(room_uuid: UUID, payload: dict[str, Any]) -> str | None:
        """The command to run: the specific triggering message named by the
        payload's 'message_uuid' (so rapid successive posts each run their own
        command, not whichever is newest). Falls back to the latest human message
        when no message_uuid is given (e.g. a manual enqueue).

        A programmatic caller (e.g. the cron scheduler) may pass the command
        directly as 'command_text', bypassing the human-message lookup — this is
        an explicit, code-driven path, not a chat post."""
        direct = payload.get("command_text")
        if direct:
            return str(direct).strip()
        msgs = db.list_room_messages(room_uuid)
        msg_uuid = payload.get("message_uuid")
        if msg_uuid:
            for m in msgs:
                if m.get("uuid") == str(msg_uuid) and m.get("sender_type") == "human":
                    return (m.get("text") or "").strip()
            return None
        for m in reversed(msgs):
            if m.get("sender_type") == "human":
                return (m.get("text") or "").strip()
        return None

    @staticmethod
    def _format_reply(command: str, result: ExecResult) -> str:
        # Four-backtick fence so command output containing ``` can't close it early.
        return f"````\n$ {command}\n{result.output}\n[exit code: {result.exit_code}]\n````"

    def _handle_kanban_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Execute a kanban task: the task's DESCRIPTION is the command (the
        cron pattern — command in the text field), validated and run exactly
        like a chat-triggered one (argv, no shell, workspace-confined, default
        cwd — kanban tasks are room-less so no per-room state is touched).
        Output lands in the task's event trail; ok = exit 0."""
        from uuid import UUID as _UUID

        import db as _db

        from .kanban_runner import run_kanban_task

        def work(task: dict[str, Any], _context_md: str) -> tuple[bool, str]:
            command = (task.get("description") or "").strip()
            if not command:
                return False, "no command (the task description is empty)"
            try:
                argv = validate_command(command, SHELL_CWD)
            except DisallowedCommand as e:
                return False, f"blocked: {e}"
            try:
                result = run_command_once(argv, SHELL_CWD, dict(SHELL_ENV))
            except CommandTimeout:
                return False, f"timed out after {COMMAND_TIMEOUT:g}s"
            except DisallowedCommand as e:
                return False, f"blocked: {e}"
            _db.kanban_append_event(
                _UUID(task["uuid"]), "progress", actor=str(self.agent_uuid),
                detail=f"$ {command}\n{result.output[:2000]}"
                       f"\n[exit code: {result.exit_code}]")
            ok = result.exit_code == 0
            return ok, "" if ok else f"exit code {result.exit_code}"

        return run_kanban_task(self.agent_uuid, payload, work)

    def _post_blocked(self, room_uuid: UUID, reason: str) -> dict[str, Any]:
        posted = db.post_chat_message(
            room_uuid, self.agent_uuid, f"[blocked: {reason}]", "markdown"
        )
        return {"ok": True, "blocked": reason, "posted_message_uuid": str(posted.uuid)}

    @staticmethod
    def _record_cron_outcome(
        journal_id: UUID, payload: dict[str, Any], status: str, error: str = ""
    ) -> None:
        """A cron-fired command carries 'cron_run_uuid'; write the outcome back
        onto that CronRun row (status/error/journal link) so the run log shows
        how the fire actually ended. A chat-triggered run has no run row → no-op."""
        run_uuid = payload.get("cron_run_uuid")
        if run_uuid:
            db.cron_record_run_outcome(
                run_uuid, status=status, error=error, journal_id=journal_id
            )

    def handle(self, journal_id: UUID, payload: dict[str, Any]) -> dict[str, Any]:
        # Kanban execution (milestone 3): a task enqueued for this agent runs
        # its DESCRIPTION as the command, via the shared kanban adapter
        # (claim → events → complete). Checked before room resolution — kanban
        # payloads carry no room.
        if payload.get("source") == "kanban":
            return self._handle_kanban_task(payload)
        room_uuid = self._room_uuid(payload)
        command = self._command_from_payload(room_uuid, payload)
        if not command:
            logger.info("workspace shell: no human command in room %s; nothing to do", room_uuid)
            self._record_cron_outcome(journal_id, payload, "error", "no command to run")
            return {"ok": True, "skipped": "no human command"}

        state = db.get_workspace_shell_state(room_uuid)
        cwd = state.cwd if state else SHELL_CWD
        # Only cwd persists; the environment always starts from the fixed baseline.
        env = dict(SHELL_ENV)

        try:
            argv = validate_command(command, cwd)
        except DisallowedCommand as e:
            self._record_cron_outcome(journal_id, payload, "error", f"blocked: {e}")
            return self._post_blocked(room_uuid, str(e))

        if payload.get("debug"):
            # Dry-run (cron "Run debug"): the command validated — echo the argv
            # it WOULD execute (in which cwd) and stop. Nothing runs, the
            # persisted cwd is untouched.
            reply = f"[debug] would run in {cwd}: {shlex.join(argv)}"
            posted = db.post_chat_message(room_uuid, self.agent_uuid, reply, "markdown")
            self._record_cron_outcome(journal_id, payload, "ok")
            return {"ok": True, "debug": True, "posted_message_uuid": str(posted.uuid)}

        try:
            result = run_command_once(argv, cwd, env)
        except CommandTimeout:
            reply = f"[timed out after {COMMAND_TIMEOUT:g}s]"
            posted = db.post_chat_message(room_uuid, self.agent_uuid, reply, "markdown")
            self._record_cron_outcome(
                journal_id, payload, "error", f"timed out after {COMMAND_TIMEOUT:g}s"
            )
            return {"ok": True, "timed_out": True, "posted_message_uuid": str(posted.uuid)}
        except DisallowedCommand as e:
            self._record_cron_outcome(journal_id, payload, "error", f"blocked: {e}")
            return self._post_blocked(room_uuid, str(e))

        db.set_workspace_shell_state(room_uuid, result.cwd, env)
        posted = db.post_chat_message(
            room_uuid, self.agent_uuid, self._format_reply(command, result), "markdown"
        )
        logger.info(
            "workspace shell ran %r in room %s (exit %d)", command, room_uuid, result.exit_code
        )
        # Cron semantics: a non-zero exit is a failed run.
        self._record_cron_outcome(
            journal_id, payload,
            "ok" if result.exit_code == 0 else "error",
            "" if result.exit_code == 0 else f"exit code {result.exit_code}",
        )
        return {
            "ok": True,
            "exit_code": result.exit_code,
            "posted_message_uuid": str(posted.uuid),
        }
