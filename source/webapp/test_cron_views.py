"""Tests for webapp/cron_views.py + static/cron.js.

The /cron page is frontend-only: the route renders the HTML shell (+ inline
CSS) and all interactivity lives in browser-side JS served from
static/cron.js. `_body()` returns the rendered page concatenated with the
served JS, so marker assertions cover both regardless of which side a marker
lives on.
"""

from webapp.core import app


def _body() -> str:
    client = app.test_client()
    page = client.get("/cron").get_data(as_text=True)
    js_resp = client.get("/static/cron.js")
    assert js_resp.status_code == 200  # the shell references it; it must serve
    return page + js_resp.get_data(as_text=True)


def test_cron_page_renders_with_nav():
    client = app.test_client()
    resp = client.get("/cron")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert 'class="cron-split"' in body  # the cron page layout
    assert "pp-nav" in body  # shared nav included
    # The shell pulls the page logic from the static JS with a cache-buster.
    assert '/static/cron.js?v=' in body


def test_nav_has_cron_link():
    client = app.test_client()
    resp = client.get("/cron")
    body = resp.get_data(as_text=True)
    # The shared nav links to the cron page and marks it active on this page.
    assert ">Cron<" in body
    assert "pp-active" in body


def test_cron_page_has_builder_controls():
    body = _body()
    # Five schedule selects.
    for sel in ['id="f-min"', 'id="f-hour"', 'id="f-dom"', 'id="f-mon"', 'id="f-dow"']:
        assert sel in body, sel
    # Live cron string target + action-type toggle.
    assert 'id="cron-string"' in body
    assert 'name="atype"' in body
    assert 'value="message"' in body
    assert 'value="command"' in body
    # Message target is a chatroom picker (<select>), not free text, and the JS
    # populates it from the loaded chatrooms.
    assert '<select id="f-target">' in body
    assert '<select id="ea-target">' in body
    assert "cronPopulateTargetSelect" in body
    assert "cronChatrooms" in body
    # The JS that assembles the cron string and toggles type must be present.
    assert "function cronCurrent" in body
    assert "function cronToggleType" in body


def test_cron_page_has_table_and_crud():
    body = _body()
    # Table headers.
    for header in [">Active<", ">uuid<", ">name<", ">schedule<", ">command<", ">description<"]:
        assert header in body, header
    # CRUD entry points + add control + uuid generation.
    assert 'id="cron-rows"' in body
    # Name is a required field; description is optional.
    assert 'id="f-name"' in body
    assert "Name is required." in body


def test_cron_page_has_enable_toggle():
    body = _body()
    # The list "Active" column is read-only (edit via the detail Active toggle).
    assert ">Active<" in body       # column header uses the "Active" terminology
    assert "row-toggle" not in body  # no editable checkbox in the table
    assert "function cronToggle" in body
    assert "tr.cron-off" in body    # grayed-out style hook for disabled rows


