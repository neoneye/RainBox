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


def test_js_has_core_markers():
    b = _body()
    for marker in ["personalityLoadTree", "personalityRenderTree",
                   "personalityItemNode", "personalitySavePush",
                   "personalityAddPersonalityConfirm", "personalityDeleteItem",
                   "/personality/api/tree"]:
        assert marker in b, f"missing JS marker: {marker}"


def test_tree_save_declares_no_deletes():
    """Per docs/ui-tree-persistence.md the tree PUT cannot delete, so the
    client must not carry a deletes counter — deletion goes to DELETE."""
    b = _body()
    assert "deletes" not in b
    assert "method: 'DELETE'" in b


def test_history_view_markers():
    b = _body()
    for marker in ["function personalityToggleHistory",
                   "function personalityLoadHistory",
                   "function personalityShowRevisionDiff",
                   "function personalityConfirmRestore",
                   "/revisions", "/restore"]:
        assert marker in b, f"missing history marker: {marker}"


def test_content_editing_is_explicit():
    """Personality text is read-only until Edit is clicked; the edit resolves
    only via Save or Cancel, with the rest of the page behind the modal
    backdrop meanwhile — no autosave."""
    b = _body()
    assert 'id="personality-edit-btn"' in b
    assert 'id="personality-save-btn"' in b
    assert 'id="personality-cancel-btn"' in b
    assert "function personalityStartEdit" in b
    assert "function personalitySaveEdit" in b
    assert "function personalityCancelEdit" in b
    assert "#personality-editor.editing{position:relative;z-index:1600" in b


def test_find_resolves_a_personality_uuid():
    import db
    c = app.test_client()
    made = c.post("/personality/api/personalities",
                  json={"name": "FindMe", "folderId": None}).get_json()
    uuid = made["personality"]["uuid"]
    try:
        a = db.make_app()
        db.init_db(a)
        with a.app_context():
            hits = db.find_uuid(uuid)
        assert hits, "personality uuid did not resolve"
        assert hits[0]["url"] == f"/personality?id={uuid}"
    finally:
        c.delete(f"/personality/api/personalities/{uuid}")
