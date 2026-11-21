"""A turn's load of the room's background: when it is sent, what is held
back for want of room, and what the run records of it."""
from __future__ import annotations

from uuid import uuid4


import db
from agents import model_groups as mg
from agents.assistant import AssistantAgent
from agents.room_background import PROMPT_RESERVE_CHARS, Background
from diary.test_retrieval import app_ctx  # noqa: F401 — fixture


class _Run:
    def __init__(self):
        self.metadata_ = {}


def _messages(n: int = 40) -> list[dict]:
    return [{"id": i, "text": "word " * 20} for i in range(1, n + 1)]


LOADED = Background(topics="- the garden board (3 times)",
                    earlier="1999-10-04: the opening hours of the harbour office",
                    record={"chunks": 1, "series": [10, 2, 23], "load_ms": 3})


def _turn(monkeypatch, *, on=True, window=None, loaded=LOADED, messages=None):
    seen = {}

    def load(room_uuid, window_first_id, *, diary_allowed=True, series=None):
        seen.update(first=window_first_id, diary_allowed=diary_allowed)
        if isinstance(loaded, Exception):
            raise loaded
        return loaded

    monkeypatch.setattr(db, "get_setting", lambda key: on if key == "assistant.room_background" else None)
    monkeypatch.setattr(db, "load_room_background", load)
    monkeypatch.setattr(mg, "assistant_models_all_local", lambda: (False, "assistant.decide"))
    monkeypatch.setattr(mg, "assistant_context_window", lambda slot: window)
    agent = AssistantAgent(agent_uuid=uuid4(), name="assistant", send=lambda _: None)
    run = _Run()
    agent._load_room_background(uuid4(), _messages() if messages is None else messages, run)
    return agent._room_background, run.metadata_.get("room_background"), seen


def test_off_is_a_turn_as_before(app_ctx, monkeypatch):
    sent, record, seen = _turn(monkeypatch, on=False)
    assert sent is None and record is None and not seen


def test_on_sends_both_blocks_and_records_the_counts(app_ctx, monkeypatch):
    sent, record, seen = _turn(monkeypatch)
    assert (sent.topics, sent.earlier) == (LOADED.topics, LOADED.earlier)
    # The window is the 30 messages before the request: 40 messages, so 10 on.
    assert seen == {"first": 10, "diary_allowed": False}
    assert record["sent"] == "both" and record["context_window"] is None
    assert record["base_chars"] == 31 * 100 and record["chunks"] == 1
    assert LOADED.earlier not in str(record)


def test_a_declared_window_holds_the_blocks_back(app_ctx, monkeypatch):
    base = 31 * 100
    both = len(LOADED.topics) + len(LOADED.earlier)
    room = -(-(base + both + PROMPT_RESERVE_CHARS) // 3)     # tokens both need
    sent, record, _ = _turn(monkeypatch, window=room)
    assert record["sent"] == "both" and record["context_window"] == room
    # A little less: the topics go first.
    sent, record, _ = _turn(monkeypatch, window=room - 5)
    assert sent.topics == "" and sent.earlier == LOADED.earlier
    assert record["sent"] == "earlier"
    sent, record, _ = _turn(monkeypatch, window=8_192)
    assert sent is None and record["sent"] == "none"


def test_a_room_without_summaries_or_inside_its_window_sends_nothing(app_ctx, monkeypatch):
    sent, record, _ = _turn(monkeypatch, loaded=Background(record={"load_ms": 1}))
    assert sent is None and record == {"load_ms": 1, "sent": "none"}
    sent, record, seen = _turn(monkeypatch, messages=_messages(1))
    assert sent is None and record is None and not seen


def test_a_failed_load_leaves_the_turn_and_says_so(app_ctx, monkeypatch):
    sent, record, _ = _turn(monkeypatch, loaded=RuntimeError("no such table"))
    assert sent is None and record == {"error": "RuntimeError: no such table"}
