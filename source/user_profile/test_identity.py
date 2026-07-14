"""Tests for the operator identity block: the profile.current setting selects
a /profile person profile, and its filled-in fields render into the
<operator_identity> prompt block.

Deterministic and model-free: rendering is registry-driven text assembly.
"""

from uuid import uuid4

import pytest

import db
from db.models import Profile
from user_profile.identity import (
    build_identity_block,
    current_profile,
    format_identity_block,
)


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    try:
        yield app
    finally:
        db.db.session.rollback()
        ctx.pop()


@pytest.fixture
def profile_row(app_ctx):
    """A user-owned profile row, cleaned up (with the setting) afterwards."""
    row = Profile(uuid=uuid4(), name="Test Operator", position=0, data={
        "full_name": "Ada Lovelace",
        "preferred_name": "Ada",
        "about": "mathematician, first programmer",
        "units": "metric",
    })
    db.db.session.add(row)
    db.db.session.commit()
    try:
        yield row
    finally:
        db.set_setting("profile.current", None)
        db.db.session.delete(row)
        db.db.session.commit()


def test_format_identity_block_renders_filled_fields_in_registry_order(profile_row):
    block = format_identity_block(db.profile_get(profile_row.uuid))
    lines = block.splitlines()
    assert lines[0] == "Who the operator is (profile: Test Operator):"
    assert lines[1:] == [
        "- Full name: Ada Lovelace",
        "- Address them as: Ada",
        "- About: mathematician, first programmer",
        "- Units: metric",
    ]


def test_format_identity_block_skips_blank_fields(app_ctx):
    block = format_identity_block(
        {"name": "Sparse", "data": {"full_name": "  ", "city": "Copenhagen"}})
    assert block == "Who the operator is (profile: Sparse):\n- City: Copenhagen"


def test_unset_setting_means_no_block(app_ctx):
    db.set_setting("profile.current", None)
    assert current_profile() is None
    assert build_identity_block() == ""


def test_setting_selects_profile_and_builds_block(profile_row):
    db.set_setting("profile.current", str(profile_row.uuid))
    profile = current_profile()
    assert profile is not None and profile["name"] == "Test Operator"
    assert "- Full name: Ada Lovelace" in build_identity_block()


def test_deleted_profile_degrades_to_empty_block(app_ctx):
    """A selected-then-deleted profile must not break prompt assembly."""
    row = Profile(uuid=uuid4(), name="Doomed", position=0, data={})
    db.db.session.add(row)
    db.db.session.commit()
    db.set_setting("profile.current", str(row.uuid))
    try:
        db.db.session.delete(row)
        db.db.session.commit()
        assert current_profile() is None
        assert build_identity_block() == ""
    finally:
        db.set_setting("profile.current", None)


def test_validator_rejects_non_uuid_and_unknown_uuid(app_ctx):
    with pytest.raises(ValueError, match="not a uuid"):
        db.set_setting("profile.current", "not-a-uuid")
    with pytest.raises(ValueError, match="no profile with uuid"):
        db.set_setting("profile.current", str(uuid4()))


def test_builtin_template_is_selectable(app_ctx):
    """Built-in templates are valid identities (they resolve via profile_get)."""
    entry = db.profile_templates_entries()[0]
    try:
        db.set_setting("profile.current", entry["uuid"])
        profile = current_profile()
        assert profile is not None and profile["uuid"] == entry["uuid"]
    finally:
        db.set_setting("profile.current", None)
