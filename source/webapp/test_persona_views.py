"""Tests for webapp/persona_views.py + static/persona.js.

The page is frontend-only: the route renders the HTML shell (+ inline CSS) and
all interactivity lives in static/persona.js. `_body()` returns the page
concatenated with the served JS so marker assertions cover both.
"""
from webapp.core import app


def _body() -> str:
    client = app.test_client()
    page = client.get("/persona").get_data(as_text=True)
    js = client.get("/static/persona.js")
    assert js.status_code == 200  # the shell references it; it must serve
    return page + js.get_data(as_text=True)


def test_page_renders_with_nav():
    body = app.test_client().get("/persona").get_data(as_text=True)
    assert 'class="persona-split"' in body
    assert "pp-nav" in body
    assert "/static/persona.js?v=" in body


def test_nav_has_persona_link():
    body = app.test_client().get("/persona").get_data(as_text=True)
    assert ">Persona<" in body
    assert "pp-active" in body


def test_page_has_editor_and_history_markers():
    body = app.test_client().get("/persona").get_data(as_text=True)
    for marker in ['id="persona-content"', 'id="persona-history"',
                   'id="persona-revcount"', 'id="persona-history-btn"',
                   'id="persona-new-modal"', 'id="persona-delete-modal"',
                   'id="persona-restore-modal"']:
        assert marker in body, f"missing page marker: {marker}"


def test_js_has_core_markers():
    b = _body()
    for marker in ["personaLoadTree", "personaRenderTree",
                   "personaItemNode", "personaSavePush",
                   "personaAddPersonaConfirm", "personaDeleteItem",
                   "/persona/api/tree"]:
        assert marker in b, f"missing JS marker: {marker}"


def test_tree_save_declares_no_deletes():
    """Per notes/ui-tree-persistence.md the tree PUT cannot delete, so the
    client must not carry a deletes counter — deletion goes to DELETE."""
    b = _body()
    assert "deletes" not in b
    assert "method: 'DELETE'" in b


def test_history_view_markers():
    b = _body()
    for marker in ["function personaToggleHistory",
                   "function personaLoadHistory",
                   "function personaShowRevisionDiff",
                   "function personaConfirmRestore",
                   "/revisions", "/restore"]:
        assert marker in b, f"missing history marker: {marker}"


def test_content_editing_is_explicit():
    """Persona text is read-only until Edit is clicked; the edit resolves
    only via Save or Cancel, with the rest of the page behind the modal
    backdrop meanwhile — no autosave."""
    b = _body()
    assert 'id="persona-edit-btn"' in b
    assert 'id="persona-save-btn"' in b
    assert 'id="persona-cancel-btn"' in b
    assert "function personaStartEdit" in b
    assert "function personaSaveEdit" in b
    assert "function personaCancelEdit" in b
    assert "#persona-editor.editing{position:relative;z-index:1600" in b


def test_create_and_delete_flush_pending_save():
    """Per notes/ui-tree-persistence.md the client must flush or await a
    pending tree PUT before issuing a create or delete, so the older PUT's
    response can't land after the create/delete's fresher token and stomp it
    with a stale one."""
    b = _body()
    assert "function personaFlushPendingSave" in b
    for fn in ["personaAddFolderConfirm", "personaAddPersonaConfirm",
               "personaDeleteItem", "personaDeleteFolderById"]:
        start = b.index("async function " + fn)
        end = b.index("\n}", start)
        body = b[start:end]
        assert "await personaFlushPendingSave()" in body, \
            f"{fn} does not flush the pending tree PUT before its fetch"


def test_find_resolves_a_persona_uuid():
    import db
    c = app.test_client()
    made = c.post("/persona/api/personas",
                  json={"name": "FindMe", "folderId": None}).get_json()
    uuid = made["persona"]["uuid"]
    try:
        a = db.make_app()
        db.init_db(a)
        with a.app_context():
            hits = db.find_uuid(uuid)
        assert hits, "persona uuid did not resolve"
        assert hits[0]["url"] == f"/persona?id={uuid}"
    finally:
        c.delete(f"/persona/api/personas/{uuid}")
