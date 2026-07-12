"""HTTP API tests for /chat/api/rooms/<uuid>/export — the Export sidebar's
JSON document: full vs minimal metadata, the last-N limit, and the
credential redaction in the model parameters."""

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


@pytest.fixture
def direct_room(client):
    """A direct room with three turns: human, model, and a notice row."""
    _c, app = client
    with app.app_context():
        human = db.get_human_user()
        assert human is not None
        room = db.create_chatroom(
            f"export-api-{uuid4().hex[:6]}", human.uuid, [DIRECT_CHAT_UUID],
            room_type="direct",
        )
        room_uuid = room.uuid
        db.post_chat_message(room_uuid, human.uuid, "hello model")
        db.post_chat_message(room_uuid, DIRECT_CHAT_UUID, "hello human")
        db.post_chat_message(room_uuid, DIRECT_CHAT_UUID, "pick a model first",
                             kind="notice")
        try:
            yield room_uuid, human.uuid
        finally:
            _cleanup_room(room_uuid)


def test_export_full_metadata(client, direct_room):
    test_client, _app = client
    room_uuid, human_uuid = direct_room
    resp = test_client.get(f"/chat/api/rooms/{room_uuid}/export")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["room"]["uuid"] == str(room_uuid)
    assert data["room"]["room_type"] == "direct"
    assert data["exported_at"]
    assert data["message_count"] == 3
    assert data["total_message_count"] == 3
    msgs = data["messages"]
    assert [m["text"] for m in msgs] == [
        "hello model", "hello human", "pick a model first"]
    first = msgs[0]
    assert UUID(first["uuid"])  # a real per-message uuid
    assert first["sender_uuid"] == str(human_uuid)
    assert first["sender_type"] == "human"
    assert first["sender_name"]
    assert first["timestamp"]
    assert msgs[2]["kind"] == "notice"


def test_export_minimal_metadata(client, direct_room):
    test_client, _app = client
    room_uuid, _human = direct_room
    resp = test_client.get(
        f"/chat/api/rooms/{room_uuid}/export?metadata=minimal")
    assert resp.status_code == 200
    data = resp.get_json()
    assert set(data.keys()) == {"messages"}
    msgs = data["messages"]
    assert msgs[0] == {"role": "user", "text": "hello model"}
    assert msgs[1] == {"role": "assistant", "text": "hello human"}
    # Non-message rows keep their kind so a notice isn't read as a reply,
    # but still carry no uuids/dates/usernames.
    assert msgs[2] == {"role": "assistant", "text": "pick a model first",
                       "kind": "notice"}


def test_export_last_n_limit(client, direct_room):
    test_client, _app = client
    room_uuid, _human = direct_room
    resp = test_client.get(f"/chat/api/rooms/{room_uuid}/export?limit=2")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["message_count"] == 2
    assert data["total_message_count"] == 3
    assert [m["text"] for m in data["messages"]] == [
        "hello human", "pick a model first"]


def test_export_rejects_bad_params(client, direct_room):
    test_client, _app = client
    room_uuid, _human = direct_room
    base = f"/chat/api/rooms/{room_uuid}/export"
    assert test_client.get(base + "?metadata=bogus").status_code == 400
    assert test_client.get(base + "?limit=0").status_code == 400
    assert test_client.get(base + "?limit=abc").status_code == 400


def test_export_unknown_room_404(client):
    test_client, _app = client
    assert test_client.get(
        f"/chat/api/rooms/{uuid4()}/export").status_code == 404


def test_export_model_info_redacts_credentials(client, direct_room):
    """The full export names the room's model and its parameters, but any
    credential-like argument (api_key, tokens, ...) is redacted."""
    test_client, app = client
    room_uuid, _human = direct_room
    with app.app_context():
        cfg = db.ModelConfig(
            provider="lm_studio",
            model_name=f"export-test-{uuid4().hex[:6]}",
            arguments={"api_base": "http://x/v1", "api_key": "sekret",
                       "temperature": 0.2},
        )
        db.db.session.add(cfg)
        db.db.session.commit()
        cfg_uuid = cfg.uuid
        db.set_chatroom_settings(room_uuid, model_uuid=cfg_uuid)
    try:
        resp = test_client.get(f"/chat/api/rooms/{room_uuid}/export")
        assert resp.status_code == 200
        model = resp.get_json()["model"]
        assert model["uuid"] == str(cfg_uuid)
        assert model["provider"] == "lm_studio"
        assert model["model_name"].startswith("export-test-")
        assert model["parameters"]["api_key"] == "[redacted]"
        assert model["parameters"]["api_base"] == "http://x/v1"
        assert model["parameters"]["temperature"] == 0.2
    finally:
        with app.app_context():
            db.db.session.query(db.ModelConfig).filter(
                db.ModelConfig.uuid == cfg_uuid
            ).delete()
            db.db.session.commit()
