"""Chat transcript rendering — shared by every agent that turns a chatroom's
message history into an IRC-style prompt.

`format_history` renders messages oldest-first, one message per line as
`[ts] <sender> text`, with the latest message separated out as the "Current
message" at the bottom — the transcript shape local models expect. It lives in
its own module (rather than inside any one agent) because the structured and
unstructured chat agents, the router/query agents, and the tool/MCP agents all
share it; none of them should import it from a sibling agent module.
"""

from datetime import datetime
from typing import Any


def _format_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    text = str(value).strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return text  # already a usable timestamp string; keep it


def _message_timestamp(m: dict[str, Any]) -> str | None:
    for key in ("created_at", "created", "timestamp", "time"):
        if key in m:
            formatted = _format_timestamp(m.get(key))
            if formatted:
                return formatted
    return None


def _one_line_text(value: Any) -> str:
    """Collapse a message body to a single line so each transcript line is one
    message (escape CR/LF rather than emitting real line breaks)."""
    return str(value or "").replace("\r", "\\r").replace("\n", "\\n")


def _format_irc_line(m: dict[str, Any]) -> str:
    """One IRC-style line: `[ts] <sender> text` (timestamp omitted if absent)."""
    sender = _one_line_text(m.get("sender_name") or "unknown")
    text = _one_line_text(m.get("text") or "")
    timestamp = _message_timestamp(m)
    if timestamp:
        return f"[{timestamp}] <{sender}> {text}"
    return f"<{sender}> {text}"


def format_history(messages: list[dict[str, Any]], context_limit: int = 20) -> str:
    """Render chat history as an IRC-style transcript: oldest first, one message
    per line as `<sender> text` (with an optional timestamp prefix), and the
    latest message separated out as the Current message at the bottom. This is
    the transcript shape local models expect. db.list_room_messages returns
    messages oldest first."""
    if not messages:
        return "Chat history: none\n\nCurrent message: none"
    current = messages[-1]
    context = messages[:-1][-context_limit:]
    lines: list[str] = []
    if context:
        lines.append("Chat history, oldest first:")
        lines.extend(_format_irc_line(m) for m in context)
    else:
        lines.append("Chat history: none")
    lines.append("")
    lines.append("Current message:")
    lines.append(_format_irc_line(current))
    return "\n".join(lines)
