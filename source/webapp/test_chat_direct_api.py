"""HTTP API tests for direct LLM chat rooms: room creation with room_type,
the direct-chat trigger on message post, message editing, room settings,
and the /chat/api/models listing."""

from uuid import UUID, uuid4

import pytest

import db
from agents.config import DIRECT_CHAT_UUID


@pytest.fixture
def client():
    app = db.make_app()
    db.init_db(app)
    import webapp.core as webapp_core
    return webapp_core.app.test_client(), webapp_core.app


def _cleanup_room(room_uuid):
    db.db.session.query(db.Chatroom).filter(
        db.Chatroom.uuid == room_uuid
    ).delete()
    db.db.session.commit()


def _drain_direct_inbox():
    """Remove (and return) pending inbox rows for the direct-chat agent so
    tests don't leave work behind for a running supervisor."""
    rows = (
        db.db.session.query(db.Inbox)
        .filter(db.Inbox.agent_uuid == DIRECT_CHAT_UUID)
        .all()
    )
    payloads = [r.payload for r in rows]
    for r in rows:
        db.db.session.delete(r)
    db.db.session.commit()
    return payloads


@pytest.fixture
def direct_room(client):
    _c, app = client
    with app.app_context():
        human = db.get_human_user()
        assert human is not None
        room = db.create_chatroom(
            f"direct-api-{uuid4().hex[:6]}", human.uuid, [DIRECT_CHAT_UUID],
            room_type="direct",
        )
        room_uuid = room.uuid
        _drain_direct_inbox()
        try:
            yield room_uuid, human.uuid
        finally:
            _drain_direct_inbox()
            _cleanup_room(room_uuid)


@pytest.fixture
def agents_room(client):
    _c, app = client
    with app.app_context():
        human = db.get_human_user()
        assert human is not None
        room = db.create_chatroom(f"agents-api-{uuid4().hex[:6]}", human.uuid, [])
        room_uuid = room.uuid
        try:
            yield room_uuid, human.uuid
        finally:
            _cleanup_room(room_uuid)


def test_create_direct_room_members(client):
    test_client, app = client
    resp = test_client.post(
        "/chat/api/rooms",
        json={"name": f"direct-create-{uuid4().hex[:6]}", "room_type": "direct",
              "member_uuids": [str(uuid4())]},  # ignored for direct rooms
    )
    assert resp.status_code == 201
    room_uuid = UUID(resp.get_json()["uuid"])
    with app.app_context():
        try:
            members = set(db.get_room_member_uuids(room_uuid))
            human = db.get_human_user()
            assert members == {human.uuid, DIRECT_CHAT_UUID}
            assert db.get_chatroom(room_uuid).room_type == "direct"
        finally:
            _cleanup_room(room_uuid)


def test_create_room_rejects_bad_room_type(client):
    test_client, _app = client
    resp = test_client.post(
        "/chat/api/rooms", json={"name": "x", "room_type": "bogus"}
    )
    assert resp.status_code == 400


def test_post_in_direct_room_enqueues_direct_chat(client, direct_room):
    test_client, app = client
    room_uuid, _human = direct_room
    resp = test_client.post(
        f"/chat/api/rooms/{room_uuid}/messages", json={"text": "hi model"}
    )
    assert resp.status_code == 201
    with app.app_context():
        payloads = _drain_direct_inbox()
        assert len(payloads) == 1
        assert str(room_uuid) in payloads[0]


def test_post_in_agents_room_does_not_enqueue_direct_chat(client, agents_room):
    test_client, app = client
    room_uuid, _human = agents_room
    resp = test_client.post(
        f"/chat/api/rooms/{room_uuid}/messages", json={"text": "hi agents"}
    )
    assert resp.status_code == 201
    with app.app_context():
        assert _drain_direct_inbox() == []


def test_model_reply_does_not_retrigger(client, direct_room):
    """A post authored by the direct-chat agent itself (sender_uuid set) must
    not enqueue another turn — the human-only guard."""
    test_client, app = client
    room_uuid, _human = direct_room
    resp = test_client.post(
        f"/chat/api/rooms/{room_uuid}/messages",
        json={"text": "model says hi", "sender_uuid": str(DIRECT_CHAT_UUID)},
    )
    assert resp.status_code == 201
    with app.app_context():
        assert _drain_direct_inbox() == []


