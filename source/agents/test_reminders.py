"""S4: assistant set_reminder — confirm-tier write with a dry-run preview that
schedules a one-shot cron 'message' job. Model-free; fake clock via cron_tick."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

import db
from agents.assistant import (
    CAPABILITIES,
    AssistantActionContext,
    AssistantActionName,
    AssistantAgent,
    AssistantStepDecision,
    _action_set_reminder,
)
from agents.assistant_fakes import scripted_decisions
from agents.assistant_writes import execute_write_intent
from agents.config import ASSISTANT_UUID
from db import AssistantRun, AssistantWriteIntent, ChatMessage, CronJob, CronRun


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    try:
        yield app
    finally:
        db.db.session.rollback()
        ctx.pop()


def _ctx(dry_run=False, room_uuid=None):
    return AssistantActionContext(
        journal_id=None, room_uuid=room_uuid or uuid4(), agent_uuid=ASSISTANT_UUID,
        step_index=0, dry_run=dry_run,
    )


def _jobs_with(tag):
    return db.db.session.query(CronJob).filter(CronJob.message.like(f"%{tag}%")).all()


def _cleanup_cron(tag):
    for j in _jobs_with(tag):
        db.db.session.query(CronRun).filter(CronRun.cron_uuid == j.uuid).delete()
    db.db.session.query(CronJob).filter(CronJob.message.like(f"%{tag}%")).delete()
    db.db.session.commit()


def test_capability_is_confirm_tier_dry_run_write():
    cap = CAPABILITIES[AssistantActionName.SET_REMINDER]
    assert cap.write is True and cap.tier == "confirm" and cap.dry_run is True


def test_dry_run_previews_without_creating_a_job(app_ctx):
    tag = f"rem-{uuid4()}"
    obs = _action_set_reminder(_ctx(dry_run=True),
                               {"text": tag, "when": "2026-06-27T09:00"})
    assert obs.ok is True and obs.text.startswith("Would remind you at")
    assert _jobs_with(tag) == []


def test_real_execution_creates_one_shot_job(app_ctx):
    tag = f"rem-{uuid4()}"
    room = uuid4()
    obs = _action_set_reminder(_ctx(room_uuid=room),
                               {"text": tag, "when": "2026-06-27T09:00"})
    try:
        assert obs.ok is True
        job = _jobs_with(tag)[0]
        assert job.action_type == "message" and job.cron_expr == ""
        # A naive 'when' is interpreted as the host's LOCAL time, not UTC, so the
        # stored fire instant is 09:00 local (astimezone() is DST-correct for the
        # given date). The job is tagged "localtime" to match.
        assert job.next_run_at == datetime(2026, 6, 27, 9, 0).astimezone()
        assert job.timezone == "localtime"
        assert job.target == str(room)
        assert job.message.startswith("⏰ Reminder:")
    finally:
        _cleanup_cron(tag)


def test_set_reminder_result_includes_cron_link(app_ctx):
    # The confirm response carries a link to the created cron job so the chat
    # card can show "View reminder ↗" pointing at /cron?id=<cron_job_uuid>.
    tag = f"rem-{uuid4()}"
    obs = _action_set_reminder(_ctx(), {"text": tag, "when": "2026-06-29T09:00"})
    try:
        assert obs.ok is True
        assert obs.data["link"] == f"/cron?id={obs.data['cron_job_uuid']}"
    finally:
        _cleanup_cron(tag)


def test_bad_datetime_rejected(app_ctx):
    assert _action_set_reminder(_ctx(dry_run=True), {"text": "x", "when": "friday"}).ok is False
    assert _action_set_reminder(_ctx(), {"text": "x", "when": "friday"}).ok is False


def _room():
    human = db.get_human_user()
    return db.create_chatroom(f"rem-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])


def test_propose_uses_dry_run_preview_and_does_not_schedule(app_ctx):
    tag = f"rem-{uuid4()}"
    chatroom = _room()
    db.post_chat_message(chatroom.uuid, db.get_human_user().uuid, "remind me")
    agent = AssistantAgent(agent_uuid=ASSISTANT_UUID, name="assistant", send=lambda _: None)
    agent._decide_next_step = scripted_decisions(
        AssistantStepDecision(reason="remind", action=AssistantActionName.SET_REMINDER,
                              args={"text": tag, "when": "2026-06-27T09:00"}),
        AssistantStepDecision(reason="reply", action=AssistantActionName.REPLY,
                              args={"message": "proposed", "audit": "OK"}),
    )
    try:
        agent.handle(uuid4(), {"room_uuid": str(chatroom.uuid)})
        intent = db.db.session.query(AssistantWriteIntent).filter(
            AssistantWriteIntent.room_uuid == chatroom.uuid).one()
        assert intent.state == "proposed"
        assert intent.preview_text.startswith("Would remind you at")
        assert _jobs_with(tag) == []  # confirm-tier: nothing scheduled inline
        # Confirm executes and schedules.
        obs = execute_write_intent(intent.uuid)
        assert obs.ok is True
        assert len(_jobs_with(tag)) == 1
        assert db.get_write_intent(intent.uuid).state == "completed"
    finally:
        _cleanup_cron(tag)
        db.db.session.query(AssistantWriteIntent).filter(
            AssistantWriteIntent.room_uuid == chatroom.uuid).delete()
        db.db.session.query(AssistantRun).filter(
            AssistantRun.room_uuid == chatroom.uuid).delete()
        db.db.session.query(db.Chatroom).filter(db.Chatroom.uuid == chatroom.uuid).delete()
        db.db.session.commit()


def test_one_shot_fires_once_then_retires(app_ctx):
    tag = f"rem-{uuid4()}"
    chatroom = _room()
    past = datetime.now(UTC) - timedelta(minutes=1)
    job = db.cron_create_one_shot_message(
        message=f"⏰ Reminder: {tag}", fire_at=past, target=str(chatroom.uuid),
    )
    try:
        fired = db.cron_tick(now=datetime.now(UTC))
        assert fired >= 1
        db.db.session.refresh(job)
        assert job.enabled is False  # retired after its single fire
        texts = [m.text for m in db.db.session.query(ChatMessage).filter_by(
            room_uuid=chatroom.uuid).all()]
        assert any(tag in t for t in texts)
        # A second tick does not fire it again.
        before = db.db.session.query(CronRun).filter_by(cron_uuid=job.uuid).count()
        db.cron_tick(now=datetime.now(UTC))
        assert db.db.session.query(CronRun).filter_by(cron_uuid=job.uuid).count() == before
    finally:
        _cleanup_cron(tag)
        db.db.session.query(ChatMessage).filter_by(room_uuid=chatroom.uuid).delete()
        db.db.session.query(db.Chatroom).filter(db.Chatroom.uuid == chatroom.uuid).delete()
        db.db.session.commit()


def test_set_reminder_records_origin_from_step(app_ctx):
    from agents.config import ASSISTANT_UUID as _A
    chatroom = _room()
    room = chatroom.uuid
    run = db.start_assistant_run(journal_id=uuid4(), room_uuid=room, agent_uuid=_A)
    step = db.append_assistant_step(
        run_uuid=run.uuid, step_index=0, phase="observed", action="set_reminder")
    tag = f"rem-{uuid4()}"
    ctx = AssistantActionContext(
        journal_id=None, room_uuid=room, agent_uuid=_A, step_index=0,
        step_uuid=step.uuid,
    )
    obs = _action_set_reminder(ctx, {"text": tag, "when": "2026-06-29T09:00"})
    try:
        assert obs.ok is True
        job = _jobs_with(tag)[0]
        assert job.origin_run_uuid == run.uuid
        assert job.origin_step_uuid == step.uuid
    finally:
        _cleanup_cron(tag)
        db.db.session.query(db.AssistantRun).filter(db.AssistantRun.uuid == run.uuid).delete()
        db.db.session.query(db.Chatroom).filter(db.Chatroom.uuid == chatroom.uuid).delete()
        db.db.session.commit()


def test_proposal_meta_attached_to_reply(app_ctx):
    tag = f"rem-{uuid4()}"
    chatroom = _room()
    db.post_chat_message(chatroom.uuid, db.get_human_user().uuid, "remind me")
    agent = AssistantAgent(agent_uuid=ASSISTANT_UUID, name="assistant", send=lambda _: None)
    agent._decide_next_step = scripted_decisions(
        AssistantStepDecision(reason="remind", action=AssistantActionName.SET_REMINDER,
                              args={"text": tag, "when": "2026-06-29T09:00"}),
        AssistantStepDecision(reason="reply", action=AssistantActionName.REPLY,
                              args={"message": "awaits your confirmation", "audit": "OK"}),
    )
    try:
        agent.handle(uuid4(), {"room_uuid": str(chatroom.uuid)})
        msgs = db.list_room_messages(chatroom.uuid)
        reply = next(m for m in msgs
                     if m["sender_type"] == "agent" and m["kind"] == "message"
                     and m["meta"].get("write_intent"))
        assert reply["meta"]["capability"] == "set_reminder"
        assert reply["meta"]["step_link"].startswith("/assistant?id=")
        assert "#step-" in reply["meta"]["step_link"]
        # The intent the card points at really exists and is proposed.
        assert reply["meta"]["intent_state"] == "proposed"
    finally:
        db.db.session.query(db.AssistantWriteIntent).filter(
            db.AssistantWriteIntent.room_uuid == chatroom.uuid).delete()
        db.db.session.query(db.AssistantRun).filter(
            db.AssistantRun.room_uuid == chatroom.uuid).delete()
        db.db.session.query(db.Chatroom).filter(db.Chatroom.uuid == chatroom.uuid).delete()
        db.db.session.commit()
