"""Tests for the eval_case table + create/list helpers and feedback promotion.

Live local Postgres. Each test cleans up the rows it creates via the
unique `name` tag (per-test UUID-based) so test artifacts don't accumulate.
"""

from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

import db
from db import EvalCase, FeedbackEvent


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
    """Unique per-test prefix used in EvalCase.name for cleanup."""
    return f"test-{uuid4().hex[:8]}"


def _cleanup_by_name_prefix(prefix: str) -> None:
    db.db.session.query(EvalCase).filter(
        EvalCase.name.like(f"{prefix}%")
    ).delete(synchronize_session=False)
    db.db.session.commit()


def test_create_eval_case_persists_all_fields(app_ctx, fresh_tag):
    try:
        ec = db.create_eval_case(
            name=f"{fresh_tag}: persistence",
            case_type="chat_reply",
            split="train",
            input={"current_message": "hi"},
            expected={"must_include": ["hello"]},
            rubric={"threshold": 0.7},
            status="candidate",
        )
        db.db.session.expire_all()
        reloaded = db.get_eval_case(ec.uuid)
        assert reloaded is not None
        assert reloaded.case_type == "chat_reply"
        assert reloaded.split == "train"
        assert reloaded.status == "candidate"
        assert reloaded.input == {"current_message": "hi"}
        assert reloaded.expected == {"must_include": ["hello"]}
        assert reloaded.rubric == {"threshold": 0.7}
        assert reloaded.created_at is not None
        assert reloaded.updated_at is not None
        assert reloaded.source_feedback_uuid is None
    finally:
        _cleanup_by_name_prefix(fresh_tag)


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("case_type", "nonsense"),
        ("split", "nonsense"),
        ("status", "nonsense"),
    ],
)
def test_invalid_enum_values_rejected_by_db(app_ctx, fresh_tag, field, bad_value):
    args = dict(
        name=f"{fresh_tag}: invalid {field}",
        case_type="chat_reply",
        split="train",
        status="candidate",
    )
    args[field] = bad_value
    try:
        with pytest.raises(sa.exc.IntegrityError):
            db.create_eval_case(**args)
    finally:
        db.db.session.rollback()
        _cleanup_by_name_prefix(fresh_tag)


def test_list_eval_cases_filters_by_status_split_case_type(app_ctx, fresh_tag):
    try:
        a = db.create_eval_case(
            name=f"{fresh_tag}: A", case_type="chat_reply",
            split="train", status="candidate",
        )
        b = db.create_eval_case(
            name=f"{fresh_tag}: B", case_type="chat_reply",
            split="regression", status="candidate",
        )
        c = db.create_eval_case(
            name=f"{fresh_tag}: C", case_type="memory_retrieval",
            split="train", status="active",
        )
        candidates = db.list_eval_cases(status="candidate")
        cand_uuids = {x.uuid for x in candidates}
        assert a.uuid in cand_uuids and b.uuid in cand_uuids
        assert c.uuid not in cand_uuids

        regs = db.list_eval_cases(split="regression")
        assert [x.uuid for x in regs if x.name.startswith(fresh_tag)] == [b.uuid]

        mrs = db.list_eval_cases(case_type="memory_retrieval")
        assert [x.uuid for x in mrs if x.name.startswith(fresh_tag)] == [c.uuid]
    finally:
        _cleanup_by_name_prefix(fresh_tag)


def _new_chatroom_with_agent():
    """Set up a chatroom + agent ChatUser for feedback fixtures. Returns
    (room_uuid, human_uuid, agent_uuid, cleanup_callable)."""
    human = db.get_human_user()
    assert human is not None
    agent_uuid = uuid4()
    agent_user = db.ChatUser(
        uuid=agent_uuid, name=f"ec-test-{uuid4().hex[:6]}",
        user_type="agent",
    )
    db.db.session.add(agent_user)
    db.db.session.flush()
    room = db.create_chatroom(
        f"ec-{uuid4().hex[:6]}", human.uuid, [agent_uuid],
    )

    def _cleanup():
        db.db.session.query(EvalCase).filter(
            EvalCase.source_feedback_uuid.in_(
                db.db.session.query(FeedbackEvent.uuid)
                .filter(FeedbackEvent.room_uuid == room.uuid)
                .subquery().select()
            )
        ).delete(synchronize_session=False)
        db.db.session.query(FeedbackEvent).filter(
            FeedbackEvent.room_uuid == room.uuid
        ).delete()
        db.db.session.query(db.Chatroom).filter(
            db.Chatroom.uuid == room.uuid
        ).delete()
        db.db.session.query(db.ChatUser).filter(
            db.ChatUser.uuid == agent_uuid
        ).delete()
        db.db.session.commit()

    return room.uuid, human.uuid, agent_uuid, _cleanup