def test_cron_page_has_folder_tree_split():
    body = _body()
    # Split view: left folder tree + right panel keeps the existing UI.
    assert 'class="cron-split"' in body
    assert 'id="cron-tree"' in body
    assert 'id="cron-main"' in body
    # Builder gained a folder-assignment dropdown.
    assert 'id="f-folder"' in body
    # Folder tree rendering + folder CRUD.
    for fn in ["function cronRenderTree", "function cronSelectFolder",
               "function cronAddFolder", "function cronDeleteFolder"]:
        assert fn in body, fn
    # The page hydrates its folder/job tree from the backend (no inline demo
    # seed), so it starts empty when the DB is empty.
    # "+ Job" opens the create-new-cronjob form; the builder folder picker
    # ("(unfiled)" + folders) is the optional parent-folder chooser.
    assert ">+ Job<" in body
    assert "function cronNewJob" in body
    assert "(unfiled)" in body
    # The create/edit form is a toggleable section, separate from the job list.
    assert 'id="cron-builder"' in body
    assert 'id="cron-builder-title"' in body
    assert 'id="cron-table-wrap"' in body
    # New cronjob opens as a modal overlay over the shared click-blocking
    # backdrop; the builder card carries the app-wide ui-modal class.
    assert 'id="ui-modal-backdrop"' in body
    assert "builder ui-modal" in body
    # Modals use the shared modal stylesheet + the unified title/action pattern
    # (docs/ui-modals.md): an <h3> title and a right-aligned .modal-actions row
    # with cancel-then-primary buttons carrying .btn-* classes. The old
    # .builder-title div + inline-styled action buttons are gone.
    assert '<link rel="stylesheet" href="/static/ui-modal.css">' in body
    assert '<h3 id="cron-builder-title">' in body
    assert 'class="modal-actions"' in body
    assert 'class="btn-primary"' in body
    assert 'class="btn-cancel"' in body
    assert 'class="btn-danger"' in body  # destructive delete-confirm button
    assert 'class="builder-title"' not in body
    # No right-pane title label — the click-to-rename name display doubles as
    # the pane heading (docs/ui-modal-rename.md).
    assert 'id="cron-pane-title"' not in body
    # Job creation is via the tree's "+ Job" action / folder kebab — there is no
    # separate "New job" button cluttering the All jobs / Folder details pages.
    assert 'id="cron-add-job-btn"' not in body
    # Inlined Lucide folder / folder-open icons drive the expand state.
    assert "CRON_ICON_FOLDER" in body
    assert "CRON_ICON_FOLDER_OPEN" in body
    # Folder detail panel with activate/deactivate, cascading to the subtree.
    assert 'id="cron-folder-detail"' in body
    assert "function cronToggleFolderEnabled" in body
    assert "function cronFolderEnabled" in body
    # Renaming is modal-confirmed (docs/ui-modal-rename.md): the right pane
    # shows a click-to-rename name display; editing happens in the modal.
    assert 'id="cron-node-rename"' in body
    assert "function cronRenderRename" in body
    assert 'id="cron-rename-modal"' in body
    assert 'id="cron-rename-input"' in body
    assert "cron-rename-display" in body
    assert "function cronOpenRenameModal" in body
    assert "function cronConfirmRenameModal" in body
    # Job details edits via dedicated overlays (NOT the New-job builder): a
    # read-only schedule/action summary panel + "Edit schedule" / "Edit action".
    assert 'id="cron-job-detail"' in body
    assert "function cronRenderJobDetail" in body
    assert ">Edit schedule</button>" in body
    assert ">Edit action</button>" in body
    assert 'id="cron-sched-modal"' in body
    assert 'id="cron-action-modal"' in body
    assert "function cronSaveSchedule" in body
    assert "function cronSaveAction" in body
    # The Edit-schedule overlay has its own ('es-' prefixed) schedule selects.
    for sel in ['id="es-min"', 'id="es-hour"', 'id="es-dom"', 'id="es-mon"', 'id="es-dow"']:
        assert sel in body, sel
    # Timezone picker (Local time / UTC) on both create and edit-schedule.
    assert 'id="f-tz"' in body
    assert 'id="es-tz"' in body
    assert 'value="localtime"' in body and 'value="UTC"' in body
    assert "function cronTzLabel" in body


def test_cron_page_has_drag_and_drop():
    body = _body()
    # Single-node drag/drop: reorder jobs, move jobs/folders between folders,
    # nest folders, and drop to the root level.
    for fn in ["function cronMakeDraggable", "function cronMoveJob",
               "function cronMoveFolder", "function cronMoveFolderBeside",
               "function cronDropInto", "function cronDropOnJob",
               "function cronInitTreeDnD"]:
        assert fn in body, fn
    assert "cron-drop-target" in body
    assert "cronFolderInSubtree" in body  # cycle guard for nesting folders
    assert "function cronDuplicateJob" in body
    assert "function cronDuplicateFolder" in body
    assert 'id="cron-root-drop"' in body   # bottom 'Move to top level' drop zone


def test_cron_page_has_kebab_menu():
    body = _body()
    # 3-dot overflow menu on tree items (mirrors the chat page's room-kebab).
    assert "function cronMakeKebab" in body
    assert "cron-kebab" in body
    assert "cron-menu" in body
    assert "New job" in body  # folder kebab item opens the create form
    assert "function cronAddOrUpdate" in body
    assert "function cronEdit" in body
    assert "function cronDelete" in body
    # Delete is guarded: jobs/empty folders confirm; a non-empty folder cascades
    # and requires typing the folder name.
    assert "function cronConfirmDeleteJob" in body
    assert "function cronConfirmDeleteFolder" in body
    assert "Type the folder name to confirm" in body
    # New folder uses a custom overlay too.
    assert 'id="cron-folder-modal"' in body
    assert "function cronAddFolderConfirm" in body
    # No native dialogs anywhere (Firefox can permanently suppress those).
    assert "prompt(" not in body
    assert "confirm(" not in body  # cronConfirmDelete* are PascalCase, not confirm(
    assert "alert(" not in body
    assert "crypto.randomUUID" in body
    assert ">Create job<" in body
    # Folders also appear as rows in the All-jobs / Folder-details lists,
    # interleaved in tree order (depth-first).
    assert "function cronListNodes" in body
    assert "function cronFlattenTree" in body
    assert "cron-folder-row" in body
    # List rows link to the job's detail view (no per-row Edit/Delete buttons).
    assert "row-details" in body
    assert ">Details<" in body
    assert 'class="row-edit"' not in body
    assert 'class="row-del"' not in body


