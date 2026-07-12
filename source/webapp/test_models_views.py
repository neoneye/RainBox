"""Tests for webapp/models_views.py.

The form-helper tests are pure (no DB, no HTTP). The /models page tests
need an app + DB and seed real rows; each cleans up after itself.
"""

from unittest.mock import patch

import pytest

from db import ModelConfig, db, init_db, make_app
from webapp.core import app
from webapp.models_views import _build_overrides_dict, _new_override_form_data


@pytest.fixture
def seeded_two_providers():
    """Seed one LM Studio row + one Jan row, then yield. Cleans up."""
    a = make_app()
    init_db(a)
    with a.app_context():
        lm = ModelConfig(
            provider="lm_studio",
            model_name="pp3-uitest-lm",
            arguments={"api_base": "http://x/v1", "api_key": "k"},
        )
        ja = ModelConfig(
            provider="jan",
            model_name="pp3-uitest-jan",
            arguments={"api_base": "http://y/v1", "api_key": "jan"},
        )
        db.session.add_all([lm, ja])
        db.session.commit()
        try:
            yield
        finally:
            for r in (lm, ja):
                db.session.delete(r)
            db.session.commit()


def test_models_tree_rows_carry_provider_badges(seeded_two_providers):
    """Both LM Studio and Jan rows render in the tree with the provider
    display name visible (via the badge text)."""
    client = app.test_client()
    resp = client.get("/models")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "pp3-uitest-lm" in body
    assert "pp3-uitest-jan" in body
    assert "LM Studio" in body
    assert "Jan" in body


def test_reload_returns_per_provider_summary():
    """The reload endpoint always returns ok=True with a per-provider
    summary dict; unreachable providers come back as None."""
    a = make_app()
    init_db(a)
    fake_summary = {
        "lm_studio": {"created": 0, "re_enabled": 0, "disabled": 0,
                      "function_calling_updated": 0},
        "jan": None,
    }
    with a.app_context():
        client = app.test_client()
        with patch("webapp.models_views.sync_models_from_providers",
                   return_value=fake_summary):
            resp = client.post("/models/api/reload")
        payload = resp.get_json()
        assert payload["ok"] is True
        assert payload["summary"]["lm_studio"]["created"] == 0
        assert payload["summary"]["jan"] is None


def _base_form(**overrides):
    form = {
        "display_name": "",
        "temperature": "0.5",
        "reasoning_effort": "none",
        "should_use_structured_outputs": "1",
        "context_window": "4096",
    }
    form.update(overrides)
    return form


def test_form_data_parses_context_window():
    fd = _new_override_form_data(_base_form(context_window="8192"))
    assert fd["context_window"] == 8192


def test_form_data_context_window_defaults_when_missing():
    form = _base_form()
    form.pop("context_window")
    fd = _new_override_form_data(form)
    # Default mirrors OpenAILike's default of 3900.
    assert fd["context_window"] == 3900


def test_form_data_context_window_invalid_falls_back():
    fd = _new_override_form_data(_base_form(context_window="not a number"))
    assert fd["context_window"] == 3900


def test_form_data_context_window_clamped_to_positive():
    fd = _new_override_form_data(_base_form(context_window="0"))
    assert fd["context_window"] == 1


def test_overrides_dict_includes_context_window():
    fd = _new_override_form_data(_base_form(context_window="16384"))
    ov = _build_overrides_dict(fd, "lm_studio")
    assert ov["context_window"] == 16384


def test_rename_goes_through_confirm_modal(seeded_two_providers):
    """Renaming is modal-confirmed (docs/ui-modal-rename.md): the detail pane
    shows the display name as a click-to-rename control whose modal submits
    the rename form, so a typed-but-unconfirmed name can't be silently lost."""
    from db import ModelConfig, db as _db
    client = app.test_client()
    cfg = (
        _db.session.query(ModelConfig)
        .filter(ModelConfig.model_name == "pp3-uitest-lm").one()
    )
    body = client.get(f"/models?id={cfg.uuid}").get_data(as_text=True)
    assert 'class="pp-rename-display"' in body
    assert 'id="pp-rename-modal"' in body
    assert 'id="pp-rename-input"' in body
    assert "function ppOpenRenameModal" in body
    assert "function ppConfirmRenameModal" in body
    # The old always-visible rename field + submit button are gone.
    assert '<button type="submit">Rename</button>' not in body
