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


def test_folder_table_columns():
    body = _page()
    for col in ("<th>Name</th>", "<th>Person</th>", "<th>Language</th>",
                "<th>Units</th>", "<th>Time</th>", "<th>Country</th>"):
        assert col in body


def test_page_has_tree_and_modal_markers():
    body = _page()
    for marker in ('id="profile-tree-root"', 'id="profile-root-drop"',
                   'id="profile-all"', 'id="profile-rename-modal"',
                   'id="profile-new-modal"', 'id="profile-delete-modal"',
                   'id="profile-delete-input"', 'id="profile-node-rename"'):
        assert marker in body, f"missing marker: {marker}"


def test_no_backslash_escapes_in_template():
    # The template is a non-raw Python string: a \n-style escape inside any
    # inline script would be eaten by Python and break the page silently.
    src = Path(profile_views.__file__).read_text(encoding="utf-8")
    template = src.split('PROFILE_TEMPLATE = """', 1)[1].split('"""', 1)[0]
    assert "\\" not in template
