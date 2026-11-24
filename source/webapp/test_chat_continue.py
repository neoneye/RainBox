"""The Continue button's series: /chat/api/rooms/<room>/continue."""
from unittest.mock import patch
from uuid import uuid4

import pytest

import db
from agents.config import ASSISTANT_UUID
from chat.wake import WAKE_KIND
from webapp import chat_continue
from webapp.chat_continue import ContinueSeries, post_wake, run_series
from webapp.core import app


@pytest.fixture
def ctx():
    a = db.make_app()
    db.init_db(a)
    c = a.app_context()
    c.push()
    try:
        yield
    finally:
        c.pop()


@pytest.fixture
def room(ctx):
    human = db.get_human_user()
    r = db.create_chatroom(f"continue-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])
    chat_continue._series.pop(r.uuid, None)
    try:
        yield r
    finally:
        chat_continue._series.pop(r.uuid, None)
        db.delete_chatroom(r.uuid)


def _wakes(room_uuid):
    return [m for m in db.list_room_messages(room_uuid) if m["kind"] == WAKE_KIND]


def _finish_run_for(wake_uuid, room_uuid, status="finished"):
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room_uuid, agent_uuid=ASSISTANT_UUID,
        request_message=wake_uuid)
    db.finish_run(run, status)


def _post(room_uuid, **body):
    # The thread is not started: each test drives the series itself.
    with patch.object(chat_continue.threading, "Thread"):
        return app.test_client().post(
            f"/chat/api/rooms/{room_uuid}/continue", json=body)


def test_a_press_posts_the_first_wake_at_once_and_enqueues_the_assistant(room):
    with patch.object(db, "enqueue") as enqueue:
        resp = _post(room.uuid, count=2, sleep_seconds=30)
    assert resp.status_code == 202, resp.get_data(as_text=True)
    state = resp.get_json()
    assert (state["active"], state["index"], state["count"]) == (True, 1, 2)
    wakes = _wakes(room.uuid)
    assert len(wakes) == 1
    assert wakes[0]["meta"]["wake"] == {
        "source": "continue_button", "index": 1, "count": 2}
    assert "wake-up 1 of 2" in wakes[0]["text"]
    agent, payload = enqueue.call_args.args
    assert agent == ASSISTANT_UUID
    assert payload == {"room_uuid": str(room.uuid), "message_uuid": wakes[0]["uuid"]}
    # The working bubble a user's message would get.
    assert any(m["kind"] == "progress" for m in db.list_room_messages(room.uuid))


def test_a_wake_is_not_a_message_of_the_conversation(room):
    with patch.object(db, "enqueue"):
        _post(room.uuid, count=1, sleep_seconds=30)
    assert not [m for m in db.list_room_messages(room.uuid) if m["kind"] == "message"]


def test_values_the_pickers_do_not_offer_are_refused(room):
    assert _post(room.uuid, count=4, sleep_seconds=30).status_code == 400
    assert _post(room.uuid, count=1, sleep_seconds=5).status_code == 400
    assert not _wakes(room.uuid)


def test_a_second_press_while_one_runs_is_refused(room):
    with patch.object(db, "enqueue"):
        assert _post(room.uuid, count=1, sleep_seconds=30).status_code == 202
        assert _post(room.uuid, count=1, sleep_seconds=30).status_code == 409
    assert len(_wakes(room.uuid)) == 1


def test_a_room_without_the_assistant_has_nothing_to_wake(ctx):
    human = db.get_human_user()
    r = db.create_chatroom(f"continue-{uuid4().hex[:8]}", human.uuid, [])
    try:
        assert _post(r.uuid, count=1, sleep_seconds=30).status_code == 400
    finally:
        db.delete_chatroom(r.uuid)


def _series(room_uuid, count):
    series = ContinueSeries(room_uuid, count, 30)
    with patch.object(db, "enqueue"):
        post_wake(series, 1)
    return series


def test_the_series_wakes_again_after_each_reply_and_a_sleep(room, monkeypatch):
    monkeypatch.setattr(chat_continue, "POLL_SECONDS", 0.01)
    series = _series(room.uuid, 3)
    slept = []

    # Each wake-up's run is already finished when the series looks.
    real_post_wake = chat_continue.post_wake

    def post_and_finish(s, index):
        real_post_wake(s, index)
        _finish_run_for(s.wake_uuid, room.uuid)

    _finish_run_for(series.wake_uuid, room.uuid)
    monkeypatch.setattr(chat_continue, "_sleep", lambda s: slept.append(s.index))
    monkeypatch.setattr(chat_continue, "post_wake", post_and_finish)
    with patch.object(db, "enqueue"):
        run_series(series)
    assert series.ended == "done"
    assert [w["meta"]["wake"]["index"] for w in _wakes(room.uuid)] == [1, 2, 3]
    # A sleep between adjacent wake-ups, none after the last.
    assert slept == [1, 2]


def test_a_single_wake_ends_without_sleeping(room, monkeypatch):
    monkeypatch.setattr(chat_continue, "POLL_SECONDS", 0.01)
    series = _series(room.uuid, 1)
    _finish_run_for(series.wake_uuid, room.uuid)
    monkeypatch.setattr(chat_continue, "_sleep", lambda s: pytest.fail("slept"))
    run_series(series)
    assert series.ended == "done"


def test_a_run_that_did_not_finish_ends_the_series(room, monkeypatch):
    monkeypatch.setattr(chat_continue, "POLL_SECONDS", 0.01)
    series = _series(room.uuid, 3)
    _finish_run_for(series.wake_uuid, room.uuid, status="failed")
    run_series(series)
    assert series.ended == "run_failed"
    assert len(_wakes(room.uuid)) == 1


def test_a_message_from_the_user_during_the_sleep_ends_the_series(room, monkeypatch):
    monkeypatch.setattr(chat_continue, "POLL_SECONDS", 0.01)
    series = _series(room.uuid, 2)
    _finish_run_for(series.wake_uuid, room.uuid)
    db.post_chat_message(room.uuid, db.get_human_user().uuid, "back again")
    run_series(series)
    assert series.ended == "user_wrote"
    assert len(_wakes(room.uuid)) == 1


def test_cancel_stops_the_wakes_still_to_come(room, monkeypatch):
    monkeypatch.setattr(chat_continue, "POLL_SECONDS", 0.01)
    with patch.object(db, "enqueue"):
        _post(room.uuid, count=3, sleep_seconds=30)
    series = chat_continue._series[room.uuid]
    assert app.test_client().delete(
        f"/chat/api/rooms/{room.uuid}/continue").status_code == 200
    run_series(series)
    assert series.ended == "cancelled"
    state = app.test_client().get(f"/chat/api/rooms/{room.uuid}/continue").get_json()
    assert state["active"] is False and state["ended"] == "Cancelled."
    assert len(_wakes(room.uuid)) == 1


def test_no_worker_taking_the_wake_ends_the_series(room, monkeypatch):
    monkeypatch.setattr(chat_continue, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(chat_continue, "RUN_START_TIMEOUT_SECONDS", 0.03)
    series = _series(room.uuid, 2)
    run_series(series)
    assert series.ended == "not_started"
