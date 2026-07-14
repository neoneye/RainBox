"""Behavior tests for the /find page and its search API (db.find_uuid over
HTTP): paste a uuid or fragment, get kind/name/parents/url matches back."""

from uuid import UUID

import pytest

import db


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


@pytest.fixture
def board(app_ctx):
    b = db.kanban_create_board("find-page board")
    try:
        yield b
    finally:
        db.kanban_delete_board(UUID(b["uuid"]))


def _client():
    from webapp.core import app as flask_app
    return flask_app.test_client()


def test_find_page_renders(app_ctx):
    resp = _client().get("/find")
    assert resp.status_code == 200
    assert b"find-q" in resp.data  # the search input is on the page
    # The url syncs to the search (?q=) both ways: read on load, written on
    # search — the address bar is a permanent link to the current search.
    assert b"fSyncUrl" in resp.data and b"replaceState" in resp.data
    assert b"get('q')" in resp.data


def test_search_api_resolves_a_fragment(board):
    prefix = UUID(board["uuid"]).hex[:10]
    resp = _client().get(f"/find/api/search?q={prefix}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    (m,) = [m for m in data["matches"] if m["uuid"] == board["uuid"]]
    assert m["kind"] == "kanban board" and m["name"] == "find-page board"
    assert m["url"] == f"/kanban?id={board['uuid']}"


def test_search_api_refuses_short_query(app_ctx):
    resp = _client().get("/find/api/search?q=7d")
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False
