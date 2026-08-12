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


def test_messages_wrap_long_unbroken_text():
    """Message prose must fit the chat pane instead of forcing horizontal
    scrolling for long unbroken tokens (big numbers, hashes, urls) — code
    blocks keep a per-block scrollbar, except in debug rows where pre content
    wraps too. The assistant trace rows wrap their pre-wrap tool output."""
    body = _body()
    assert ".msg-text{line-height:1.5;overflow-wrap:anywhere;word-break:break-word}" in body
    assert ".msg-debug .msg-text pre{white-space:pre-wrap;overflow-wrap:anywhere;" in body
    assert "word-break:break-word;overflow-x:hidden" in body
    assert "white-space:pre-wrap;overflow-wrap:anywhere" in body


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
    # Only available models are offered, plus whatever the room already holds.
    assert "models.filter(mm => mm.available || mm.uuid === settings.model_uuid)" in body


def test_sidebar_modes_match_room_type():
    """Members is hidden in direct rooms (no agents there). Settings now
    applies to both room types (model/prompt for a direct room, the persona
    picker for an agents room) so it is never hidden, and the remembered mode
    maps Members->Settings in a direct room rather than dropping to hidden."""
    body = _body()
    assert "function syncSidebarModeOptions" in body
    assert "membersOpt.hidden = membersOpt.disabled = direct" in body
    assert "settingsOpt.hidden" not in body
    assert "function effectiveSidebarMode" in body
    assert "function activeSidebarMode" in body


def test_sidebar_visibility_is_separate_from_mode():
    """The panel choice and the shown/hidden state persist under separate
    localStorage keys, and Ctrl+1 toggles visibility without touching
    the panel choice."""
    body = _body()
    assert "chat.sidebarVisible" in body
    assert "function persistSidebarPrefs" in body
    assert "sidebarVisible = !sidebarVisible" in body
    assert "e.key === '1'" in body


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
    modal (notes/ui-modal-rename.md), so a typed-but-unconfirmed name can't be
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


def test_kebab_menus_stay_inside_viewport():
    """Fixed-position kebab menus are clamped to the viewport: a menu opened
    near the bottom of the log/tree flips above its anchor instead of
    rendering off-screen."""
    body = _body()
    assert "function placeMenu" in body
    assert "top = anchorRect.top - menu.offsetHeight - 4" in body


def test_room_rows_are_real_links():
    """Rooms in the left-panel tree are anchors with a real href so CMD/Ctrl
    click (and middle click) opens the chat in a new tab. Plain clicks are
    still intercepted for in-page selection; modified clicks pass through to
    the browser."""
    body = _body()
    assert "btn.href = '/chat?id=' + encodeURIComponent(r.uuid)" in body
    assert "if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;" in body
    # anchors must not look like links nor hijack the row's drag-and-drop
    assert "text-decoration:none" in body
    assert "btn.draggable = false" in body


def test_folder_rows_are_real_links():
    """Folder rows are anchors too, so CMD/Ctrl click opens the folder view
    in a new tab via its ?id= deep link. The folder kebab lives inside the
    anchor, so its handlers preventDefault to never follow the link."""
    body = _body()
    assert "node.href = '/chat?id=' + encodeURIComponent(f.id)" in body
    # kebab + menu-item handlers inside the folder anchor must not navigate
    assert body.count("never follow") >= 2


def test_unread_badge_counts_only_new_real_messages():
    """The badge bumps once per NEW real message (event 'insert' of
    kind 'message') — never for streaming token batches ('update' events,
    which reuse one message_id), edits, or debug/thinking/progress rows."""
    body = _body()
    assert "d.event === 'insert' && d.kind === 'message'" in body


def test_sse_events_never_rebuild_the_room_tree():
    """renderRooms() replaces every room anchor; calling it per SSE event
    while another room streams destroys the node between mousedown and
    mouseup, making rooms unclickable. The badge is patched in place."""
    body = _body()
    assert "function bumpUnreadBadge" in body
    # the old full-rebuild deferral is gone along with the rebuild itself
    assert "deferredUnreadRender" not in body


def test_working_bubbles_count_up():
    """A turn that takes a minute should say so. The bubble counts from the
    row's own created_at — not from when this tab first saw it — so a reload
    or a second tab shows the same number, and one timer drives every bubble
    because the rows are rebuilt under it on each status update."""
    body = _body()
    assert "function formatWorkedFor" in body
    assert "return 'Worked for ' + s + 's';" in body
    assert "return 'Worked for ' + m + 'm ' + rs + 's';" in body
    assert "line.dataset.workedSince = String(since);" in body
    assert "log.querySelectorAll('.msg-worked[data-worked-since]')" in body
    assert "if (m.kind === 'progress') msg.appendChild(workedForLine(m));" in body


