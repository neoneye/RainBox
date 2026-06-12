"""Multi-provider sync: each provider syncs independently; an unreachable
provider does not affect the other's rows."""

from unittest.mock import patch

import pytest
import requests

from db import ModelConfig, db, init_db, make_app
from webapp.core import sync_models_from_providers


@pytest.fixture
def app_ctx():
    app = make_app()
    init_db(app)
    with app.app_context():
        yield app


class _FakeProvider:
    def __init__(self, pid, available_names, sizes=None, native=None,
                 reachable=True):
        self.id = pid
        self.display_name = pid
        self._available = available_names
        self._sizes = sizes or {}
        self._native = native
        self._reachable = reachable

    def base_url(self):
        return f"http://{self.id}.test"

    def list_models(self):
        if not self._reachable:
            raise requests.ConnectionError("down")
        return list(self._available)

    def fetch_native_models(self):
        return None if not self._reachable else self._native

    def fetch_model_sizes(self):
        return dict(self._sizes)

    def default_arguments(self):
        return {
            "api_base": f"http://{self.id}.test/v1",
            "api_key": "k",
            "is_chat_model": True,
            "is_function_calling_model": False,
            "should_use_structured_outputs": True,
            "timeout": 60.0,
        }

    def ensure_loaded(self, m, c):
        pass


def test_both_providers_synced_independently(app_ctx):
    fake_lm = _FakeProvider("lm_studio", ["pp3-syncprov-lm"])
    fake_ja = _FakeProvider("jan", ["pp3-syncprov-jan"])
    with patch("webapp.core.providers.all_providers",
               return_value=[fake_lm, fake_ja]):
        result = sync_models_from_providers()
    try:
        assert result["lm_studio"]["created"] >= 1
        assert result["jan"]["created"] >= 1
        lm_row = (
            db.session.query(ModelConfig)
            .filter_by(provider="lm_studio", model_name="pp3-syncprov-lm")
            .one()
        )
        ja_row = (
            db.session.query(ModelConfig)
            .filter_by(provider="jan", model_name="pp3-syncprov-jan")
            .one()
        )
        assert lm_row.available and ja_row.available
    finally:
        for r in (
            db.session.query(ModelConfig)
            .filter(ModelConfig.model_name.in_(
                ["pp3-syncprov-lm", "pp3-syncprov-jan"]
            ))
            .all()
        ):
            db.session.delete(r)
        db.session.commit()


def test_unreachable_provider_does_not_disable_other_providers_rows(app_ctx):
    """Pre-seed an LM Studio row; Jan being unreachable must not flip it."""
    seed = ModelConfig(
        provider="lm_studio",
        model_name="pp3-syncprov-keepavail",
        arguments={},
        available=True,
    )
    db.session.add(seed)
    db.session.commit()
    try:
        fake_lm = _FakeProvider("lm_studio", ["pp3-syncprov-keepavail"])
        fake_ja_down = _FakeProvider("jan", [], reachable=False)
        with patch("webapp.core.providers.all_providers",
                   return_value=[fake_lm, fake_ja_down]):
            result = sync_models_from_providers()
        assert result["jan"] is None
        db.session.refresh(seed)
        assert seed.available is True
    finally:
        db.session.delete(seed)
        db.session.commit()
