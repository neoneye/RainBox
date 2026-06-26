"""Tests for webapp/assistant_overview_views.py + static/assistant-overview.js.

The /assistant-overview page is a frontend shell: the route renders HTML (+
inline CSS) and all interactivity lives in static/assistant-overview.js.
`_body()` concatenates the page with the served JS so marker assertions cover
both."""
from webapp.core import app


def _body() -> str:
    client = app.test_client()
    page = client.get("/assistant-overview").get_data(as_text=True)
    js = client.get("/static/assistant-overview.js")
    assert js.status_code == 200
    return page + js.get_data(as_text=True)


def test_page_renders_with_nav_and_js():
    body = app.test_client().get("/assistant-overview").get_data(as_text=True)
    assert "pp-nav" in body
    assert "/static/assistant-overview.js?v=" in body
    assert 'id="ao-body"' in body
    assert 'id="ao-range-select"' in body  # the time-range picker
    assert ">Any time<" in body
    assert ">Last 7 days<" in body


def test_nav_marks_assistant_active():
    body = app.test_client().get("/assistant-overview").get_data(as_text=True)
    assert "pp-active" in body  # the Assistant link is highlighted here


def test_js_has_core_markers():
    b = _body()
    for marker in ["aoLoad", "aoRender", "aoRenderTabs", "aoRenderPager",
                   "/assistant-overview/api/runs", "/assistant?id="]:
        assert marker in b, f"missing marker: {marker}"
