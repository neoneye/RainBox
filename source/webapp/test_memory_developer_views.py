"""Markup/marker tests for webapp/memory_developer_views.py +
static/memory_developer.js.

The /memory/developer page is frontend-only: the route renders the HTML shell
(+ inline CSS) and the interactivity lives in static/memory_developer.js.
`_body()` concatenates the rendered page with the served JS, so a marker
assertion covers both regardless of which side the marker lives on (same
approach as test_memory_views.py).

The query API itself needs live embeddings + LLMs, so only its input
validation is tested here.
"""

from webapp.core import app  # noqa: F401  ensure routes register
import webapp  # noqa: F401  registers memory_developer_views on the shared app


def _body() -> str:
    client = app.test_client()
    page = client.get("/memory/developer").get_data(as_text=True)
    js = client.get("/static/memory_developer.js")
    assert js.status_code == 200  # the shell references it; it must serve
    return page + js.get_data(as_text=True)


def test_memory_developer_page_renders_with_nav():
    resp = app.test_client().get("/memory/developer")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "pp-nav" in body                              # shared nav included
    assert "/static/memory_developer.js?v=" in body      # cache-busted JS include


def test_page_has_query_input_and_panels():
    body = app.test_client().get("/memory/developer").get_data(as_text=True)
    assert 'id="memdev-query"' in body
    assert 'id="memdev-run"' in body
    assert 'id="memdev-assistant-out"' in body
    assert 'id="memdev-router-out"' in body
    assert "memory_query" in body
    assert "query_filter_router" in body


def test_js_posts_to_the_query_api():
    body = _body()
    assert "/memory/api/developer/query" in body
    assert "memdevRenderAssistant" in body
    assert "memdevRenderRouter" in body


def test_js_renders_assistant_recall_filter_debug():
    # The assistant panel shows what its seed LLM filter kept/dropped (and a
    # mode badge when it degraded to the gated retrieval).
    body = _body()
    assert "recall_filter" in body
    assert "recalled candidates + LLM filter" in body


def test_page_has_models_overview_section():
    # After a run the page shows which models each stage used (embedding,
    # filter scorer per panel, route reply group) — apples-vs-oranges guard.
    body = _body()
    assert 'id="memdev-models"' in body
    assert "memdevRenderModels" in body
    assert "filter scorer" in body
    assert "embedding" in body


def test_js_has_no_python_interpreted_escapes():
    # The shell is a non-raw Python string; the JS lives in a static file
    # precisely so backslash escapes survive. Guard the split: the inline
    # template must stay free of backslashes.
    from webapp.memory_developer_views import MEMORY_DEVELOPER_TEMPLATE
    assert "\\" not in MEMORY_DEVELOPER_TEMPLATE


def test_query_api_requires_a_query():
    client = app.test_client()
    resp = client.post("/memory/api/developer/query", json={})
    assert resp.status_code == 400
    assert "query" in resp.get_json()["error"]
    resp = client.post("/memory/api/developer/query", json={"query": "   "})
    assert resp.status_code == 400


def test_signal_budgets_parse_defaults_and_clamp():
    from webapp.memory_developer_views import TOP_K_MAX, _parse_signal_budgets
    assert _parse_signal_budgets({}) == (5, 5)   # defaults = TOP_K_VECTOR/FULLTEXT
    assert _parse_signal_budgets({"top_k_vector": "9"}) == (9, 5)
    assert _parse_signal_budgets({"top_k_fulltext": 0}) == (5, 0)  # 0 disables
    assert _parse_signal_budgets(
        {"top_k_vector": 999, "top_k_fulltext": -3}) == (TOP_K_MAX, 0)
    assert _parse_signal_budgets({"top_k_vector": "junk"}) == (5, 5)


def test_room_uuid_parses_all_rooms_and_tolerates_garbage():
    from uuid import UUID
    from webapp.memory_developer_views import _parse_room_uuid
    assert _parse_room_uuid({}) == (None, False)
    assert _parse_room_uuid({"room_uuid": ""}) == (None, False)
    assert _parse_room_uuid({"room_uuid": "*"}) == (None, True)
    assert _parse_room_uuid({"room_uuid": "not-a-uuid"}) == (None, False)
    assert _parse_room_uuid(
        {"room_uuid": "795ea3ee-9426-4e03-973a-5d6f6c814b46"}) == (UUID(
        "795ea3ee-9426-4e03-973a-5d6f6c814b46"), False)


def test_page_has_room_selector_persisted_in_localstorage():
    body = _body()
    assert 'id="memdev-room"' in body
    assert "(all rooms)" in body
    assert 'value="*" selected' in body     # operator view is the default
    assert "(no room)" in body
    assert "memoryDeveloper.roomUuid" in body
    assert "/chat/api/rooms" in body


def test_page_has_per_signal_budget_knobs_persisted_in_localstorage():
    body = _body()
    assert 'id="memdev-topk-vector"' in body
    assert 'id="memdev-topk-fulltext"' in body
    assert "memoryDeveloper.topKVector" in body    # localStorage persistence
    assert "memoryDeveloper.topKFulltext" in body
    assert "top_k_vector" in body                  # sent to the API
    assert "top_k_fulltext" in body


def test_nav_memory_dropdown_links_here():
    body = app.test_client().get("/memory/developer").get_data(as_text=True)
    assert ">Memory &#9662;<" in body
    assert "/memory/developer" in body
