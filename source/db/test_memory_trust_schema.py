"""Schema for Tier 1 memory trust hardening: tombstone table + claim columns."""
import sqlalchemy as sa
import pytest
import db
from db import MemoryClaim
from db.models import MemoryRejectedValue


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    try:
        yield app
    finally:
        ctx.pop()


def test_memory_claim_has_trust_columns(app_ctx):
    cols = {c["name"] for c in sa.inspect(db.db.engine).get_columns("memory_claim")}
    assert {"conflicts_with_uuid", "epistemic_confidence", "retrieval_strength",
            "support_count", "subj_pred_key", "value_key", "key_version"} <= cols


def test_rejected_value_table_exists(app_ctx):
    cols = {c["name"] for c in sa.inspect(db.db.engine).get_columns("memory_rejected_value")}
    assert {"scope", "subj_pred_key", "value_key", "claim_text", "evidence_summary",
            "hit_count", "last_hit_at", "created_from_uuid"} <= cols


def test_unique_tombstone_index_exists(app_ctx):
    idx = {i["name"] for i in sa.inspect(db.db.engine).get_indexes("memory_rejected_value")}
    assert "memory_rejected_value_key_uniq" in idx
