"""The /memory/qa page: one Q&A registry entry by id, shields honored."""

import pytest

import db
import memory.seed_memory as seed_memory

ENTRY_ID = "0b1e2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


@pytest.fixture
def client():
    from webapp.core import app as flask_app
    app = db.make_app()
    db.init_db(app)
    with app.app_context():
        yield flask_app.test_client()


@pytest.fixture
def registry(monkeypatch):
    entries = [
        {"id": ENTRY_ID, "path": "garden.shed", "kind": "static",
         "questions": ["What colour is the shed?", "Is the shed painted?"],
         "answer": "The shed is blue.", "_source": "upstream"},
        {"id": "1111aaaa-2222-4333-8444-555566667777", "path": "garden.pond",
         "kind": "static", "questions": ["How deep is the pond?"],
         "answer": "Knee deep.", "shield": "garden-secrets", "_source": "upstream"},
    ]
    monkeypatch.setattr(seed_memory, "_load_jsonl", lambda: entries)
    monkeypatch.setattr(seed_memory, "_overlay_path", lambda: None)
    monkeypatch.setattr(seed_memory, "_unlocked_shields", lambda: set())
    return entries


def test_the_page_shows_the_entry(client, registry):
    body = client.get(f"/memory/qa?id={ENTRY_ID}").get_data(as_text=True)
    assert "garden.shed" in body
    assert "<li>What colour is the shed?</li>" in body
    assert "The shed is blue." in body
    assert str(seed_memory.QA_JSONL_PATH) in body


def test_the_id_matches_whatever_its_case(client, registry):
    body = client.get(f"/memory/qa?id={ENTRY_ID.upper()}").get_data(as_text=True)
    assert "The shed is blue." in body


def test_an_unknown_id_says_so(client, registry):
    body = client.get("/memory/qa?id=nope").get_data(as_text=True)
    assert "No Q&amp;A entry has the id <code>nope</code>." in body


def test_a_locked_entry_reads_as_not_found(client, registry):
    body = client.get(
        "/memory/qa?id=1111aaaa-2222-4333-8444-555566667777").get_data(as_text=True)
    assert "Knee deep." not in body
    assert "No Q&amp;A entry has the id" in body


def test_an_unlocked_entry_is_shown(client, registry, monkeypatch):
    monkeypatch.setattr(seed_memory, "_unlocked_shields", lambda: {"garden-secrets"})
    body = client.get(
        "/memory/qa?id=1111aaaa-2222-4333-8444-555566667777").get_data(as_text=True)
    assert "Knee deep." in body


def test_find_opens_a_q_and_a_entry_on_this_page(client, registry):
    match = db.describe_uuid(ENTRY_ID)
    assert match is not None
    assert match["kind"] == "Q&A entry"
    assert match["url"] == f"/memory/qa?id={ENTRY_ID}"
