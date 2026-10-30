"""Prompt layouts — how a chat-facing model call arranges its messages.

Every layout sends the same material; they differ in where the user's request
sits relative to everything else.

- `single_turn`: the system prompt, then ONE user message holding everything —
  history, settings, memories, the call's instructions — with the request as
  one section inside it.
- `request_turn`: the system prompt, a user message holding the context (the
  same sections minus the request), a fixed assistant acknowledgement, then the
  request itself, verbatim, as the final user message. The chat template
  already marks the last user turn as the thing to act on; this layout gives
  that position to the operator's own words instead of to a context blob.
- `exchange_turns`: `request_turn`, with the conversation's latest exchange —
  the user's previous message and this model's reply to it — repeated as turns
  of their own between the acknowledgement and the request. A follow-up too
  short to name its subject ("more details") takes it from that exchange, and
  as the last lines of a long history it competes with every earlier message;
  as real turns it sits where a chat template puts the previous exchange. The
  history in the context message is unchanged, so every call of a turn keeps
  one byte-identical head. Used only when those two messages are the user and
  then THIS model: in a group room another agent's message is not this model's
  turn, and the layout falls back to `request_turn`'s shape.

The `prompt.layout` setting picks the layout. Each agent reads it once per turn
so every call of one turn uses one layout. A new layout is a new constant here,
appended to PROMPT_LAYOUTS (which is the setting's `choices`), plus the branch
in each agent that builds it.

Design: notes/proposals/2026-10-19-request-as-its-own-turn.md.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

SINGLE_TURN = "single_turn"
REQUEST_TURN = "request_turn"
EXCHANGE_TURNS = "exchange_turns"

PROMPT_LAYOUTS: tuple[str, ...] = (SINGLE_TURN, REQUEST_TURN, EXCHANGE_TURNS)
# The layouts that send the request as a message of its own, after the
# context: everything request_turn changes in a prompt applies to them all.
REQUEST_MESSAGE_LAYOUTS: tuple[str, ...] = (REQUEST_TURN, EXCHANGE_TURNS)
DEFAULT_PROMPT_LAYOUT = SINGLE_TURN

# The assistant turn between the context and the request. Written in the
# model's own voice and stating what it will do, because a model stays
# consistent with what it has already said; a request for more ("now provide
# the prompt") would commit it to nothing. "Message", not "prompt": the person
# on the other end writes a message. One constant for every call that uses the
# layout — each call's own job is in its instructions, and "act on" covers
# replying, choosing an action and establishing criteria alike.
REQUEST_TURN_ACKNOWLEDGEMENT = (
    "Context received. I'll treat it as reference material and act on your "
    "next message."
)


# The acknowledgement when the latest exchange follows it: "your next
# message" would name the user's previous message, which is not the one to act
# on.
EXCHANGE_TURNS_ACKNOWLEDGEMENT = (
    "Context received. I'll treat it as reference material. Our latest "
    "exchange follows, and I'll act on your final message."
)


def latest_exchange(
    messages: list[dict[str, Any]], agent_uuid: Any,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """The user's previous message and this agent's reply to it — the two
    messages right before the request (`messages[-1]`) — or None when they
    are not exactly that: too short a conversation, or a reply from someone
    else."""
    if len(messages) < 3:
        return None
    asked, answered = messages[-3], messages[-2]
    if asked.get("sender_type") != "human":
        return None
    if (answered.get("sender_type") != "agent"
            or str(answered.get("sender_uuid")) != str(agent_uuid)):
        return None
    return asked, answered


def request_turns(
    request_text: str,
    exchange: tuple[str, str] | None = None,
) -> list[dict[str, str]]:
    """The turns that follow the context message: the acknowledgement, then
    the request. `{role, content}` dicts — the shape the rejected-attempt
    feedback turns already use, so the trace stores both alike. With
    `exchange` (the user's previous message and the reply to it, as text),
    the exchange_turns shape: its own acknowledgement, the exchange as a user
    and an assistant turn, then the request."""
    if exchange is None:
        return [
            {"role": "assistant", "content": REQUEST_TURN_ACKNOWLEDGEMENT},
            {"role": "user", "content": request_text},
        ]
    asked, answered = exchange
    return [
        {"role": "assistant", "content": EXCHANGE_TURNS_ACKNOWLEDGEMENT},
        {"role": "user", "content": asked},
        {"role": "assistant", "content": answered},
        {"role": "user", "content": request_text},
    ]


def current_prompt_layout() -> str:
    """The `prompt.layout` setting, read once per turn by each agent.

    Best-effort: an unreadable or unknown value reads as the default, so a
    settings failure never changes how a prompt is built on its own."""
    try:
        import db

        value = db.get_setting("prompt.layout")
    except Exception:
        logger.warning("prompt.layout setting read failed", exc_info=True)
        return DEFAULT_PROMPT_LAYOUT
    return str(value) if value in PROMPT_LAYOUTS else DEFAULT_PROMPT_LAYOUT
