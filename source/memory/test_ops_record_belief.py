"""ops remember/correct go through record_belief as explicit_human_command."""
import pytest
from uuid import uuid4
import db
from memory.ops import _handle_remember, _handle_correct
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


def _cleanup_by_room(room_uuid):
    """Delete all memory rows seeded in the given room_uuid (including global/None-room claims
    tagged via a special text prefix that callers must use)."""
    pass  # Not needed here; each test cleans up by text or uuid directly.


def test_handle_remember_creates_active_global_claim(app_ctx):
    out = _handle_remember(_Ctx(uuid4()), "alice is happy")
    assert "Remembered" in out
    claim = db.db.session.query(MemoryClaim).filter_by(text="alice is happy").first()
    assert claim.status == "active" and claim.scope == "global"
    assert claim.subj_pred_key   # keyed
    _cleanup("alice is happy")


# ---------------------------------------------------------------------------
# P1: _handle_correct must always leave an ACTIVE replacement
# ---------------------------------------------------------------------------

def test_correct_via_candidate_leaves_active_replacement(app_ctx):
    """When record_belief returns outcome='corroborated' with a pre-existing
    CANDIDATE claim, _handle_correct must activate the candidate AND supersede
    the old claim — so the correction leaves exactly one active belief.

    Setup:
    - Create A as active ("p1 sky is red")
    - Create B as candidate ("p1 sky is blue") — same text that will be the correction target
    - Run _handle_correct(A -> B text)
    Expected:
    - A is superseded
    - B (or a claim with text "p1 sky is blue") is active
    """
    marker = f"p1-correct-{uuid4().hex[:8]}"
    text_a = f"{marker} sky is red"
    text_b = f"{marker} sky is blue"

    # Pre-create B as a candidate (simulating a prior model-inferred suggestion not yet confirmed)
    b_claim = db.create_memory_claim(
        scope="global", kind="fact", text=text_b,
        confidence=0.5, status="candidate", sensitivity="private",
    )
    # Create A as active via record_belief (explicit human command)
    a_result = db.record_belief(
        actor="explicit_human_command", scope="global", kind="fact",
        text=text_a, confidence=1.0, sensitivity="private",
        evidence={"provenance": "confirmed_by_user", "source_type": "manual",
                  "excerpt": "setup"},
    )
    assert a_result.outcome in ("created", "corroborated"), \
        f"Unexpected outcome setting up A: {a_result.outcome}"
    a_claim = a_result.claim or db.db.session.query(MemoryClaim).filter_by(text=text_a).first()
    assert a_claim is not None and a_claim.status == "active"

    class _CorrectCtx:
        query = f"correct that {text_a} -> {text_b}"
        payload = {"message_uuid": str(uuid4())}
        room_uuid = None

    try:
        reply = _handle_correct(_CorrectCtx(), text_a, text_b)

        db.db.session.expire_all()

        # A must be superseded
        a_reloaded = db.get_memory_claim(a_claim.uuid)
        assert a_reloaded is not None, "Original claim A was deleted — should be superseded, not gone"
        assert a_reloaded.status == "superseded", \
            f"Expected A to be superseded, got {a_reloaded.status!r}"

        # Some claim with text B must be active
        active_b = (
            db.db.session.query(MemoryClaim)
            .filter(MemoryClaim.text == text_b, MemoryClaim.status == "active")
            .first()
        )
        assert active_b is not None, (
            f"No active claim with text {text_b!r} found after correct; reply={reply!r}"
        )
    finally:
        # Clean up: delete by text (both A and B texts, all statuses)
        for text in (text_a, text_b):
            rows = db.db.session.query(MemoryClaim).filter(MemoryClaim.text == text).all()
            for r in rows:
                db.db.session.query(MemoryEvidence).filter_by(memory_uuid=r.uuid).delete()
            db.db.session.query(MemoryClaim).filter(MemoryClaim.text == text).delete()
        from db.models import MemoryRejectedValue
        db.db.session.query(MemoryRejectedValue).filter(
            MemoryRejectedValue.value_key.in_([text_a.lower(), text_b.lower()])
        ).delete(synchronize_session=False)
        db.db.session.commit()
