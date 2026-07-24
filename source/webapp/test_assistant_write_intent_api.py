"""HTTP tests for the confirm/reject endpoints behind a proposed assistant
write intent (the operator's approval surface for confirm-tier writes)."""

from uuid import uuid4

import pytest

import db
from db import AssistantRun, MemoryClaim
from agents.assistant import AssistantActionName, AssistantAgent, AssistantStepDecision
from agents.assistant_fakes import scripted_decisions
from agents.config import ASSISTANT_UUID


@pytest.fixture
def client():
    app = db.make_app()
    db.init_db(app)
    import webapp.core as webapp_core
    return webapp_core.app.test_client(), webapp_core.app


def _propose_activation(app):
    """Drive the assistant to propose activating a fresh candidate; return
    (intent_uuid, candidate_uuid, room_uuid)."""
    with app.app_context():
        human = db.get_human_user()
        room = db.create_chatroom(f"wi-api-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])
        db.post_chat_message(room.uuid, human.uuid, "remember and activate")
        cand = db.create_memory_claim(
            scope="global", kind="fact", text=f"api fact {uuid4().hex[:6]}",
            confidence=0.5, status="candidate", sensitivity="public", subject="wi-api",
        )
        agent = AssistantAgent(agent_uuid=ASSISTANT_UUID, name="assistant", send=lambda _: None)
        agent._decide_next_step = scripted_decisions(
            AssistantStepDecision(reason="propose", action=AssistantActionName.MEMORY_ACTIVATE,
                                  args={"memory_uuid": str(cand.uuid)}),
            AssistantStepDecision(reason="done", action=AssistantActionName.REPLY,
                                  args={"1_message": "proposed", "2_audit": "OK"}),
        )
        result = agent.handle(uuid4(), {"room_uuid": str(room.uuid)})
        from db import AssistantWriteIntent
        intent = (
            db.db.session.query(AssistantWriteIntent)
            .filter(AssistantWriteIntent.run_uuid == result["assistant_run_uuid"]).one()
        )
        return intent.uuid, cand.uuid, room.uuid


def _cleanup(app, room_uuid):
    with app.app_context():
        db.db.session.query(AssistantRun).filter(AssistantRun.room_uuid == room_uuid).delete()
        db.db.session.query(MemoryClaim).filter(MemoryClaim.subject == "wi-api").delete()
        db.db.session.query(db.Chatroom).filter(db.Chatroom.uuid == room_uuid).delete()
        db.db.session.commit()


def test_confirm_endpoint_executes_the_write(client):
    flask_client, app = client
    intent_uuid, cand_uuid, room_uuid = _propose_activation(app)
    try:
        resp = flask_client.post(f"/chat/api/assistant/write-intents/{intent_uuid}/confirm")
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True
        with app.app_context():
            assert db.get_memory_claim(cand_uuid).status == "active"
    finally:
        _cleanup(app, room_uuid)


def test_reject_endpoint_declines_the_write(client):
    flask_client, app = client
    intent_uuid, cand_uuid, room_uuid = _propose_activation(app)
    try:
        resp = flask_client.post(f"/chat/api/assistant/write-intents/{intent_uuid}/reject")
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True            # first reject succeeds
        with app.app_context():
            assert db.get_memory_claim(cand_uuid).status == "candidate"  # untouched
        # A second (stale/double-clicked) reject is no longer 'proposed' → ok:false
        # so the UI flags it instead of falsely reporting success.
        again = flask_client.post(f"/chat/api/assistant/write-intents/{intent_uuid}/reject")
        assert again.status_code == 200
        assert again.get_json()["ok"] is False
    finally:
        _cleanup(app, room_uuid)
