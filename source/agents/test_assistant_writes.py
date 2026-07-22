"""End-to-end tests for the first write family (PR 9 / Phase 5):

- log-and-undo tier (`remember`): executes immediately, creates an inert
  candidate, leaves a reversible trace.
- confirm tier (`memory_activate`): the assistant only *proposes*; it is never
  executed inline. Execution requires an approved intent and is bound to the
  exact proposed payload.
"""

from uuid import UUID, uuid4

import pytest

import db
from db import AssistantRun, AssistantWriteIntent, MemoryClaim
from db.memory import belief_keys
from db.models import MemoryEvidence, MemoryRejectedValue
from agents.assistant import (
    CAPABILITIES,
    AssistantActionContext,
    AssistantActionName,
    AssistantAgent,
    AssistantStepDecision,
    _action_remember,
)
from agents.assistant_fakes import scripted_decisions
from agents.assistant_writes import execute_write_intent, reject_write_intent
from agents.config import ASSISTANT_UUID


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    try:
        yield app
    finally:
        db.db.session.rollback()
        ctx.pop()


@pytest.fixture
def room(app_ctx):
    human = db.get_human_user()
    assert human is not None
    chatroom = db.create_chatroom(f"write-test-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])
    db.post_chat_message(chatroom.uuid, human.uuid, "please remember something")
    try:
        yield chatroom.uuid
    finally:
        db.db.session.query(AssistantRun).filter(
            AssistantRun.room_uuid == chatroom.uuid
        ).delete()
        db.db.session.query(db.Chatroom).filter(db.Chatroom.uuid == chatroom.uuid).delete()
        db.db.session.commit()


def _agent() -> AssistantAgent:
    return AssistantAgent(agent_uuid=ASSISTANT_UUID, name="assistant", send=lambda _: None)


def _decision(action, **args):
    if action is AssistantActionName.REPLY:
        args.setdefault("audit", "OK")
    return AssistantStepDecision(reason="step", action=action, args=args)


# --- registry metadata --------------------------------------------------------


def test_write_capabilities_declare_tiers():
    remember = CAPABILITIES[AssistantActionName.MEMORY_REMEMBER]
    activate = CAPABILITIES[AssistantActionName.MEMORY_ACTIVATE]
    assert remember.write is True and remember.tier == "log_and_undo"
    assert activate.write is True and activate.tier == "confirm"


# --- log-and-undo: remember ---------------------------------------------------


def test_remember_observation_carries_the_candidate_uuid(room):
    """The observation must surface the new candidate's uuid, so a follow-up
    activate/forget uses the real uuid instead of inventing one (run 38)."""
    ctx = AssistantActionContext(
        journal_id=None, room_uuid=room, agent_uuid=ASSISTANT_UUID, step_index=0)
    text = f"I use a non-electric bicycle {uuid4().hex[:6]}"
    try:
        obs = _action_remember(ctx, {"text": text})
        assert obs.ok is True
        mem_uuid = obs.data["memory_uuid"]
        assert mem_uuid in obs.text                          # uuid is visible to the model
        assert "never invent" in obs.text.lower()            # and told not to fabricate one
        # the reply surfaces a /memory link so the operator can verify the claim
        assert obs.data["link"] == f"/memory?id={mem_uuid}"
    finally:
        db.db.session.query(MemoryClaim).filter(MemoryClaim.text == text).delete()
        db.db.session.commit()


def test_remember_dedupes_an_existing_claim(room):
    """Remembering the same fact twice (any casing/whitespace) must not create a
    second claim — the action returns the existing one with a /memory link and
    records no extra ledger row."""
    agent = _agent()
    text = f"Simon has a Triangle Draw mug {uuid4().hex[:6]}"
    agent._decide_next_step = scripted_decisions(
        _decision(AssistantActionName.MEMORY_REMEMBER, text=text),
        _decision(AssistantActionName.REPLY, message="Noted."))
    try:
        agent.handle(uuid4(), {"room_uuid": str(room)})
        first = db.db.session.query(MemoryClaim).filter(MemoryClaim.text == text).all()
        assert len(first) == 1
        existing_uuid = first[0].uuid

        # Ask again with different casing/spacing — should resolve to the same claim.
        agent2 = _agent()
        agent2._decide_next_step = scripted_decisions(
            _decision(AssistantActionName.MEMORY_REMEMBER, text="  simon HAS a triangle draw MUG " + text.split(" ")[-1]),
            _decision(AssistantActionName.REPLY, message="Already have it."))
        agent2.handle(uuid4(), {"room_uuid": str(room)})
        # still exactly one claim with the original text; no second row created
        again = db.db.session.query(MemoryClaim).filter(
            MemoryClaim.room_uuid == room,
            MemoryClaim.text.ilike("%triangle draw mug%")).all()
        assert len(again) == 1 and again[0].uuid == existing_uuid

        obs = _action_remember(
            AssistantActionContext(journal_id=None, room_uuid=room,
                                   agent_uuid=ASSISTANT_UUID, step_index=0),
            {"text": text.upper()})
        assert obs.data["noop"] is True
        assert obs.data["memory_uuid"] == str(existing_uuid)
        assert obs.data["link"] == f"/memory?id={existing_uuid}"
        assert "undo" not in obs.data
    finally:
        db.db.session.query(MemoryClaim).filter(MemoryClaim.room_uuid == room).delete()
        db.db.session.commit()


def test_remember_creates_candidate_memory_and_is_undoable(room):
    """_action_remember creates a candidate (not active) — assistant_interpreted
    actor is candidate-by-default per spec §3.1."""
    agent = _agent()
    text = f"the build server is ci-{uuid4().hex[:6]}"
    agent._decide_next_step = scripted_decisions(
        _decision(AssistantActionName.MEMORY_REMEMBER, text=text),
        _decision(AssistantActionName.REPLY, message="Noted."),
    )
    try:
        result = agent.handle(uuid4(), {"room_uuid": str(room)})
        assert result["status"] == "finished"
        claims = db.db.session.query(MemoryClaim).filter(MemoryClaim.text == text).all()
        assert len(claims) == 1
        assert claims[0].status == "candidate"  # assistant_interpreted → candidate
        # Undo: rejecting it reverses the write.
        db.reject_memory(claims[0].uuid, {"provenance": "confirmed_by_user",
                                          "source_type": "manual"})
        assert db.get_memory_claim(claims[0].uuid).status == "rejected"
    finally:
        db.db.session.query(MemoryClaim).filter(MemoryClaim.text == text).delete()
        db.db.session.commit()


def test_remember_is_undoable_through_the_write_intent_ledger(room):
    """A log-and-undo `remember` must carry a working inverse so the operator can
    undo it via the same endpoint as every other log-and-undo write."""
    from agents.assistant_writes import undo_write_intent

    agent = _agent()
    text = f"teal sky {uuid4().hex[:6]}"
    agent._decide_next_step = scripted_decisions(
        _decision(AssistantActionName.MEMORY_REMEMBER, text=text),
        _decision(AssistantActionName.REPLY, message="ok"),
    )
    try:
        result = agent.handle(uuid4(), {"room_uuid": str(room)})
        intent = db.db.session.query(AssistantWriteIntent).filter(
            AssistantWriteIntent.room_uuid == room).one()
        assert intent.state == "completed"
        # The intent points at its producing step by uuid (the identity pointer),
        # and that step row exists in the run's trace.
        assert intent.step_uuid is not None
        step_uuids = {s.uuid for s in db.list_assistant_steps(result["assistant_run_uuid"])}
        assert intent.step_uuid in step_uuids
        mem_uuid = UUID(intent.result["undo"]["payload"]["memory_uuid"])
        assert db.get_memory_claim(mem_uuid).status == "candidate"  # active→candidate (spec §3.1)
        obs = undo_write_intent(intent.uuid)
        assert obs.ok is True
        assert db.get_memory_claim(mem_uuid).status == "rejected"  # undo rejected it
        assert db.get_write_intent(intent.uuid).state == "undone"
    finally:
        db.db.session.query(MemoryClaim).filter(MemoryClaim.text == text).delete()
        db.db.session.commit()


def test_undo_remember_leaves_no_tombstone_but_direct_reject_does(room):
    """Regression guard: undo-of-remember must NOT write a tombstone (so the
    value stays re-learnable), whereas a direct reject (with default
    tombstone=True) MUST write one. A regression that drops tombstone=False from
    _action_reject_memory_candidate would create an unwanted tombstone and fail
    the first assertion."""
    from agents.assistant_writes import undo_write_intent

    agent = _agent()
    text_undo = f"undo-no-tomb {uuid4().hex[:6]}"
    text_direct = f"direct-reject-tomb {uuid4().hex[:6]}"
    agent._decide_next_step = scripted_decisions(
        _decision(AssistantActionName.MEMORY_REMEMBER, text=text_undo),
        _decision(AssistantActionName.REPLY, message="ok"),
    )
    try:
        # --- path 1: remember then undo via write-intent ledger ---------------
        result = agent.handle(uuid4(), {"room_uuid": str(room)})
        intent = db.db.session.query(AssistantWriteIntent).filter(
            AssistantWriteIntent.run_uuid == result["assistant_run_uuid"]
        ).one()
        mem_uuid = UUID(intent.result["undo"]["payload"]["memory_uuid"])
        claim = db.get_memory_claim(mem_uuid)

        # Derive tombstone lookup keys before the undo (claim is candidate).
        sp_key, val_key = belief_keys(
            claim.subject, claim.predicate, claim.object, claim.text
        )

        obs = undo_write_intent(intent.uuid)
        assert obs.ok is True
        assert db.get_memory_claim(mem_uuid).status == "rejected"

        # Core assertion: undo must NOT write a tombstone.
        tomb = db.check_tombstone(
            claim.scope, claim.room_uuid, claim.agent_uuid, sp_key, val_key
        )
        assert tomb is None, (
            "undo-of-remember wrote a tombstone — tombstone=False was dropped "
            "from _action_reject_memory_candidate"
        )

        # --- path 2: direct reject (tombstone=True by default) for contrast ---
        claim2 = db.create_memory_claim(
            scope="room", kind="fact", text=text_direct, confidence=1.0,
            status="active", sensitivity="private",
            agent_uuid=ASSISTANT_UUID, room_uuid=room,
        )
        sp2, val2 = belief_keys(
            claim2.subject, claim2.predicate, claim2.object, claim2.text
        )
        db.reject_memory(claim2.uuid, {"provenance": "confirmed_by_user",
                                       "source_type": "manual"})
        # Default tombstone=True: a tombstone MUST be present.
        tomb2 = db.check_tombstone(
            claim2.scope, claim2.room_uuid, claim2.agent_uuid, sp2, val2
        )
        assert tomb2 is not None, (
            "direct reject (tombstone=True) did not write a tombstone — "
            "tombstone logic is broken"
        )
    finally:
        db.db.session.query(MemoryRejectedValue).filter(
            MemoryRejectedValue.room_uuid == room
        ).delete()
        db.db.session.query(MemoryEvidence).filter(
            MemoryEvidence.memory_uuid.in_(
                db.db.session.query(MemoryClaim.uuid).filter(
                    MemoryClaim.room_uuid == room
                )
            )
        ).delete(synchronize_session=False)
        db.db.session.query(MemoryClaim).filter(MemoryClaim.room_uuid == room).delete()
        db.db.session.commit()


# --- confirm tier: memory_activate --------------------------------------------


def _candidate(text):
    return db.create_memory_claim(
        scope="global", kind="fact", text=text, confidence=0.5,
        status="candidate", sensitivity="public", subject="write-test",
    )


def test_confirm_tier_proposes_without_executing(room):
    cand = _candidate(f"candidate fact {uuid4().hex[:6]}")
    agent = _agent()
    agent._decide_next_step = scripted_decisions(
        _decision(AssistantActionName.MEMORY_ACTIVATE, memory_uuid=str(cand.uuid)),
        _decision(AssistantActionName.REPLY, message="Proposed."),
    )
    try:
        result = agent.handle(uuid4(), {"room_uuid": str(room)})
        # An intent was proposed; the claim was NOT activated inline.
        intents = (
            db.db.session.query(AssistantWriteIntent)
            .filter(AssistantWriteIntent.run_uuid == result["assistant_run_uuid"])
            .all()
        )
        assert len(intents) == 1
        assert intents[0].state == "proposed"
        assert db.get_memory_claim(cand.uuid).status == "candidate"  # not executed
    finally:
        db.db.session.query(MemoryClaim).filter(MemoryClaim.subject == "write-test").delete()
        db.db.session.commit()


def test_confirm_then_execute_activates_claim(room):
    cand = _candidate(f"to activate {uuid4().hex[:6]}")
    agent = _agent()
    agent._decide_next_step = scripted_decisions(
        _decision(AssistantActionName.MEMORY_ACTIVATE, memory_uuid=str(cand.uuid)),
        _decision(AssistantActionName.REPLY, message="Proposed."),
    )
    try:
        result = agent.handle(uuid4(), {"room_uuid": str(room)})
        intent = (
            db.db.session.query(AssistantWriteIntent)
            .filter(AssistantWriteIntent.run_uuid == result["assistant_run_uuid"])
            .one()
        )
        # Operator confirms -> execution activates the claim.
        obs = execute_write_intent(intent.uuid, confirmed_by_uuid=uuid4())
        assert obs.ok
        assert db.get_memory_claim(cand.uuid).status == "active"
        db.db.session.refresh(intent)
        assert intent.state == "completed"
    finally:
        db.db.session.query(MemoryClaim).filter(MemoryClaim.subject == "write-test").delete()
        db.db.session.commit()


def test_execute_refused_unless_proposed(room):
    cand = _candidate(f"reject me {uuid4().hex[:6]}")
    agent = _agent()
    agent._decide_next_step = scripted_decisions(
        _decision(AssistantActionName.MEMORY_ACTIVATE, memory_uuid=str(cand.uuid)),
        _decision(AssistantActionName.REPLY, message="Proposed."),
    )
    try:
        result = agent.handle(uuid4(), {"room_uuid": str(room)})
        intent = (
            db.db.session.query(AssistantWriteIntent)
            .filter(AssistantWriteIntent.run_uuid == result["assistant_run_uuid"])
            .one()
        )
        # Reject it, then a confirm/execute attempt must do nothing.
        assert reject_write_intent(intent.uuid) is True
        assert db.get_memory_claim(cand.uuid).status == "candidate"
        obs = execute_write_intent(intent.uuid, confirmed_by_uuid=uuid4())
        assert obs.ok is False  # cannot execute a rejected intent
        assert db.get_memory_claim(cand.uuid).status == "candidate"
    finally:
        db.db.session.query(MemoryClaim).filter(MemoryClaim.subject == "write-test").delete()
        db.db.session.commit()


def test_execute_refuses_non_confirm_tier_capability(app_ctx):
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=uuid4(), agent_uuid=ASSISTANT_UUID, step_limit=6,
    )
    # 'remember' is log_and_undo, not confirm — a proposed intent for it must be refused.
    intent = db.create_write_intent(
        run_uuid=run.uuid, capability_name="memory_remember",
        payload={"text": "x"}, preview_text="remember: …",
        room_uuid=run.room_uuid, agent_uuid=ASSISTANT_UUID,
    )
    try:
        obs = execute_write_intent(intent.uuid)
        assert obs.ok is False
        refreshed = db.get_write_intent(intent.uuid)
        assert refreshed.state == "failed"
    finally:
        db.db.session.query(AssistantWriteIntent).filter(
            AssistantWriteIntent.run_uuid == run.uuid
        ).delete()
        db.db.session.query(AssistantRun).filter(AssistantRun.uuid == run.uuid).delete()
        db.db.session.commit()
