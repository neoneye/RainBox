"""ConversationManagerAgent — the bounded turn scheduler.

The manager is an ordinary Rainbox agent (drains the inbox, journals, is
SIGKILL-bounded) that does no LLM work of its own. Each tick it loads a
`conversation_run`, checks stop conditions, picks the next speaker, and enqueues
that persona's turn with a dynamic return address pointing back at the manager.
The supervisor's routing pass re-arms the manager when the speaker completes.

See docs/proposals/2026-06-08-persona-prompts-and-agent-conversations.md
(sections "The core new primitive" and "Implementation sketch").
"""

import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import db
from agent import Agent
from agent_config import CONVERSATION_MANAGER_UUID

logger = logging.getLogger(__name__)

DEFAULT_STOP_PHRASES: tuple[str, ...] = ("DONE", "NO_REPLY")
DEFAULT_MAX_TURNS: int = 12
# How many recent visible turns a persona sees as conversation context. Bounded
# so local-model context stays small; a manager-maintained summary for older
# context is a later phase.
CONVO_LAST_N: int = 6


def next_speaker(participants: list[dict[str, Any]], turn: int) -> dict[str, Any]:
    """Deterministic round-robin pick. Pure and trivially unit-testable."""
    ordered = sorted(participants, key=lambda p: p["turn_order"])
    return ordered[turn % len(ordered)]


@dataclass
class StopDecision:
    should_stop: bool
    status: str = "finished"
    reason: str = ""
    summary: str = ""
    budget_left: int = 0


def _last_agent_message_text(messages: list[dict[str, Any]], manager_uuid: UUID) -> str:
    """Text of the most recent real `message` authored by a participant (an agent
    that is not the manager). The manager's own debug/summary rows don't count."""
    mgr = str(manager_uuid)
    for m in reversed(messages):
        if (
            m.get("kind") == "message"
            and m.get("sender_type") == "agent"
            and m.get("sender_uuid") != mgr
        ):
            return m.get("text") or ""
    return ""


def evaluate_stop(run, messages: list[dict[str, Any]], manager_uuid: UUID) -> StopDecision:
    """Pure stop-condition evaluation for one tick. Order: operator stop, then
    max_turns, then wall-clock, then stop phrase. A `min_turns` floor makes the
    manager ignore stop phrases until enough turns have happened — small local
    models often emit DONE on turn 0, and this guarantees a real exchange even
    when the prompt is not obeyed. (No-progress detection is a later phase.)"""
    policy = run.turn_policy or {}
    max_turns = int(policy.get("max_turns", DEFAULT_MAX_TURNS))
    min_turns = int(policy.get("min_turns", 0))
    budget_left = max(0, max_turns - run.turn)

    if run.stop_requested:
        return StopDecision(True, "stopped", "operator_stop",
                            "Conversation stopped by operator.", budget_left)
    if run.turn >= max_turns:
        return StopDecision(True, "finished", "max_turns",
                            f"Conversation finished: reached max_turns ({max_turns}).", 0)
    max_secs = policy.get("max_wall_clock_seconds")
    if max_secs:
        # Measure from the resettable wall-clock anchor (epoch in budget), which
        # resume refreshes — so time spent paused/stopped doesn't count. Fall back
        # to created_at for runs predating the anchor.
        anchor = (run.budget or {}).get("wall_clock_started_at")
        if anchor is not None:
            elapsed = time.time() - float(anchor)
        elif run.created_at is not None:
            started = run.created_at
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            elapsed = (datetime.now(UTC) - started).total_seconds()
        else:
            elapsed = 0.0
        if elapsed > float(max_secs):
            return StopDecision(True, "finished", "wall_clock",
                                f"Conversation finished: wall-clock budget ({max_secs}s) exceeded.",
                                budget_left)
    if run.turn >= min_turns:
        stop_phrases = policy.get("stop_phrases") or list(DEFAULT_STOP_PHRASES)
        last = _last_agent_message_text(messages, manager_uuid)
        if last:
            matched = next((sp for sp in stop_phrases if sp and sp in last), None)
            if matched is not None:
                return StopDecision(True, "finished", "stop_phrase",
                                    f"Conversation finished: stop phrase {matched!r}.", budget_left)
    return StopDecision(False, budget_left=budget_left)


