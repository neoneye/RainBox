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
                   "promptCloneUuid", "promptLoadDiff", "promptSaveEdit",
                   "promptSavePush", "/prompt/api/tree"]:
        assert marker in b, f"missing JS marker: {marker}"


def test_content_editing_is_explicit():
    """Prompt content is read-only until Edit is clicked; the edit is resolved
    only by Save or Cancel, with the rest of the page behind the modal
    backdrop meanwhile — no autosave, so an accidental keystroke in a system
    prompt can never persist on its own."""
    b = _body()
    assert 'id="prompt-edit-btn"' in b
    assert 'id="prompt-save-btn"' in b
    assert 'id="prompt-cancel-btn"' in b
    assert "function promptStartEdit" in b
    assert "function promptSaveEdit" in b
    assert "function promptCancelEdit" in b
    assert "promptEditorReadOnly(true);  // content is read-only until Edit is clicked" in b
    assert "#prompt-editor.editing{position:relative;z-index:1600" in b
    assert "promptContentEdited" not in b  # the autosave machinery is gone


def test_editor_is_codemirror():
    """The content editor is CodeMirror: markdown highlighting, line numbers,
    soft wrap, and a hard-line-end symbol so soft wraps are identifiable."""
    b = _body()
    assert "codemirror" in b            # CDN css/js pulled in
    assert "mode/markdown/markdown" in b
    assert "function promptInitEditor" in b
    assert "lineNumbers: true" in b
    assert "lineWrapping: true" in b
    assert 'content:"⏎"' in app.test_client().get("/prompt").get_data(as_text=True)


def test_new_chat_button():
    """"New chat" creates a direct /chat room linked to the open prompt
    version and navigates to it."""
    b = _body()
    assert 'id="prompt-newchat-btn"' in b
    assert "function promptNewChat" in b
    assert "/chat/api/rooms" in b
    assert "prompt_uuid" in b
    assert "/chat?id=" in b


def test_rename_goes_through_confirm_modal():
    """Renaming is modal-confirmed: the right pane shows the node's name as a
    click-to-rename control, and all editing happens in the rename modal, so
    a typed-but-unconfirmed name can't be silently lost."""
    b = _body()
    assert 'id="prompt-rename-modal"' in b
    assert 'id="prompt-rename-input"' in b
    assert 'id="prompt-rename-confirm"' in b
    assert "prompt-rename-display" in b
    assert "function promptOpenRenameModal" in b
    assert "function promptConfirmRenameModal" in b