def test_working_bubble_opens_its_run():
    """Clicking the bubble is the shortcut to the trace; the row's own controls
    and a click that merely ended a text selection are not that click."""
    body = _body()
    assert "msg.classList.add('msg-progress-linked');" in body
    assert "const href = '/assistant?id=' + encodeURIComponent(progressRunUuid);" in body
    assert "if (e.target.closest('a, button, input, textarea')) return;" in body
    assert "if (String(window.getSelection() || '')) return;" in body


def test_streaming_rows_are_patched_not_rebuilt():
    """The same trap as the room tree, one level down: a streaming row is
    re-sent every ~150ms, and rebuilding its node destroyed the button under
    the operator's finger — a click needs mousedown and mouseup on ONE element,
    so "Expand to view thoughts" did nothing until the model stopped thinking.
    Only the text changes between flushes, so only the text is refreshed."""
    body = _body()
    assert "function messageRenderKey" in body
    assert "function fillMessageBody" in body
    assert ("if (existing && m.streaming && "
            "existing.dataset.renderKey === messageRenderKey(m)){") in body
    assert "fillMessageBody(existing.querySelector('.msg-text'), m);" in body
    # A settled row still rebuilds — that is what clears the edit textarea.
    assert "existing.replaceWith(node);" in body
    # The copy button reads the row through the node, so a patched row doesn't
    # leave it copying the text this render happened to close over.
    assert "addCopyButton(actions, () => (msg._row || m).text);" in body


def test_live_messages_do_not_move_a_reader_who_scrolled_back():
    """Arriving rows used to yank the log to the bottom, resetting anyone
    reading older history. Following the tail is now conditional on already
    being at it — and the decision is taken before the append, since appending
    grows scrollHeight and the same reading afterwards would strand a reader
    who WAS at the bottom one screen short."""
    body = _body()
    assert "const follow = (opts && opts.force) || isNearBottom();" in body
    assert "msgs.forEach(appendMessage);\n  if (follow) scrollLogToBottom();" in body
    # The threshold is named, because it is the pivot for every live update.
    assert "const FOLLOW_THRESHOLD_PX = 80;" in body
    assert "log.scrollHeight - log.scrollTop - log.clientHeight < FOLLOW_THRESHOLD_PX" in body


def test_sending_a_message_always_scrolls_to_it():
    """The one case that overrides the reader's position: the operator's own
    send. They wrote the newest row, so take them to it wherever they were."""
    body = _body()
    assert "await fetchNew(currentRoom, {force: true});" in body


def test_assistant_rows_link_to_their_run_from_the_kebab():
    """Every terminal assistant post carries `meta.assistant_run_uuid`, so the
    chat keeps a way into the trace after the progress bubble is reaped. It
    lives in the message kebab, with the row's other navigations, rather than
    beside copy and feedback where a bare "run" read as a control that would
    execute something. An anchor, so cmd/middle-click opens a new tab."""
    body = _body()
    assert "const runUuid = (m.meta || {}).assistant_run_uuid;" in body
    assert "inspect.href = '/assistant?id=' + encodeURIComponent(runUuid);" in body
    assert "inspect.textContent = 'Inspect \u2197';" in body
    assert "inspect.className = 'item';" in body
    assert "msg-run-link" not in body


def test_a_failed_send_restores_the_text_and_says_so():
    """The composer is cleared optimistically, so a failed POST used to eat
    what was typed and leave the room looking like nothing happened — no
    message, no error. The text goes back in the box and the toast explains."""
    body = _body()
    assert "await postJSON('/chat/api/rooms/' + currentRoom + '/messages', { text });" in body
    assert "input.value = text;" in body
    assert ("chatToast('Message not sent: ' + e.message "
            "+ ' — your text is back in the box.');") in body


def test_agents_room_settings_panel_markers():
    """The sidebar's Settings mode carries the persona picker for agents
    rooms; direct rooms keep the model/prompt panel."""
    body = app.test_client().get("/chat").get_data(as_text=True)
    for marker in ["function renderAgentsSettings", "function openPersonaPicker",
                   "function openPersonaVersionPicker", "function setMemberPersona",
                   "function pinMemberPersonaRevision",
                   "function renderPersonaMemberSection",
                   "/persona/api/tree", "/personas", "persona_following"]:
        assert marker in body, f"missing marker: {marker}"


