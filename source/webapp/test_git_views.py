"""Tests for webapp/git_views.py + static/git.js.

The /git page is frontend-only: the route renders the HTML shell (+ inline CSS)
and all interactivity lives in static/git.js. `_body()` returns the page
concatenated with the served JS so marker assertions cover both.
"""
from webapp.core import app


def _body() -> str:
    client = app.test_client()
    page = client.get("/git").get_data(as_text=True)
    js = client.get("/static/git.js")
    assert js.status_code == 200  # the shell references it; it must serve
    return page + js.get_data(as_text=True)


def test_git_page_renders_with_nav():
    body = app.test_client().get("/git").get_data(as_text=True)
    assert 'class="git-split"' in body   # the git page layout
    assert "pp-nav" in body              # shared nav included
    assert "/static/git.js?v=" in body   # JS pulled in with a cache-buster


def test_nav_has_git_link():
    body = app.test_client().get("/git").get_data(as_text=True)
    assert ">Git<" in body
    assert "pp-active" in body


def test_js_has_core_markers():
    b = _body()
    for marker in ["gitLoadTree", "gitRenderTree", "gitRepoNode",
                   "gitAddRepoConfirm", "/git/api/check-path",
                   "gitLoadRepoDetail", "gitSavePush"]:
        assert marker in b, f"missing JS marker: {marker}"
