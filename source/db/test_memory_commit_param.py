"""commit=False defers the transaction so record_belief can be atomic."""
import pytest
import db


@pytest.fixture
def app_ctx():
    app = db.make_app(); db.init_db(app)
    ctx = app.app_context(); ctx.push()
    try:
        yield app
    finally:
        ctx.pop()


def test_create_with_commit_false_is_rolled_back(app_ctx):
    from uuid import uuid4
    marker = uuid4()
    c = db.create_memory_claim(scope="global", kind="fact", text="nocommit",
                               confidence=0.5, status="active",
                               room_uuid=marker, commit=False)
    assert c.uuid is not None            # flush assigned it
    db.db.session.rollback()
    assert db.get_memory_claim(c.uuid) is None   # nothing persisted


def test_delete_memory_embeddings_commit_false_is_rolled_back(app_ctx):
    """delete_memory_embeddings(commit=False) must leave the row when rolled back."""
    claim = db.create_memory_claim(
        scope="global", kind="fact", text="embedding target for rollback test",
        confidence=0.9, status="active", sensitivity="public",
    )
    vec = [0.1] * 768
    db.upsert_memory_embedding(
        memory_uuid=claim.uuid, model_name="test-model",
        embed_dim=768, text_hash="testhash", embedding=vec,
    )
    # Verify embedding exists before deletion attempt
    assert db.get_memory_embedding(claim.uuid, "test-model") is not None

    # Delete with commit=False then rollback — embedding must survive
    n = db.delete_memory_embeddings(claim.uuid, commit=False)
    assert n == 1
    db.db.session.rollback()
    assert db.get_memory_embedding(claim.uuid, "test-model") is not None, (
        "embedding was committed despite commit=False"
    )

    # Cleanup: delete with default commit=True — embedding must be gone
    n2 = db.delete_memory_embeddings(claim.uuid)
    assert n2 == 1
    assert db.get_memory_embedding(claim.uuid, "test-model") is None

    # Cleanup claim
    db.db.session.query(db.MemoryClaim).filter_by(uuid=claim.uuid).delete()
    db.db.session.commit()


def test_create_accepts_trust_kwargs(app_ctx):
    from uuid import uuid4
    marker = uuid4()
    c = db.create_memory_claim(scope="global", kind="fact", text="kw",
                               confidence=0.5, status="active", room_uuid=marker,
                               support_count=1, epistemic_confidence=0.5,
                               retrieval_strength=0.5, subj_pred_key="a\x1fis",
                               value_key="b", key_version=1)
    got = db.get_memory_claim(c.uuid)
    assert got.support_count == 1 and got.subj_pred_key == "a\x1fis"
    db.db.session.query(db.MemoryClaim).filter_by(uuid=c.uuid).delete()
    db.db.session.commit()