def test_settings_mode_is_available_in_agents_rooms():
    """effectiveSidebarMode no longer maps settings->members away from agents
    rooms — that mapping is what made the mode a dead end there."""
    body = app.test_client().get("/chat").get_data(as_text=True)
    assert "if (!direct && sidebarMode === 'settings') return 'members';" not in body


def test_typing_anywhere_focuses_the_composer():
    """A keystroke with the composer unfocused went nowhere — nothing else on
    the page consumes bare printable keys — so the opening characters of a
    message were lost until the textarea was clicked. A printable key now hands
    focus to the composer WITHOUT preventDefault, leaving the browser to deliver
    that same keystroke into it: replaying the character by hand would break
    dead keys, IME composition, key-repeat and the textarea's undo history."""
    body = _body()
    assert "function typingGoesToAnotherField(el)" in body
    assert "if (e.ctrlKey || e.metaKey || e.altKey) return;" in body
    assert "if (e.isComposing) return;" in body
    assert "if (e.key.length !== 1) return;" in body
    assert "if (typingGoesToAnotherField(e.target)) return;" in body
    # No hand-replay of the keystroke anywhere in the handler.
    handler = body.split("function typingGoesToAnotherField(el)")[1].split(
        "});")[1]
    assert "preventDefault" not in handler
    assert "input.value +=" not in body


def test_typing_redirect_yields_to_modals_and_the_folder_view():
    """Two places have no composer to type into: an open modal owns the
    keyboard even once its own field has lost focus, and the folder view hides
    the compose form outright."""
    body = _body()
    assert "if (!document.getElementById('ui-modal-backdrop').hidden) return;" in body
    assert "if (form.hidden) return;" in body


def test_overlong_messages_are_clamped_to_their_opening():
    """A pasted document or log used to own the whole pane, so scrolling back
    through the conversation meant scrolling through all of it. Over 24 lines
    or 2000 characters a message now shows its opening behind a fade, with a
    toggle naming what is held back — two limits because a pasted log is many
    short lines and a pasted document can be one very long line."""
    body = _body()
    assert "const MSG_PEEK_LINES = 24;" in body
    assert "const MSG_PEEK_CHARS = 2000;" in body
    assert "return s.length > MSG_PEEK_CHARS || s.split(MSG_NL).length > MSG_PEEK_LINES;" in body
    assert "const chars = s.length.toLocaleString() + ' characters';" in body
    assert "? 'show all ' + lines.toLocaleString() + ' lines (' + chars + ')'" in body
    # A pasted document that is one very long line has no line count to read.
    assert ": 'show the whole message (' + chars + ')';" in body
    assert ".msg-text.msg-clamped{max-height:24em;overflow:hidden;" in body
    assert "body.classList.toggle('msg-clamped', !expanded);" in body


def test_clamped_messages_collapse_from_either_end():
    """The toggle under the fade opens it; a second one above the body closes
    it, so a long message need not be scrolled back through to be put away.
    The collapse choice is keyed by message id like the thinking rows, so it
    survives the rebuilds an edit or a settling stream trigger."""
    body = _body()
    assert "moreTop.textContent = 'show less';" in body
    assert "msg.insertBefore(moreTop, body);" in body
    assert "moreBottom.textContent = expanded ? 'show less' : label;" in body
    assert "moreTop.style.display = expanded ? '' : 'none';" in body
    assert "if (expanded) expandedSections.add(m.id); else expandedSections.delete(m.id);" in body


def test_clamping_leaves_streaming_and_collapsible_rows_alone():
    """Thinking / debug-* rows already hide their whole body, so they must not
    gain a second toggle inside the first. A streaming row is re-sent every
    ~150ms through the in-place patch, which never re-runs this wiring — its
    label would count a message that has since grown, so it waits until the
    row settles and is rebuilt."""
    body = _body()
    assert "if (!collapseNoun && !m.streaming && isOverlongMessage(m.text)){" in body


def test_editing_hides_the_clamp_toggles():
    """The edit textarea holds the whole message, so show-all / show-less have
    nothing to act on while it is open."""
    body = _body()
    assert ("msgEl.querySelectorAll('.msg-more-toggle')"
            ".forEach(b => { b.style.display = 'none'; });") in body
