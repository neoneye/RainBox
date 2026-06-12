"""Unit tests for chat_transcript.format_history (IRC-style transcript rendering).

These are pure functions — no database or LM Studio needed:

    python -m pytest chat/test_transcript.py -v
"""

from datetime import datetime

from chat.transcript import format_history


def test_empty_history():
    assert format_history([]) == "Chat history: none\n\nCurrent message: none"


def test_single_message_has_no_history():
    out = format_history([{"sender_name": "operator", "text": "ping"}])
    assert out == "Chat history: none\n\nCurrent message:\n<operator> ping"


def test_multiple_oldest_first_with_current_separated():
    msgs = [
        {"sender_name": "operator", "text": "hi"},
        {"sender_name": "chatagent", "text": "Hi!"},
        {"sender_name": "operator", "text": "ping"},
    ]
    assert format_history(msgs) == (
        "Chat history, oldest first:\n"
        "<operator> hi\n"
        "<chatagent> Hi!\n"
        "\n"
        "Current message:\n"
        "<operator> ping"
    )


def test_multiline_text_collapsed_to_one_line():
    out = format_history([{"sender_name": "operator", "text": "first line\nsecond line"}])
    assert "<operator> first line\\nsecond line" in out
    assert "first line\nsecond line" not in out  # no real newline inside a message


def test_string_timestamp_rendered():
    out = format_history(
        [
            {"sender_name": "a", "text": "x"},
            {"sender_name": "operator", "text": "ping", "timestamp": "2026-05-26 01:31"},
        ]
    )
    assert "[2026-05-26 01:31] <operator> ping" in out


def test_datetime_timestamp_rendered():
    out = format_history(
        [{"sender_name": "operator", "text": "ping", "created_at": datetime(2026, 5, 26, 1, 31)}]
    )
    assert "[2026-05-26 01:31] <operator> ping" in out


def test_context_limit_caps_history():
    msgs = [{"sender_name": "u", "text": str(i)} for i in range(30)]
    out = format_history(msgs, context_limit=5)
    history = out.split("Current message:")[0]
    assert history.count("<u>") == 5  # only the last 5 context messages
