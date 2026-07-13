"""Marker tests for webapp/kanban_views.py + static/kanban.js (the page shell
and its wiring; backend behavior is covered by test_kanban_api.py). `_body()`
returns the rendered page concatenated with the served JS, so assertions cover
both regardless of which side a marker lives on.
"""

from webapp.core import app


def _body() -> str:
    client = app.test_client()
    page = client.get("/kanban").get_data(as_text=True)
    js_resp = client.get("/static/kanban.js")
    assert js_resp.status_code == 200  # the shell references it; it must serve
    return page + js_resp.get_data(as_text=True)


def test_kanban_page_renders_with_nav():
    client = app.test_client()
    resp = client.get("/kanban")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert 'class="kb-split"' in body   # boards list | board canvas layout
    assert "pp-nav" in body             # shared nav included
    assert ">Kanban<" in body and "pp-active" in body  # nav links + marks this page
    assert "/static/kanban.js?v=" in body  # logic served externally, cache-busted


def test_kanban_page_is_database_backed():
    body = _body()
    # State hydrates from and saves to the kanban API — no browser-local
    # source of truth. localStorage holds only the sidebar-mode UI preference
    # (same as chat's chat.sidebarMode); the old board-data store key is gone.
    assert "/kanban/api/boards" in body
    assert "/kanban/api/board/" in body
    assert "rainbox.kanban.v1" not in body
    assert "kanban.sidebarMode" in body
    # The save carries the optimistic-concurrency token + declared deletes,
    # serializes in-flight PUTs, and re-hydrates on 409 instead of clobbering.
    assert "version: board.version" in body
    assert "pendingDeletes" in body
    assert "r.status === 409" in body
    assert "function kbSavePush" in body
    # Save lifecycle is board-switch safe: the debounced save captures its
    # board when scheduled, all saves run on an awaitable promise chain
    # (duplicate awaits the flush so it can't snapshot stale server state),
    # and a save is cancelled when its board is about to be deleted.
    assert "kbSaveBoardRef = kbCurrent" in body
    assert "kbSaveChain" in body
    assert "await kbFlushSave()" in body
    assert "function kbCancelSave" in body


def test_tree_kebab_has_copy_id_items():
    body = _body()
    # The left-panel kebab offers a copy-uuid action for both kinds.
    assert "Copy board id" in body
    assert "Copy folder id" in body
    assert "function kbCopyId" in body
    assert "navigator.clipboard.writeText(uuid)" in body


def test_kanban_page_has_right_sidebar_picker():
    body = _body()
    # Right sidebar with an off/stats/developer picker, mirroring /chat's
    # mechanics.
    assert 'id="kb-sidebar-mode"' in body
    assert ">Sidebar: off<" in body
    assert ">Stats<" in body
    assert ">Developer<" in body
    assert 'id="kb-sidebar"' in body
    assert "kb-sidebar-open" in body
    assert "function kbRenderSidebar" in body
    assert "function kbStatRow" in body
    # Developer mode: View/Copy per LLM serialization (markdown, json).
    assert "function kbRenderSidebarDev" in body
    assert "KB_SERIALIZATIONS" in body


def test_kanban_page_has_boards_and_columns():
    body = _body()
    assert 'id="kb-tree-root"' in body
    assert ">+ Board<" in body
    assert "function kbNewBoard" in body
    assert "function kbSelectBoard" in body
    assert "function kbConfirmDeleteBoard" in body
    assert "searchParams.set('id'" in body  # single ?id= deep-link, like /cron
    assert "+ Add task" in body
    # Kebab (3-dot) menu on the selected board item: Duplicate / Delete
    # (mirrors the cron tree's and chat room list's kebab).
    assert "function kbMakeKebab" in body
    assert "kb-kebab" in body
    assert "kb-menu" in body
    assert "'Duplicate'" in body
    assert "function kbDuplicateBoard" in body
    assert "/duplicate" in body


def test_kanban_tasks_have_uuid_and_agent_uuid():
    body = _body()
    # Tasks carry a uuid (shown short, full on hover + in the edit modal) and
    # an agent referenced BY UUID (stable across role renames); the picker is
    # server-injected {name, uuid} pairs, names for display only.
    assert "crypto.randomUUID" in body
    assert 'class="kb-uuid"' in body
    assert 'id="kb-t-uuid"' in body
    assert "window.KANBAN_AGENTS" in body
    assert "agentUuid" in body
    assert "function kbAgentName" in body
    assert "(unassigned)" in body
    # Drag-and-drop between columns + reorder onto a card.
    assert "function kbMoveTask" in body
    assert "kb-drop-before" in body
    # The edit modal shows the task's audit trail (kanban_task_event) and the
    # read-only lease state (claimedBy / claimExpiresAt — only the agent claim
    # operations write the lease).
    assert 'id="kb-t-events"' in body
    assert "function kbLoadTaskEvents" in body
    assert "/events" in body
    assert 'id="kb-t-claim"' in body
    assert "claimedBy" in body
    # "Run" enqueues the assigned agent to execute the task (milestone 3).
    assert 'id="kb-t-run"' in body
    assert "function kbEnqueueTask" in body
    assert "/enqueue" in body


def test_kanban_serializations_are_served_from_db():
    body = _body()
    # The LLM-facing serializations (markdown + json) are generated
    # server-side from canonical DB state, not from browser state.
    assert "/markdown" in body
    assert "/json" in body
    assert "function kbShowSerialization" in body
    assert "function kbCopySerialization" in body
    assert 'id="kb-md-pre"' in body
    assert "kbBoardMarkdown" not in body  # no client-side serializer remains


def test_kanban_no_native_dialogs():
    body = _body()
    # House rule: in-page overlays only (a browser can permanently suppress
    # native dialogs).
    assert "prompt(" not in body
    assert "confirm(" not in body   # kbConfirm* are PascalCase, not confirm(
    assert "alert(" not in body
    assert 'id="kb-confirm-modal"' in body


def test_board_rows_are_real_links():
    # Boards in the left-panel tree are anchors with a real href so CMD/Ctrl
    # click (and middle click) opens the board in a new tab. Plain clicks are
    # still intercepted for in-page selection; modified clicks pass through
    # to the browser. The kebab lives inside the anchor, so its handlers
    # preventDefault to never follow the link.
    body = _body()
    assert "node.href = '/kanban?id=' + encodeURIComponent(b.uuid)" in body
    assert "if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;" in body
    assert "text-decoration:none" in body
