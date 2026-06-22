"""Markup/marker tests for webapp/memory_views.py + static/memory.js.

The /memory page is frontend-only: the route renders the HTML shell (+ inline
CSS) and the interactivity lives in static/memory.js. `_body()` concatenates the
rendered page with the served JS, so a marker assertion covers both regardless
of which side the marker lives on (same approach as test_cron_views.py)."""

from webapp.core import app  # noqa: F401  ensure routes register
import webapp  # noqa: F401  registers memory_views/api on the shared app


def _body() -> str:
    client = app.test_client()
    page = client.get("/memory").get_data(as_text=True)
    js = client.get("/static/memory.js")
    assert js.status_code == 200  # the shell references it; it must serve
    return page + js.get_data(as_text=True)


def test_memory_page_renders_with_nav():
    resp = app.test_client().get("/memory")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert 'class="mem-split"' in body          # split layout
    assert "pp-nav" in body                      # shared nav included
    assert "/static/memory.js?v=" in body        # cache-busted JS include


def test_nav_has_memory_link():
    body = app.test_client().get("/memory").get_data(as_text=True)
    assert ">Memory<" in body
    assert "pp-active" in body


def test_sidebar_has_all_node_and_filters():
    body = app.test_client().get("/memory").get_data(as_text=True)
    assert 'id="mem-all"' in body                # static "All memories" root node
    assert ">All memories<" in body
    assert 'id="mem-filter-text"' in body        # filter bar
    assert 'id="mem-filter-scope"' in body
    assert 'id="mem-tree-root"' in body          # the rendered facet tree mounts here


def test_detail_and_table_panes_present():
    body = app.test_client().get("/memory").get_data(as_text=True)
    assert 'id="mem-detail"' in body
    assert 'id="mem-rows"' in body               # flat table body


def test_modals_present():
    body = _body()
    for el in ('mem-correct-modal', 'mem-sens-modal', 'mem-expiry-modal',
               'mem-reject-modal', 'ui-modal-backdrop'):
        assert 'id="' + el + '"' in body, el


def test_js_wires_facets_and_actions():
    body = _body()
    # grouping seam + the lifecycle actions the page drives
    assert 'function groupClaims' in body
    assert "STATUS_ORDER" in body
    for action in ('activate', 'reject', 'reactivate', 'correct', 'sensitivity', 'expiry'):
        assert "'" + action + "'" in body, action
    # per-row optimistic concurrency token is sent on mutations
    assert "expected_updated_at" in body
