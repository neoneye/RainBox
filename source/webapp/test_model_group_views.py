"""Marker tests for webapp/model_group_views.py (the /modelgroup page shell;
its JS is inline in the rendered template, so a GET carries both markup and
behavior — same idea as test_chat_views)."""

from webapp.core import app  # noqa: F401  ensure routes register
import webapp  # noqa: F401  registers model_group_views on the shared app


def _body() -> str:
    return app.test_client().get("/modelgroup").get_data(as_text=True)


def test_legacy_plural_path_redirects_to_singular():
    # The page moved from /modelgroups to /modelgroup (singular, like /cron
    # and /kanban); old links redirect and keep their ?id= selection.
    resp = app.test_client().get("/modelgroups?id=abc")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/modelgroup?id=abc")


def test_group_rows_are_real_links():
    # Group rows in the left list are anchors with a real href so CMD/Ctrl
    # click (and middle click) opens the group in a new tab via its ?id= deep
    # link. Plain clicks are still intercepted for in-page selection; modified
    # clicks pass through to the browser.
    body = _body()
    assert 'href="?id=${encodeURIComponent(g.uuid)}"' in body
    assert "if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;" in body
