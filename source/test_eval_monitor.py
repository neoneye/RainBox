"""Tests for eval_monitor.run_production_sample.

Lives against live local Postgres. Creates real ChatUser + Chatroom +
ChatMessage rows so the kind-filter test exercises the actual query.
"""

from uuid import uuid4

import pytest

import db
from db import (
    ChatMessage,
    Chatroom,
    ChatUser,
    EvalCase,
    EvalResult,
    EvalRun,
)

from eval_monitor import run_production_sample


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    try:
        yield app
    finally:
        ctx.pop()


@pytest.fixture
def fresh_tag() -> str:
    return f"test-{uuid4().hex[:8]}"


def _make_room_and_user(prefix: str) -> tuple[ChatUser, Chatroom]:
    user = ChatUser(uuid=uuid4(), name=f"{prefix}-user", user_type="agent")
    db.db.session.add(user)
    db.db.session.flush()
    room = db.create_chatroom(
        name=f"{prefix}-room",
        created_by=user.uuid,
        member_uuids=[user.uuid],
    )
    return user, room


def _post(room_uuid, sender_uuid, text: str, *, kind: str = "message"):
    return db.post_chat_message(
        room_uuid=room_uuid, sender_uuid=sender_uuid,
        text=text, kind=kind,
    )


def _cleanup(prefix: str) -> None:
    run_uuids = [
        r.uuid for r in db.db.session.query(EvalRun)
        .filter(EvalRun.name.like(f"{prefix}%")).all()
    ]
    if run_uuids:
        db.db.session.query(EvalResult).filter(
            EvalResult.eval_run_uuid.in_(run_uuids)
        ).delete(synchronize_session=False)
        db.db.session.query(EvalRun).filter(
            EvalRun.uuid.in_(run_uuids)
        ).delete(synchronize_session=False)
    room_uuids = [
        r.uuid for r in db.db.session.query(Chatroom)
        .filter(Chatroom.name.like(f"{prefix}%")).all()
    ]
    if room_uuids:
        db.db.session.query(ChatMessage).filter(
            ChatMessage.room_uuid.in_(room_uuids)
        ).delete(synchronize_session=False)
        db.db.session.query(Chatroom).filter(
            Chatroom.uuid.in_(room_uuids)
        ).delete(synchronize_session=False)
    db.db.session.query(ChatUser).filter(
        ChatUser.name.like(f"{prefix}%")
    ).delete(synchronize_session=False)
    # NOTE: do NOT delete the shared production_sample_message EvalCase —
    # it's idempotent and harmless across tests.
    db.db.session.commit()


def test_production_monitor_creates_eval_run_from_recent_chat(
    app_ctx, fresh_tag,
):
    try:
        user, room = _make_room_and_user(fresh_tag)
        _post(room.uuid, user.uuid, f"{fresh_tag} hello world")
        _post(room.uuid, user.uuid, f"{fresh_tag} second message")

        run = run_production_sample(
            limit=50, name_prefix=fresh_tag,
        )
        assert isinstance(run, EvalRun)
        assert (run.config or {}).get("source") == "production_sample"

        results = db.list_eval_results_for_run(run.uuid)
        our_results = [
            r for r in results
            if fresh_tag in (r.details or {}).get("text", "")
        ]
        assert len(our_results) == 2
    finally:
        _cleanup(fresh_tag)


def test_production_monitor_filters_out_human_messages(
    app_ctx, fresh_tag,
):
    """Spec requires sampling AGENT outputs only, not human inputs."""
    try:
        agent_user, room = _make_room_and_user(fresh_tag)
        # Build a human user in the same room.
        human_user = ChatUser(
            uuid=uuid4(),
            name=f"{fresh_tag}-human",
            user_type="human",
        )
        db.db.session.add(human_user)
        db.db.session.flush()

        _post(room.uuid, agent_user.uuid,
              f"{fresh_tag} agent reply", kind="message")
        _post(room.uuid, human_user.uuid,
              f"{fresh_tag} human input", kind="message")

        run = run_production_sample(limit=50, name_prefix=fresh_tag)
        results = db.list_eval_results_for_run(run.uuid)
        our_results = [
            r for r in results
            if fresh_tag in (r.details or {}).get("text", "")
        ]
        assert len(our_results) == 1, our_results
        assert "agent reply" in our_results[0].details["text"]
        assert "human input" not in our_results[0].details["text"]
    finally:
        _cleanup(fresh_tag)


def test_production_monitor_ignores_diagnostic_rows(app_ctx, fresh_tag):
    try:
        user, room = _make_room_and_user(fresh_tag)
        _post(room.uuid, user.uuid, f"{fresh_tag} real message",
              kind="message")
        _post(room.uuid, user.uuid, f"{fresh_tag} diagnostic A",
              kind="debug-memory")
        _post(room.uuid, user.uuid, f"{fresh_tag} diagnostic B",
              kind="debug-router")
        _post(room.uuid, user.uuid, f"{fresh_tag} progress",
              kind="progress")

        run = run_production_sample(
            limit=50, name_prefix=fresh_tag,
        )
        results = db.list_eval_results_for_run(run.uuid)
        our_results = [
            r for r in results
            if fresh_tag in (r.details or {}).get("text", "")
        ]
        assert len(our_results) == 1
        assert "real message" in our_results[0].details["text"]
    finally:
        _cleanup(fresh_tag)
