"""Tests for webapp/profile_views.py (+ the profile.js reference).

Marker-string tests prove presence, not behaviour — the real tree/form
behaviours are verified in a browser per docs/ui-left-panel-tree.md §8.
"""
from pathlib import Path

from webapp.core import app
import webapp.profile_views as profile_views


def _page() -> str:
    return app.test_client().get("/profile").get_data(as_text=True)


def test_profile_page_renders_with_nav():
    body = _page()
    assert 'class="profile-split"' in body
    assert "pp-nav" in body
    assert "/static/profile.js?v=" in body    # mtime cache-busted external JS


def test_nav_has_profile_link():
    assert ">Profile<" in _page()


def test_form_fieldsets_from_registry():
    body = _page()
    for legend in ("Identity", "Locale &amp; formats", "Contact &amp; location"):
        assert f"<legend>{legend}</legend>" in body
    for key in ("full_name", "native_name", "preferred_name", "handle", "gender",
                "about", "birthday", "units", "timezone", "date_format",
                "time_format", "language", "language_2", "currency", "currency_2",
                "country", "city", "address", "email"):
        assert f'data-key="{key}"' in body, f"missing field {key}"
    for dl in ("profile-dl-tz", "profile-dl-lang", "profile-dl-currency",
               "profile-dl-country"):
        assert f'id="{dl}"' in body
    assert 'id="profile-preview"' in body
    assert 'id="profile-dynamic"' in body
    assert 'id="profile-save-status"' in body
    assert "Built-in template" in body


def test_soft_validation_affordances():
    """Datalist-backed fields carry visible placeholders and an advisory
    warning line; timezone gets a one-click browser fill. Advisory only —
    the server deliberately stays soft on IANA/BCP-47/4217 membership."""
    body = _page()
    for key in ("timezone", "language", "language_2", "currency", "currency_2",
                "country"):
        assert f'id="pf-warn-{key}"' in body, f"missing warning line for {key}"
    assert 'placeholder="IANA name, e.g. Europe/Copenhagen"' in body
    assert 'id="profile-tz-mine"' in body
    b = _body()
    for marker in ("profileUpdateWarnings", "profileCheckTimezone",
                   "profileCheckLanguage", "profileCheckCurrency",
                   "resolvedOptions().timeZone"):
        assert marker in b, f"missing JS marker: {marker}"


def test_folder_table_columns():
    body = _page()
    for col in ("<th>Name</th>", "<th>Person</th>", "<th>Language</th>",
                "<th>Time</th>", "<th>Country</th>"):
        assert col in body
    assert "<th>Units</th>" not in body  # nearly always metric — not worth a column


def test_page_has_tree_and_modal_markers():
    body = _page()
    for marker in ('id="profile-tree-root"', 'id="profile-root-drop"',
                   'id="profile-all"', 'id="profile-rename-modal"',
                   'id="profile-new-modal"', 'id="profile-delete-modal"',
                   'id="profile-delete-input"', 'id="profile-node-rename"'):
        assert marker in body, f"missing marker: {marker}"


def _body() -> str:
    client = app.test_client()
    page = client.get("/profile").get_data(as_text=True)
    js = client.get("/static/profile.js")
    assert js.status_code == 200  # the shell references it; it must serve
    return page + js.get_data(as_text=True)


def test_js_has_core_markers():
    b = _body()
    for marker in ["profileLoadTree", "profileRenderTree", "profileItemNode",
                   "profileSavePush", "profileDataPush", "profileFieldEdited",
                   "profileDuplicateUuid", "profileUpdatePreview",
                   "profileFlushData", "profileRenderDynamic",
                   "/profile/api/tree", "Intl.supportedValuesOf",
                   "Preview unavailable", "beforeunload"]:
        assert marker in b, f"missing JS marker: {marker}"


def test_rename_goes_through_confirm_modal():
    """Renaming is modal-confirmed: the right pane shows the node's name as a
    click-to-rename control, and all editing happens in the rename modal, so
    a typed-but-unconfirmed name can't be silently lost."""
    b = _body()
    for marker in ["profile-rename-display", "function profileOpenRenameModal",
                   "function profileConfirmRenameModal"]:
        assert marker in b, f"missing rename marker: {marker}"


def test_tree_rows_are_real_links():
    # Folder and profile rows (and the static "All profiles" node) are anchors
    # with a real href so CMD/Ctrl click (and middle click) opens the
    # selection in a new tab via its ?id= deep link.
    b = _body()
    assert "node.href = '/profile?id=' + encodeURIComponent(f.id)" in b
    assert "n.href = '/profile?id=' + encodeURIComponent(p.uuid)" in b
    assert '<a class="profile-node" id="profile-all" href="/profile">' in b
    assert "if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;" in b
    assert "text-decoration:none" in b


def test_calibration_fieldset_present():
    """The Knowledge calibration fieldset renders after Contact & location
    with its own status line, error line, row container, and add button."""
    body = _page()
    assert "<legend>Knowledge calibration</legend>" in body
    assert (body.index("<legend>Contact &amp; location</legend>")
            < body.index("<legend>Knowledge calibration</legend>"))
    for marker in ("profile-cal-status", "profile-cal-error",
                   "profile-cal-rows", "profile-cal-add", "profile-dl-topic"):
        assert marker in body, f"missing calibration marker: {marker}"


def test_calibration_js_markers():
    """profile.js carries the calibration editor: per-profile autosave state,
    debounced single-in-flight PUT with per-class response handling, reorder
    via up/down buttons (not drag-and-drop), and unload-guard participation."""
    b = _body()
    for marker in ["profileCalState", "profileCalPush", "profileCalEdited",
                   "profileCalLoad", "profileCalRender", "profileCalMove",
                   "profileCalFlush", "profileCalPayload",
                   "/calibration", "PROFILE_CAL_DEBOUNCE_MS",
                   "PROFILE_CAL_RETRY_MAX_MS", "status === 400"]:
        assert marker in b, f"missing calibration JS marker: {marker}"
    # The unload guard covers calibration pending/invalid states.
    assert "profileCalPending(st) || (st && st.invalid)" in b


def test_no_backslash_escapes_in_template():
    # The template is a non-raw Python string: a \n-style escape inside any
    # inline script would be eaten by Python and break the page silently.
    src = Path(profile_views.__file__).read_text(encoding="utf-8")
    template = src.split('PROFILE_TEMPLATE = """', 1)[1].split('"""', 1)[0]
    assert "\\" not in template
