"""sync_model_configs must scope availability reconciliation to the given
provider so syncing one backend doesn't flip the other's rows."""

import pytest

from db import ModelConfig, db, init_db, make_app, sync_model_configs


@pytest.fixture
def app_ctx():
    app = make_app()
    init_db(app)
    with app.app_context():
        yield app


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


def test_sync_of_one_provider_leaves_other_provider_rows_untouched(app_ctx):
    lm = _insert("lm_studio", "pp3-sync-test-keep")
    ja = _insert("jan", "pp3-sync-test-other")
    try:
        sync_model_configs(
            provider="lm_studio",
            available_model_names=["pp3-sync-test-keep"],
            default_arguments={"api_base": "x", "api_key": "y"},
        )
        db.session.refresh(lm)
        db.session.refresh(ja)
        assert lm.available is True
        assert ja.available is True
    finally:
        for r in (lm, ja):
            db.session.delete(r)
        db.session.commit()


def test_sync_disables_missing_rows_only_for_named_provider(app_ctx):
    lm = _insert("lm_studio", "pp3-sync-test-missing")
    ja = _insert("jan", "pp3-sync-test-missing")
    try:
        sync_model_configs(
            provider="lm_studio",
            available_model_names=[],
            default_arguments={"api_base": "x", "api_key": "y"},
        )
        db.session.refresh(lm)
        db.session.refresh(ja)
        assert lm.available is False
        assert ja.available is True
    finally:
        for r in (lm, ja):
            db.session.delete(r)
        db.session.commit()


def test_sync_creates_new_row_with_given_provider(app_ctx):
    sync_model_configs(
        provider="jan",
        available_model_names=["pp3-sync-test-new"],
        default_arguments={"api_base": "j", "api_key": "k"},
    )
    row = (
        db.session.query(ModelConfig)
        .filter_by(provider="jan", model_name="pp3-sync-test-new")
        .one()
    )
    try:
        assert row.arguments == {"api_base": "j", "api_key": "k"}
    finally:
        db.session.delete(row)
        db.session.commit()
