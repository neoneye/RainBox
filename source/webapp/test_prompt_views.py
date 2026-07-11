"""Tests for webapp/prompt_views.py + static/prompt.js.

The /prompt page is frontend-only: the route renders the HTML shell (+ inline
CSS) and all interactivity lives in static/prompt.js. `_body()` returns the
page concatenated with the served JS so marker assertions cover both.
"""
from webapp.core import app


def _body() -> str:
    client = app.test_client()
    page = client.get("/prompt").get_data(as_text=True)
    js = client.get("/static/prompt.js")
    assert js.status_code == 200  # the shell references it; it must serve
    return page + js.get_data(as_text=True)


def test_prompt_page_renders_with_nav():
    body = app.test_client().get("/prompt").get_data(as_text=True)
    assert 'class="prompt-split"' in body   # the prompt page layout
    assert "pp-nav" in body                 # shared nav included
    assert "/static/prompt.js?v=" in body   # JS pulled in with a cache-buster


def test_nav_has_prompts_link():
    body = app.test_client().get("/prompt").get_data(as_text=True)
    assert ">Prompt<" in body
    assert "pp-active" in body


def test_page_has_editor_markers():
    body = app.test_client().get("/prompt").get_data(as_text=True)
    for marker in ['id="prompt-content"', 'id="prompt-diff"',
                   'id="prompt-based-on"', 'id="prompt-diff-against"',
                   'id="prompt-new-modal"', 'id="prompt-delete-modal"']:
        assert marker in body, f"missing page marker: {marker}"


def test_js_has_core_markers():
    b = _body()
    for marker in ["promptLoadTree", "promptRenderTree", "promptItemNode",
                   "promptCloneUuid", "promptLoadDiff", "promptContentPush",
                   "promptSavePush", "/prompt/api/tree"]:
        assert marker in b, f"missing JS marker: {marker}"