def test_promoting_downvote_creates_candidate_regression_case(app_ctx):
    room_uuid, human_uuid, agent_uuid, cleanup = _new_chatroom_with_agent()
    try:
        db.post_chat_message(room_uuid, human_uuid, "what is x?")
        reply = db.post_chat_message(
            room_uuid, agent_uuid, "x is unrelated stuff",
        )
        fb = db.create_feedback_event(
            room_uuid=room_uuid, message_uuid=reply.uuid,
            agent_uuid=agent_uuid, rating="downvote",
            comment="answer ignored the question",
            created_by_uuid=human_uuid,
        )
        ec = db.promote_feedback_to_eval_case(fb.uuid)
        assert ec.status == "candidate"
        assert ec.split == "regression"
        assert ec.case_type == "chat_reply"
    finally:
        cleanup()


def test_promoted_case_links_back_to_source_feedback(app_ctx):
    room_uuid, human_uuid, agent_uuid, cleanup = _new_chatroom_with_agent()
    try:
        db.post_chat_message(room_uuid, human_uuid, "ping")
        reply = db.post_chat_message(room_uuid, agent_uuid, "pong")
        fb = db.create_feedback_event(
            room_uuid=room_uuid, message_uuid=reply.uuid,
            agent_uuid=agent_uuid, rating="downvote",
            comment=None, created_by_uuid=human_uuid,
        )
        ec = db.promote_feedback_to_eval_case(fb.uuid)
        assert ec.source_feedback_uuid == fb.uuid
    finally:
        cleanup()


def test_promoted_case_input_includes_prev_human_and_rated_text(app_ctx):
    room_uuid, human_uuid, agent_uuid, cleanup = _new_chatroom_with_agent()
    try:
        db.post_chat_message(room_uuid, human_uuid, "give me a fact")
        reply = db.post_chat_message(room_uuid, agent_uuid, "cats are mammals")
        fb = db.create_feedback_event(
            room_uuid=room_uuid, message_uuid=reply.uuid,
            agent_uuid=agent_uuid, rating="downvote",
            comment=None, created_by_uuid=human_uuid,
        )
        ec = db.promote_feedback_to_eval_case(fb.uuid)
        history = ec.input.get("room_history") or []
        assert any(
            h.get("text") == "give me a fact" for h in history
        ), f"expected prev human in history; got {history}"
        assert ec.input.get("current_message") == "give me a fact"
        assert ec.input.get("rated_message_text") == "cats are mammals"
        assert ec.input.get("agent_role") == "chat"
    finally:
        cleanup()


def test_promoted_case_expected_notes_carry_feedback_comment(app_ctx):
    room_uuid, human_uuid, agent_uuid, cleanup = _new_chatroom_with_agent()
    try:
        db.post_chat_message(room_uuid, human_uuid, "ping")
        reply = db.post_chat_message(room_uuid, agent_uuid, "pong")
        fb = db.create_feedback_event(
            room_uuid=room_uuid, message_uuid=reply.uuid,
            agent_uuid=agent_uuid, rating="downvote",
            comment="too short", created_by_uuid=human_uuid,
        )
        ec = db.promote_feedback_to_eval_case(fb.uuid)
        assert ec.expected.get("notes") == "too short"
    finally:
        cleanup()


def test_promoting_missing_feedback_uuid_raises_clear_error(app_ctx):
    nonexistent = uuid4()
    with pytest.raises(ValueError) as excinfo:
        db.promote_feedback_to_eval_case(nonexistent)
    assert str(nonexistent) in str(excinfo.value)


def test_promoting_upvote_defaults_to_train_split(app_ctx):
    """An upvote isn't a regression — only downvotes default to
    `split="regression"`. Upvotes default to `train`."""
    room_uuid, human_uuid, agent_uuid, cleanup = _new_chatroom_with_agent()
    try:
        db.post_chat_message(room_uuid, human_uuid, "ping")
        reply = db.post_chat_message(room_uuid, agent_uuid, "pong")
        fb = db.create_feedback_event(
            room_uuid=room_uuid, message_uuid=reply.uuid,
            agent_uuid=agent_uuid, rating="upvote",
            comment=None, created_by_uuid=human_uuid,
        )
        ec = db.promote_feedback_to_eval_case(fb.uuid)
        assert ec.split == "train"
    finally:
        cleanup()


def test_explicit_split_overrides_default(app_ctx):
    room_uuid, human_uuid, agent_uuid, cleanup = _new_chatroom_with_agent()
    try:
        db.post_chat_message(room_uuid, human_uuid, "ping")
        reply = db.post_chat_message(room_uuid, agent_uuid, "pong")
        fb = db.create_feedback_event(
            room_uuid=room_uuid, message_uuid=reply.uuid,
            agent_uuid=agent_uuid, rating="downvote",
            comment=None, created_by_uuid=human_uuid,
        )
        ec = db.promote_feedback_to_eval_case(fb.uuid, split="holdout")
        assert ec.split == "holdout"
    finally:
        cleanup()
