"""A curated provider (Provider.curated — OpenRouter) syncs availability but
never creates rows: its catalog is several hundred models and mirroring it
would bury the /model tree."""

import pytest

from db import ModelConfig, db, init_db, make_app, sync_model_configs


@pytest.fixture
def app_ctx():
    app = make_app()
    init_db(app)
    with app.app_context():
        yield app


def _rows(provider: str) -> list[ModelConfig]:
    return (
        db.session.query(ModelConfig).filter(ModelConfig.provider == provider).all()
    )


def _insert(provider: str, name: str, available: bool = True) -> ModelConfig:
    row = ModelConfig(
        provider=provider,
        model_name=name,
        arguments={},
        available=available,
    )
    db.session.add(row)
    db.session.commit()
    return row


def test_curated_sync_creates_nothing(app_ctx):
    before = len(_rows("openrouter"))
    summary = sync_model_configs(
        provider="openrouter",
        available_model_names=["vendor/a", "vendor/b", "vendor/c"],
        default_arguments={"api_base": "https://openrouter.ai/api/v1"},
        create_missing=False,
    )
    assert summary["created"] == 0
    assert len(_rows("openrouter")) == before


def test_curated_sync_still_re_enables_and_disables_existing_rows(app_ctx):
    stale = _insert("openrouter", "pp3-curated-gone", available=True)
    back = _insert("openrouter", "pp3-curated-back", available=False)
    try:
        summary = sync_model_configs(
            provider="openrouter",
            available_model_names=["pp3-curated-back", "vendor/never-added"],
            default_arguments={"api_base": "https://openrouter.ai/api/v1"},
            create_missing=False,
        )
        db.session.refresh(stale)
        db.session.refresh(back)
        assert back.available is True
        assert stale.available is False
        assert summary["created"] == 0
        assert summary["re_enabled"] == 1
        assert summary["disabled"] == 1
    finally:
        for r in (stale, back):
            db.session.delete(r)
        db.session.commit()


def test_non_curated_sync_still_creates(app_ctx):
    """The default is unchanged — local providers keep mirroring their whole
    model list into rows."""
    summary = sync_model_configs(
        provider="jan",
        available_model_names=["pp3-curated-control"],
        default_arguments={"api_base": "http://x/v1", "api_key": "jan"},
    )
    created = [r for r in _rows("jan") if r.model_name == "pp3-curated-control"]
    try:
        assert summary["created"] == 1
        assert len(created) == 1
    finally:
        for r in created:
            db.session.delete(r)
        db.session.commit()