def build_conversation_prompt(
    speaker_name: str,
    other_names: list[str],
    turn: int,
    max_turns: int | None,
    transcript: str,
) -> str:
    """Build the per-turn user prompt for a persona speaking inside a managed
    conversation: a runtime preamble (who you are, who else is here, the turn
    budget, the output contract) followed by the recent transcript. Pure, so it is
    unit-testable without a DB or model."""
    others = ", ".join(other_names) if other_names else "another agent"
    max_s = str(max_turns) if max_turns is not None else "?"
    preamble = (
        f"You are {speaker_name}. You are in a working conversation with {others}.\n"
        f"This is turn {turn} of at most {max_s}. Read the latest message, respond to "
        f"it specifically, make one concrete step of progress, and ask a question to "
        f"move it forward.\n"
        f"End your message with DONE on its own line only when the goal is genuinely "
        f"met and agreed."
    )
    return f"{preamble}\n\n{transcript}" if transcript else preamble


class ConversationManagerAgent(Agent):
    """Drains manager-tick jobs and drives one `conversation_run` forward by one
    speaker turn per tick. Does no LLM work."""

    @staticmethod
    def _run_uuid(payload: dict[str, Any]) -> UUID | None:
        raw = payload.get("run_uuid") or (payload.get("input") or {}).get("run_uuid")
        return UUID(str(raw)) if raw else None

    def handle(self, journal_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        run_uuid = self._run_uuid(payload)
        if run_uuid is None:
            return {"ok": True, "skipped": "no run_uuid"}
        run = db.get_conversation_run(run_uuid)
        if run is None or run.status != "running":
            return {"ok": True, "skipped": "run not active"}

        routed = "from_journal_id" in payload
        if not routed:
            # Manual tick (start / resume / stop / single-step): claim it so a
            # double click can't enqueue two speakers for the same turn.
            expected = payload.get("expected_tick_count")
            if expected is None or not db.claim_conversation_tick(run_uuid, expected):
                return {"ok": True, "skipped": "stale manual tick", "expected_tick_count": expected}
        else:
            # Routed speaker completion. Branch on the journal state the supervisor
            # stamped into the payload: a FAILED turn must not be treated as a
            # successful turn (that would silently swallow the error and advance).
            src = payload.get("from_journal_id")
            completed_turn = (payload.get("input") or {}).get("turn")
            if completed_turn is None:
                return {"ok": True, "skipped": "no completed turn", "journal": src}
            if payload.get("state") == "failed":
                return self._handle_failed_turn(run_uuid, src, completed_turn)
            if not db.advance_conversation_if_new(run_uuid, src, completed_turn):
                return {"ok": True, "skipped": "already advanced", "journal": src}

        run = db.get_conversation_run(run_uuid)  # reload after the state mutation
        if run is None or run.status != "running":
            return {"ok": True, "skipped": "run not active"}

        # Human interruption: pause and let the operator decide to resume.
        interruption = db.find_human_message_after(run.room_uuid, run.last_human_message_id)
        if interruption is not None:
            db.pause_conversation(run_uuid, reason="human_interruption",
                                  last_human_message_id=interruption.id)
            db.post_chat_message(
                run.room_uuid, CONVERSATION_MANAGER_UUID,
                "Conversation paused: a human message arrived. Resume to continue.",
                kind="debug-conversation",
            )
            return {"ok": True, "paused": "human_interruption"}

        messages = db.list_room_messages(run.room_uuid)
        stop = evaluate_stop(run, messages, CONVERSATION_MANAGER_UUID)
        if stop.should_stop:
            db.finish_conversation(run_uuid, status=stop.status, reason=stop.reason)
            db.post_chat_message(run.room_uuid, CONVERSATION_MANAGER_UUID, stop.summary, kind="message")
            logger.info("conversation %s stopped: %s", run_uuid, stop.reason)
            return {"ok": True, "stopped": stop.reason}

        if run.active_turn is not None:
            # A speaker turn is already in flight; nothing to schedule.
            return {"ok": True, "skipped": "speaker in flight", "turn": run.active_turn}

        if not run.participants:
            db.finish_conversation(run_uuid, status="failed", reason="no_participants")
            return {"ok": True, "stopped": "no_participants"}

        speaker = self._enqueue_speaker(
            run_uuid, run.room_uuid, run.participants, run.turn, budget_left=stop.budget_left
        )
        logger.info("conversation %s turn %d -> %s", run_uuid, run.turn, speaker.get("slug"))
        return {"ok": True, "enqueued": speaker.get("slug"), "turn": run.turn}

    def _enqueue_speaker(
        self,
        run_uuid: UUID,
        room_uuid: UUID,
        participants: list[dict[str, Any]],
        turn: int,
        budget_left: int | None = None,
        attempt: int | None = None,
    ) -> dict[str, Any]:
        """Mark the turn in flight, post a debug-conversation row, and enqueue the
        round-robin speaker for `turn` with a dynamic return address. Shared by the
        normal schedule path and the failed-turn retry path."""
        speaker = next_speaker(participants, turn)
        speaker_uuid = UUID(speaker["agent_uuid"])
        db.mark_conversation_turn_in_flight(run_uuid, turn, speaker_uuid)
        info: dict[str, Any] = {"next": speaker.get("slug"), "turn": turn}
        if budget_left is not None:
            info["budget_left"] = budget_left
        if attempt is not None:
            info["retry_attempt"] = attempt
        db.post_chat_message(
            room_uuid, CONVERSATION_MANAGER_UUID, json.dumps(info),
            content_type="json", kind="debug-conversation",
        )
        db.enqueue(speaker_uuid, {
            "run_uuid": str(run_uuid),
            "turn": turn,
            "room_uuid": str(room_uuid),
            "persona_id": speaker.get("persona_id"),
            "expected_speaker_uuid": speaker["agent_uuid"],
            "return_to_agent_uuid": str(CONVERSATION_MANAGER_UUID),
        })
        return speaker

    def _handle_failed_turn(
        self, run_uuid: UUID, src_journal_id: int, completed_turn: int
    ) -> dict[str, Any]:
        """A speaker turn errored (routed back as a failed journal). Retry the same
        speaker up to MAX_TURN_RETRIES, then mark the whole run failed. Idempotent
        via claim_failed_turn so a duplicate failed delivery is ignored."""
        outcome = db.claim_failed_turn(run_uuid, src_journal_id, completed_turn)
        if outcome is None:
            return {"ok": True, "skipped": "failed turn already handled", "journal": src_journal_id}
        run = db.get_conversation_run(run_uuid)
        if run is None or run.status != "running":
            return {"ok": True, "skipped": "run not active"}
        if outcome > db.MAX_TURN_RETRIES:
            db.finish_conversation(run_uuid, status="failed", reason="turn_failed")
            db.post_chat_message(
                run.room_uuid, CONVERSATION_MANAGER_UUID,
                f"Conversation failed: a speaker turn errored {outcome} time(s).",
                kind="message",
            )
            logger.warning("conversation %s failed: turn %d errored %d time(s)",
                           run_uuid, completed_turn, outcome)
            return {"ok": True, "failed": "turn_failed", "turn": completed_turn}
        speaker = self._enqueue_speaker(
            run_uuid, run.room_uuid, run.participants, completed_turn, attempt=outcome
        )
        logger.info("conversation %s retry turn %d (attempt %d) -> %s",
                    run_uuid, completed_turn, outcome, speaker.get("slug"))
        return {"ok": True, "retried": speaker.get("slug"), "turn": completed_turn, "attempt": outcome}