def test_edit_message_in_direct_room(client, direct_room):
    test_client, app = client
    room_uuid, human_uuid = direct_room
    with app.app_context():
        msg = db.post_chat_message(room_uuid, human_uuid, "original")
        msg_id = msg.id
    resp = test_client.put(
        f"/chat/api/rooms/{room_uuid}/messages/{msg_id}",
        json={"text": "edited"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["text"] == "edited"
    with app.app_context():
        assert db.get_room_message(room_uuid, msg_id)["text"] == "edited"


def test_edit_message_rejected_in_agents_room(client, agents_room):
    test_client, app = client
    room_uuid, human_uuid = agents_room
    with app.app_context():
        msg = db.post_chat_message(room_uuid, human_uuid, "original")
        msg_id = msg.id
    resp = test_client.put(
        f"/chat/api/rooms/{room_uuid}/messages/{msg_id}",
        json={"text": "edited"},
    )
    assert resp.status_code == 403


def test_delete_message_in_direct_room(client, direct_room):
    test_client, app = client
    room_uuid, human_uuid = direct_room
    with app.app_context():
        msg = db.post_chat_message(room_uuid, human_uuid, "delete me")
        msg_id = msg.id
    resp = test_client.delete(f"/chat/api/rooms/{room_uuid}/messages/{msg_id}")
    assert resp.status_code == 200
    assert resp.get_json() == {"id": msg_id, "deleted": True}
    with app.app_context():
        assert db.get_room_message(room_uuid, msg_id) is None


def test_delete_notice_message_in_direct_room(client, direct_room):
    """Non-'message' kinds (e.g. the 'no model selected' notice) are
    deletable too — the operator can clear the whole transcript."""
    test_client, app = client
    room_uuid, _human = direct_room
    with app.app_context():
        from agents.config import DIRECT_CHAT_UUID
        notice = db.post_chat_message(
            room_uuid, DIRECT_CHAT_UUID, "No model selected.", kind="notice"
        )
        notice_id = notice.id
        _drain_direct_inbox()
    resp = test_client.delete(
        f"/chat/api/rooms/{room_uuid}/messages/{notice_id}"
    )
    assert resp.status_code == 200
    assert resp.get_json() == {"id": notice_id, "deleted": True}
    with app.app_context():
        assert db.get_room_message(room_uuid, notice_id) is None


def test_delete_message_rejected_in_agents_room(client, agents_room):
    test_client, app = client
    room_uuid, human_uuid = agents_room
    with app.app_context():
        msg = db.post_chat_message(room_uuid, human_uuid, "not deletable")
        msg_id = msg.id
    resp = test_client.delete(f"/chat/api/rooms/{room_uuid}/messages/{msg_id}")
    assert resp.status_code == 403
    with app.app_context():
        assert db.get_room_message(room_uuid, msg_id) is not None


def test_delete_message_missing_message_404(client, direct_room):
    test_client, _app = client
    room_uuid, _human = direct_room
    resp = test_client.delete(
        f"/chat/api/rooms/{room_uuid}/messages/999999999"
    )
    assert resp.status_code == 404


def test_edit_message_missing_message_404(client, direct_room):
    test_client, _app = client
    room_uuid, _human = direct_room
    resp = test_client.put(
        f"/chat/api/rooms/{room_uuid}/messages/999999999",
        json={"text": "edited"},
    )
    assert resp.status_code == 404


def test_feedback_rejected_in_direct_room(client, direct_room):
    """Feedback rates responder agents; a direct room has none, so even a
    hand-crafted request against the model's reply is refused."""
    test_client, app = client
    room_uuid, _human = direct_room
    with app.app_context():
        msg = db.post_chat_message(room_uuid, DIRECT_CHAT_UUID, "model reply")
        msg_uuid = msg.uuid
    resp = test_client.post(
        f"/chat/api/messages/{msg_uuid}/feedback", json={"rating": "upvote"}
    )
    assert resp.status_code == 400


def test_settings_get_and_put(client, direct_room):
    test_client, app = client
    room_uuid, _human = direct_room
    resp = test_client.get(f"/chat/api/rooms/{room_uuid}/settings")
    assert resp.status_code == 200
    body = resp.get_json()
    # default_model_uuid mirrors the global chat.default_model setting, whose
    # dynamic default depends on the shared DB's overrides — shape-check only.
    assert body.pop("default_model_uuid", "missing") != "missing"
    assert body == {
        "room_type": "direct", "system_prompt": "", "model_uuid": None,
        "prompt_uuid": None, "prompt_name": None, "prompt_exists": None,
        "request_timeout": None,
    }
    with app.app_context():
        cfg = db.create_model_config(f"direct-test-model-{uuid4().hex[:6]}", {})
        cfg_uuid = cfg.uuid
    try:
        resp = test_client.put(
            f"/chat/api/rooms/{room_uuid}/settings",
            json={"system_prompt": "Be brief.", "model_uuid": str(cfg_uuid)},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["system_prompt"] == "Be brief."
        assert body["model_uuid"] == str(cfg_uuid)
        # Clearing the model.
        resp = test_client.put(
            f"/chat/api/rooms/{room_uuid}/settings", json={"model_uuid": None}
        )
        assert resp.get_json()["model_uuid"] is None
        assert resp.get_json()["system_prompt"] == "Be brief."
    finally:
        with app.app_context():
            db.db.session.query(db.ModelConfig).filter(
                db.ModelConfig.uuid == cfg_uuid
            ).delete()
            db.db.session.commit()


def test_settings_request_timeout(client, direct_room):
    """The per-room reply-timeout override: positive int or null, anything
    else is a 400; PUTting null clears it back to the model config default."""
    test_client, _app = client
    room_uuid, _human = direct_room
    url = f"/chat/api/rooms/{room_uuid}/settings"

    resp = test_client.put(url, json={"request_timeout": 300})
    assert resp.status_code == 200
    assert resp.get_json()["request_timeout"] == 300
    assert test_client.get(url).get_json()["request_timeout"] == 300

    for bad in (0, -5, "300", 12.5, True):
        resp = test_client.put(url, json={"request_timeout": bad})
        assert resp.status_code == 400, bad

    resp = test_client.put(url, json={"request_timeout": None})
    assert resp.status_code == 200
    assert resp.get_json()["request_timeout"] is None


def test_settings_prompt_link_flow(client, direct_room):
    test_client, app = client
    room_uuid, _human = direct_room
    with app.app_context():
        from db.models import Prompt
        row = Prompt(uuid=uuid4(), name="Pirate", content="Arr.")
        db.db.session.add(row)
        db.db.session.commit()
        prompt_uuid = row.uuid
    try:
        # Link the stored prompt; the GET shape carries name + existence so
        # the sidebar can label the link without a second request.
        resp = test_client.put(
            f"/chat/api/rooms/{room_uuid}/settings",
            json={"prompt_uuid": str(prompt_uuid)},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["prompt_uuid"] == str(prompt_uuid)
        assert body["prompt_name"] == "Pirate"
        assert body["prompt_exists"] is True
        # Deleting the linked version: reported, not an error.
        with app.app_context():
            from db.models import Prompt
            db.db.session.query(Prompt).filter(
                Prompt.uuid == prompt_uuid).delete()
            db.db.session.commit()
        body = test_client.get(f"/chat/api/rooms/{room_uuid}/settings").get_json()
        assert body["prompt_uuid"] == str(prompt_uuid)
        assert body["prompt_exists"] is False
        assert body["prompt_name"] is None
        # Unlink.
        resp = test_client.put(
            f"/chat/api/rooms/{room_uuid}/settings", json={"prompt_uuid": None}
        )
        assert resp.get_json()["prompt_uuid"] is None
    finally:
        with app.app_context():
            from db.models import Prompt
            db.db.session.query(Prompt).filter(
                Prompt.uuid == prompt_uuid).delete()
            db.db.session.commit()


def test_settings_put_rejects_unknown_prompt(client, direct_room):
    test_client, _app = client
    room_uuid, _human = direct_room
    resp = test_client.put(
        f"/chat/api/rooms/{room_uuid}/settings",
        json={"prompt_uuid": str(uuid4())},
    )
    assert resp.status_code == 400


def test_settings_put_rejects_agents_room(client, agents_room):
    test_client, _app = client
    room_uuid, _human = agents_room
    resp = test_client.put(
        f"/chat/api/rooms/{room_uuid}/settings", json={"system_prompt": "x"}
    )
    assert resp.status_code == 400


def test_settings_put_rejects_unknown_model(client, direct_room):
    test_client, _app = client
    room_uuid, _human = direct_room
    resp = test_client.put(
        f"/chat/api/rooms/{room_uuid}/settings",
        json={"model_uuid": str(uuid4())},
    )
    assert resp.status_code == 400


def test_models_listing(client):
    test_client, app = client
    with app.app_context():
        cfg = db.create_model_config(f"direct-list-model-{uuid4().hex[:6]}", {})
        cfg_uuid = cfg.uuid
    try:
        resp = test_client.get("/chat/api/models")
        assert resp.status_code == 200
        models = resp.get_json()
        entry = next(m for m in models if m["uuid"] == str(cfg_uuid))
        assert "label" in entry and "available" in entry
    finally:
        with app.app_context():
            db.db.session.query(db.ModelConfig).filter(
                db.ModelConfig.uuid == cfg_uuid
            ).delete()
            db.db.session.commit()
