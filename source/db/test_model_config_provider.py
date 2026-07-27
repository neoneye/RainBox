"""Schema tests for the new ModelConfig.provider column. Uses a real
Postgres connection (per project convention — no SQLite). Each test
cleans up the row it creates."""

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from db import (
    ModelConfig,
    db,
    init_db,
    list_model_configs_with_overrides,
    make_app,
)


@pytest.fixture
def app_ctx():
    app = make_app()
    init_db(app)
    with app.app_context():
        yield app


def test_provider_column_exists_and_defaults_to_ollama(app_ctx):
    row = ModelConfig(model_name="pp3-test-provider-col-default", arguments={})
    db.session.add(row)
    db.session.commit()
    try:
        assert row.provider == "ollama"
        column_default = db.session.execute(sa.text(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_name='model_config' AND column_name='provider'"
        )).scalar_one()
        assert "ollama" in column_default
    finally:
        db.session.delete(row)
        db.session.commit()


def test_provider_sort_puts_ollama_first(app_ctx):
    suffix = "pp3-test-provider-order"
    rows = [
        ModelConfig(
            model_name=f"{suffix}-{provider}",
            arguments={},
            provider=provider,
        )
        for provider in ("lm_studio", "ollama", "jan")
    ]
    db.session.add_all(rows)
    db.session.commit()
    try:
        ordered = [
            cfg.provider
            for cfg, _overrides in list_model_configs_with_overrides()
            if cfg.uuid in {row.uuid for row in rows}
        ]
        assert ordered == ["ollama", "jan", "lm_studio"]
    finally:
        for row in rows:
            db.session.delete(row)
        db.session.commit()


def test_can_create_same_model_name_under_two_providers(app_ctx):
    a = ModelConfig(model_name="pp3-test-dup-name", arguments={}, provider="lm_studio")
    b = ModelConfig(model_name="pp3-test-dup-name", arguments={}, provider="jan")
    db.session.add_all([a, b])
    db.session.commit()
    try:
        rows = (
            db.session.query(ModelConfig)
            .filter_by(model_name="pp3-test-dup-name")
            .all()
        )
        providers = {r.provider for r in rows}
        assert providers == {"lm_studio", "jan"}
    finally:
        for r in (a, b):
            db.session.delete(r)
        db.session.commit()


def test_resolved_model_kwargs_returns_provider_id(app_ctx):
    from db import resolved_model_kwargs

    row = ModelConfig(
        provider="jan",
        model_name="pp3-test-resolved-shape",
        arguments={"api_base": "http://j/v1", "api_key": "jan"},
    )
    db.session.add(row)
    db.session.commit()
    try:
        provider_id, model_name, args = resolved_model_kwargs(row.uuid)
        assert provider_id == "jan"
        assert model_name == "pp3-test-resolved-shape"
        assert args["api_base"] == "http://j/v1"
    finally:
        db.session.delete(row)
        db.session.commit()


def test_duplicate_provider_plus_model_name_rejected(app_ctx):
    a = ModelConfig(model_name="pp3-test-dup-pair", arguments={}, provider="lm_studio")
    db.session.add(a)
    db.session.commit()
    try:
        b = ModelConfig(
            model_name="pp3-test-dup-pair", arguments={}, provider="lm_studio"
        )
        db.session.add(b)
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()
    finally:
        db.session.delete(a)
        db.session.commit()
