"""Tests for webapp/personality_views.py + static/personality.js.

The page is frontend-only: the route renders the HTML shell (+ inline CSS) and
all interactivity lives in static/personality.js. `_body()` returns the page
concatenated with the served JS so marker assertions cover both.
"""
from webapp.core import app


def _body() -> str:
    client = app.test_client()
    page = client.get("/personality").get_data(as_text=True)
    js = client.get("/static/personality.js")
    assert js.status_code == 200  # the shell references it; it must serve
    return page + js.get_data(as_text=True)


def test_page_renders_with_nav():
    body = app.test_client().get("/personality").get_data(as_text=True)
    assert 'class="personality-split"' in body
    assert "pp-nav" in body
    assert "/static/personality.js?v=" in body


def test_nav_has_personality_link():
    body = app.test_client().get("/personality").get_data(as_text=True)
    assert ">Personality<" in body
    assert "pp-active" in body


def test_page_has_editor_and_history_markers():
    body = app.test_client().get("/personality").get_data(as_text=True)
    for marker in ['id="personality-content"', 'id="personality-history"',
                   'id="personality-revcount"', 'id="personality-history-btn"',
                   'id="personality-new-modal"', 'id="personality-delete-modal"',
                   'id="personality-restore-modal"']:
        assert marker in body, f"missing page marker: {marker}"