def test_cron_page_has_human_readable_describer():
    body = _body()
    # The cron explanation (deterministic describer) is rendered under the cron
    # string in the merged "schedule" column, and also feeds the builder's hint.
    assert "function cronDescribe" in body
    assert "cronDescribe(r.cron)" in body  # rendered into each table row


def test_cron_page_persists_via_api():
    body = _body()
    # The page hydrates from and saves to the tree endpoint.
    assert "/cron/api/tree" in body
    assert "function cronSave" in body
    assert "function cronLoadTree" in body
    # The hardcoded demo seed array is gone (these were real former-seed strings).
    assert "Token usage" not in body
    assert "review open PRs" not in body
    # ?id=<uuid> deep-links to a folder/job on load and the URL reflects selection.
    assert "function cronSyncUrl" in body
    assert "searchParams.set('id'" in body  # selection updates the URL
    assert ".get('id')" in body             # load reads the deep-link param
    # Job details shows backend creation/modification timestamps.
    assert "function cronFmtDate" in body
    assert "node.created_at" in body
    assert "node.updated_at" in body
    # Folders carry an editable description (notes about child nodes).
    assert 'id="cron-folder-desc"' in body
    assert "function cronRenderFolderDesc" in body
    # Description (folder + job) is edited via an overlay, not an inline textarea.
    assert 'id="cron-desc-modal"' in body
    assert ">Edit description</button>" in body
    assert "function cronSaveDescription" in body
    # "Run now" fires the selected job immediately via the API.
    assert ">Run now</button>" in body
    assert "function cronRunNow" in body
    assert "/run" in body


def test_cron_page_has_health_column():
    body = _body()
    # The list tables carry a health column whose cell shows the latest run's
    # outcome with the details (timestamp, trigger, error) on hover.
    assert "<th>health</th>" in body
    assert "function cronHealthCell" in body
    assert "cron-health-cell" in body


def test_cron_page_has_next_run_column():
    body = _body()
    # Next-run column: when the job fires next, or why it won't (disabled /
    # paused / draft).
    assert "<th>next run</th>" in body
    assert "function cronNextRunCell" in body
    assert "cron-nextrun-cell" in body


def test_tree_kebab_has_copy_id_items():
    body = _body()
    # The left-panel kebab offers a copy-uuid action for both folders and jobs.
    assert "Copy folder id" in body
    assert "Copy job id" in body
    assert "function cronCopyId" in body
    assert "navigator.clipboard.writeText(uuid)" in body


def test_cron_page_offers_script_action_type():
    body = _body()
    # Both the New-job builder and the Edit-action overlay offer Script; the
    # command input is shared between the command and script types.
    assert body.count('value="script"') == 2
    assert "t === 'script'" in body


def test_cron_page_has_check_health_button():
    body = _body()
    # Script jobs get a "Check health" button in the details Run section; it
    # POSTs to the check_health endpoint and renders the output inline.
    assert 'id="cjd-check-health"' in body
    assert ">Check health<" in body
    assert "function cronCheckHealth" in body
    assert "/check_health" in body
    assert 'id="cjd-health-check-out"' in body


def test_job_rows_are_real_links():
    # Jobs in the left-panel tree are anchors with a real href so CMD/Ctrl
    # click (and middle click) opens the job in a new tab. Plain clicks are
    # still intercepted for in-page selection; modified clicks pass through
    # to the browser. The kebab lives inside the anchor, so its handlers
    # preventDefault to never follow the link.
    body = _body()
    assert "n.href = '/cron?id=' + encodeURIComponent(j.uuid)" in body
    assert "if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;" in body
    assert body.count("e.preventDefault();") >= 3  # plain click + kebab + menu items
    assert "text-decoration:none" in body


def test_folder_rows_are_real_links():
    # Folder rows (and the static "All jobs" root node) are anchors too, so
    # CMD/Ctrl click opens the folder view in a new tab via its ?id= deep
    # link; the root node deep-links to the bare page (no ?id=).
    body = _body()
    assert "node.href = '/cron?id=' + encodeURIComponent(f.id)" in body
    assert '<a id="cron-all-jobs" class="cron-node" href="/cron">' in body
