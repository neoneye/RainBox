"""Tests for webapp/bridges_views.py + static/bridges.js.

The /bridges page is frontend-only: the route renders the HTML shell (+ inline
CSS) and all interactivity lives in static/bridges.js. `_body()` returns the
page concatenated with the served JS so marker assertions cover both.
"""
from webapp.core import app


def _body() -> str:
    client = app.test_client()
    page = client.get("/bridges").get_data(as_text=True)
    js = client.get("/static/bridges.js")
    assert js.status_code == 200  # the shell references it; it must serve
    return page + js.get_data(as_text=True)


def test_bridges_page_renders_with_nav():
    body = app.test_client().get("/bridges").get_data(as_text=True)
    assert 'class="br-split"' in body        # the page layout
    assert "pp-nav" in body                  # shared nav included
    assert "/static/bridges.js?v=" in body   # JS pulled in with a cache-buster
    assert ">Bridges<" in body and "pp-active" in body


def test_js_has_core_markers():
    b = _body()
    for marker in ["brLoadTree", "brRenderTree", "brConnectorLi", "brFolderLi", "brBindingNode",
                   "brAddConnectorConfirm", "brAddBindingConfirm", "/bridges/api/connectors",
                   "/bridges/api/bindings", "/bridges/api/tree", "brFlushPendingSave", "brSavePush",
                   "/services/api/status", "/services/api/restart/bridge:", "brResolvePolicy",
                   "brEffectiveEnabled", "brLaunchCommand", "Copy ID"]:
        assert marker in b, f"missing JS marker: {marker}"


def test_tree_rows_are_real_links():
    body = _body()
    assert "node.href = '/bridges?id=' + encodeURIComponent(c.uuid)" in body
    assert "node.href = '/bridges?id=' + encodeURIComponent(f.id)" in body
    assert "n.href = '/bridges?id=' + encodeURIComponent(b.uuid)" in body
    assert '<a class="br-node" id="br-all" href="/bridges">' in body
    assert "if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;" in body
    assert "text-decoration:none" in body


def test_tree_save_declares_no_deletes():
    """Per notes/ui-tree-persistence.md the tree PUT cannot delete, so the
    client must not carry a deletes counter — deletion goes to DELETE."""
    b = _body()
    assert "deletes" not in b
    assert "method: 'DELETE'" in b


def test_rename_goes_through_the_modal_only():
    """notes/ui-modal-rename.md: a click-to-rename display opens a modal with
    Cancel/Rename as the only ways out; no inline field + Save button."""
    b = _body()
    assert 'id="br-rename-modal"' in b and "brOpenRenameModal" in b and "brConfirmRenameModal" in b
    assert "btn.id = 'br-rename-display'" in b


def test_credentials_never_appear_in_the_page_or_command():
    """The launch command names the credential variable beside the command
    and never writes an assignment for it; the connector form stores a NAME."""
    b = _body()
    assert "must already be set in the launch environment" in b
    assert "stored sealed and never shown again" in b
    # The token field is write-only: a password input inside its own modal,
    # cleared on close, and the pane only ever renders "set" / "not set".
    assert 'type="password" id="br-credential-input"' in b
    assert "/credential'" in b and "brRenderCredential" in b
    assert "document.getElementById('br-credential-input').value = '';   // never keep it around" in b
    # Values are single-quoted for the shell so display names never become syntax.
    assert "brShellQuote" in b


def test_admin_views_for_bridge_rows_are_read_only():
    """Every bridge write goes through /bridges (validation, ownership
    guards, restart nonce, bridge_config notify, launcher push); an admin
    edit or delete would bypass all of it."""
    from webapp.core import BridgeBindingView, BridgeConnectorView, BridgeFolderView
    for view in (BridgeConnectorView, BridgeFolderView, BridgeBindingView):
        assert not view.can_create and not view.can_edit and not view.can_delete
