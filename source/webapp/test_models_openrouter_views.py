"""The /model Add-model overlay: catalog endpoint, add endpoint, and the
markup that drives them. OpenRouter's HTTP catalog is stubbed throughout."""

from unittest.mock import patch

import pytest

from db import ModelConfig, db, init_db, make_app
from providers import openrouter as openrouter_mod
from webapp.core import app

_CATALOG = [
    {
        "id": "vendor/tooled",
        "name": "Vendor: Tooled",
        "context_length": 128000,
        "pricing": {"prompt": "0.00000015", "completion": "0.0000006"},
        "supported_parameters": ["tools", "structured_outputs"],
        "top_provider": {"max_completion_tokens": 16384},
        "capabilities": ["tool_use"],
    },
    {
        "id": "vendor/plain",
        "name": "Vendor: Plain",
        "context_length": 32768,
        "pricing": {"prompt": "0", "completion": "-1"},
        "supported_parameters": ["reasoning"],
        "capabilities": [],
    },
]


@pytest.fixture
def app_ctx():
    a = make_app()
    init_db(a)
    with a.app_context():
        yield a


@pytest.fixture
def stub_catalog():
    with patch(
        "providers.openrouter.fetch_catalog", return_value=list(_CATALOG)
    ) as m:
        yield m


def _delete(model_name: str) -> None:
    row = (
        db.session.query(ModelConfig)
        .filter(
            ModelConfig.provider == "openrouter",
            ModelConfig.model_name == model_name,
        )
        .one_or_none()
    )
    if row is not None:
        db.session.delete(row)
        db.session.commit()


def test_catalog_endpoint_flattens_entries(app_ctx, stub_catalog):
    payload = app.test_client().get("/model/api/openrouter/models").get_json()
    assert payload["ok"] is True
    by_id = {m["id"]: m for m in payload["models"]}
    tooled = by_id["vendor/tooled"]
    assert tooled["context_length"] == 128000
    assert tooled["tools"] is True
    assert tooled["structured_outputs"] is True
    assert tooled["reasoning"] is False
    # USD per token in the catalog, USD per million tokens on the wire.
    assert tooled["prompt_price"] == pytest.approx(0.15)
    assert tooled["completion_price"] == pytest.approx(0.60)
    plain = by_id["vendor/plain"]
    assert plain["prompt_price"] == 0
    # "-1" means the price varies; that reads as unknown, not as free.
    assert plain["completion_price"] is None
    assert plain["reasoning"] is True


def test_catalog_endpoint_marks_models_already_added(app_ctx, stub_catalog):
    client = app.test_client()
    client.post(
        "/model/api/openrouter/add", json={"model_name": "vendor/plain"}
    )
    try:
        payload = client.get("/model/api/openrouter/models").get_json()
        by_id = {m["id"]: m for m in payload["models"]}
        assert by_id["vendor/plain"]["added"] is True
        assert by_id["vendor/tooled"]["added"] is False
    finally:
        _delete("vendor/plain")


def test_catalog_endpoint_names_the_missing_key(app_ctx, monkeypatch):
    monkeypatch.delenv(openrouter_mod.API_KEY_ENV, raising=False)
    openrouter_mod.reset_catalog_cache()
    payload = app.test_client().get("/model/api/openrouter/models").get_json()
    assert payload["ok"] is False
    assert openrouter_mod.API_KEY_ENV in payload["error"]


def test_add_seeds_arguments_from_the_catalog_entry(app_ctx, stub_catalog):
    resp = app.test_client().post(
        "/model/api/openrouter/add", json={"model_name": "vendor/tooled"}
    )
    try:
        body = resp.get_json()
        assert body["ok"] is True
        row = (
            db.session.query(ModelConfig)
            .filter(ModelConfig.model_name == "vendor/tooled")
            .one()
        )
        assert row.provider == "openrouter"
        # Without these the llama-index wrapper would default to 3900 / 256.
        assert row.arguments["context_window"] == 128000
        assert row.arguments["max_tokens"] == 16384
        assert row.arguments["is_function_calling_model"] is True
        assert row.arguments["should_use_structured_outputs"] is True
        # The key stays in .env — never in the row, which /model prints verbatim.
        assert "api_key" not in row.arguments
    finally:
        _delete("vendor/tooled")


def test_add_reflects_a_models_lack_of_capabilities(app_ctx, stub_catalog):
    app.test_client().post(
        "/model/api/openrouter/add", json={"model_name": "vendor/plain"}
    )
    try:
        row = (
            db.session.query(ModelConfig)
            .filter(ModelConfig.model_name == "vendor/plain")
            .one()
        )
        assert row.arguments["is_function_calling_model"] is False
        assert row.arguments["should_use_structured_outputs"] is False
        # No top_provider.max_completion_tokens — the provider default holds.
        assert row.arguments["max_tokens"] == 4096
    finally:
        _delete("vendor/plain")


def test_adding_twice_returns_the_existing_row(app_ctx, stub_catalog):
    client = app.test_client()
    first = client.post(
        "/model/api/openrouter/add", json={"model_name": "vendor/plain"}
    ).get_json()
    try:
        second = client.post(
            "/model/api/openrouter/add", json={"model_name": "vendor/plain"}
        ).get_json()
        assert second["ok"] is True
        assert second["existing"] is True
        assert second["uuid"] == first["uuid"]
    finally:
        _delete("vendor/plain")


def test_add_rejects_a_model_outside_the_catalog(app_ctx, stub_catalog):
    resp = app.test_client().post(
        "/model/api/openrouter/add", json={"model_name": "vendor/not-real"}
    )
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_add_requires_a_model_name(app_ctx, stub_catalog):
    resp = app.test_client().post("/model/api/openrouter/add", json={})
    assert resp.status_code == 400


def test_page_carries_the_add_model_button_and_overlay(app_ctx):
    html = app.test_client().get("/model").get_data(as_text=True)
    assert 'id="pp-add-model-btn"' in html
    assert 'id="pp-addmodel-modal"' in html
    assert 'id="pp-addmodel-filter"' in html
    assert 'id="pp-addmodel-confirm"' in html
    # The overlay is a sibling of the shared backdrop, per notes/ui-modals.md.
    assert 'id="ui-modal-backdrop"' in html
