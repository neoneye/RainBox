"""Marker tests for the /chat page shell (webapp/chat_template.py). The page's
JS is inline in the rendered template, so a GET of /chat carries both markup and
behavior — a single body assertion covers either side (same idea as
test_cron_views / test_kanban_views)."""

from webapp.core import app  # noqa: F401  ensure routes register
import webapp  # noqa: F401  registers chat_views on the shared app


def _body() -> str:
    return app.test_client().get("/chat").get_data(as_text=True)


def test_chat_page_renders_with_nav():
    resp = app.test_client().get("/chat")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "pp-nav" in body
    assert "function buildRoomMenu" in body
    assert "function buildFolderMenu" in body


def test_room_and_folder_kebabs_have_copy_id():
    body = _body()
    assert "Copy room id" in body
    assert "Copy folder id" in body
    # both copy + confirm via the bottom-right toast (not an in-menu flash)
    assert "copyIdToast(roomUuid, 'Room')" in body
    assert "copyIdToast(folderId, 'Folder')" in body
    assert "function chatToast" in body
    assert 'id="chat-toast"' in body


def test_typed_newlines_render_as_line_breaks():
    """A single newline in a typed message must render as a line break (chat
    style), not collapse to a space — marked is configured with breaks:true."""
    body = _body()
    assert "breaks: true" in body
    assert "marked.parse(src, { breaks: true, gfm: true })" in body


def test_new_room_modal_has_room_type_choice():
    body = _body()
    assert 'name="chat-room-type"' in body
    assert "Direct LLM chat" in body
    assert "function syncRoomTypeUI" in body
    assert 'id="chat-room-agents"' in body


def test_direct_room_settings_sidebar():
    body = _body()
    assert '<option value="settings">Settings</option>' in body
    assert "function renderDirectSettings" in body
    assert "/chat/api/models" in body
    assert "ds-prompt" in body
    assert "ds-model" in body


def test_direct_room_prompt_picker():
    """The Settings sidebar links a room to a stored /prompt version via a
    modal showing the prompt folder tree."""
    body = _body()
    assert 'id="chat-prompt-modal"' in body
    assert 'id="chat-prompt-tree"' in body
    assert "function openPromptPicker" in body
    assert "function renderPromptPicker" in body
    assert "/prompt/api/tree" in body
    assert "ds-prompt-mode" in body
    assert "Choose stored prompt" in body
    assert "Unlink" in body


def test_direct_room_message_edit():
    body = _body()
    assert "function startEditMessage" in body
    assert "msg-edit-btn" in body
    assert "function currentRoomIsDirect" in body
    assert "function putJSON" in body


def test_direct_room_message_delete():
    body = _body()
    assert "function deleteMessage" in body
    assert "msg-delete-btn" in body


def test_direct_room_has_no_feedback_buttons():
    """The upvote/downvote row is gated on not being in a direct room —
    feedback rates responder agents, and a direct chat has none."""
    body = _body()
    assert "!currentRoomIsDirect() && !isDebug && m.sender_type === 'agent'" in body


def test_export_sidebar():
    """The Export sidebar mode: scope + metadata controls, JSON format note,
    and Download / Copy actions wired to the /export endpoint."""
    body = _body()
    assert '<option value="export">Export</option>' in body
    assert "function renderExport" in body
    assert "/export?metadata=" in body
    assert "Last N messages" in body
    assert "Minimal (user / assistant, text only)" in body
    assert "Output format" in body
    assert "Copy to clipboard" in body
    assert "Download" in body


def test_room_rename_goes_through_confirm_modal():
    """The room title is a click-to-rename control; renaming happens in a
    modal (docs/ui-modal-rename.md), so a typed-but-unconfirmed name can't be
    silently lost."""
    body = _body()
    assert '<button type="button" id="room-title-name" title="Click to rename">' in body
    assert 'id="chat-rename-modal"' in body
    assert 'id="chat-rename-input"' in body
    assert 'id="chat-rename-confirm"' in body
    assert "function openChatRenameModal" in body
    assert "function confirmChatRenameModal" in body


def test_export_prefs_persist_in_localstorage():
    """The Export panel's scope / last-N / metadata selections persist in
    localStorage so the panel reopens the way it was last used."""
    body = _body()
    assert "chat.exportPrefs" in body
    assert "function loadExportPrefs" in body
    assert "function saveExportPrefs" in body


def test_direct_room_message_retry():
    """The message overflow menu offers Retry in direct rooms: re-ask the model
    from that turn, confirming first when later user messages would be deleted."""
    body = _body()
    assert "function retryFromMessage" in body
    assert "'/retry'" in body
    assert "textContent = 'Retry'" in body
    assert "window.confirm('Retrying from here deletes everything after this turn" in body


def test_message_edit_saves_on_enter():
    """In the in-place message editor, Enter saves (Shift+Enter for a newline,
    Escape cancels) — the same keys the compose box uses to send."""
    body = _body()
    assert "const doSave = async () => {" in body
    assert "e.key === 'Enter' && !e.shiftKey && !save.disabled" in body
