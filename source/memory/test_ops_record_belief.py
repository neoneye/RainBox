"""ops remember/correct go through record_belief as explicit_human_command."""
import pytest
from uuid import uuid4
import db
from memory.ops import _handle_remember
from db import MemoryClaim, MemoryEvidence
from db.models import MemoryRejectedValue


@pytest.fixture
def app_ctx():
    app = db.make_app(); db.init_db(app)
    ctx = app.app_context(); ctx.push()
    try:
        yield app
    finally:
        ctx.pop()


class _Ctx:
    def __init__(self, room):
        self.query = "remember that alice is happy"
        self.payload = {"message_uuid": str(uuid4())}
        self.room_uuid = room


def _cleanup(scope_text):
    rows = db.db.session.query(MemoryClaim).filter(MemoryClaim.text == scope_text).all()
    for r in rows:
        db.db.session.query(MemoryEvidence).filter_by(memory_uuid=r.uuid).delete()
    db.db.session.query(MemoryClaim).filter(MemoryClaim.text == scope_text).delete()
    db.db.session.commit()


def test_handle_remember_creates_active_global_claim(app_ctx):
    out = _handle_remember(_Ctx(uuid4()), "alice is happy")
    assert "Remembered" in out
    claim = db.db.session.query(MemoryClaim).filter_by(text="alice is happy").first()
    assert claim.status == "active" and claim.scope == "global"
    assert claim.subj_pred_key   # keyed
    _cleanup("alice is happy")
