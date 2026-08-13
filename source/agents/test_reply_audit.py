"""Tests for the reply audit: a separate model call that reads the finished
message before it posts, replacing the `2_audit` reply argument.

The property the old design enforced four times and still leaked — the audit
composed AFTER the message exists — is structural here: the message is a
string in the auditor's prompt, so it cannot be audited before it exists.

Deterministic: the decide seam is scripted (`scripted_decisions`) and the
audit seam is either monkeypatched at the agent method (loop tests) or
exercised for real with `agents.query_filter_router.structured_llm_call`
monkeypatched (unit tests).
"""

from uuid import uuid4

import pytest

import db
from db import AssistantRun
from agents.assistant import (
    CAPABILITIES,
    REPLY_AUDIT_TURN_INSTRUCTIONS,
    AssistantActionName,
    AssistantAgent,
    AssistantStepDecision,
    ReplyAudit,
)
from agents.assistant_fakes import scripted_decisions
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
        ctx.pop()


@pytest.fixture
def room(app_ctx):
    """A chatroom with the assistant as a member, plus one human message."""
    human = db.get_human_user()
    assert human is not None
    name = f"assistant-test-{uuid4().hex[:8]}"
    chatroom = db.create_chatroom(name, human.uuid, [ASSISTANT_UUID])
    msg = db.post_chat_message(
        chatroom.uuid, human.uuid, "how much is 12 feet in meters?")
    try:
        yield chatroom.uuid, msg.uuid
    finally:
        db.db.session.query(AssistantRun).filter(
            AssistantRun.room_uuid == chatroom.uuid
        ).delete()
        db.db.session.query(db.Chatroom).filter(
            db.Chatroom.uuid == chatroom.uuid
        ).delete()
        db.db.session.commit()


def _agent() -> AssistantAgent:
    return AssistantAgent(
        agent_uuid=ASSISTANT_UUID, name="assistant", send=lambda _: None
    )


def _reply(message: str) -> AssistantStepDecision:
    return AssistantStepDecision(
        reason="done", action=AssistantActionName.REPLY,
        args={"message": message},
    )


# --- the verdict schema -------------------------------------------------------


def test_send_verdict_parses_with_no_problems():
    audit = ReplyAudit.model_validate(
        {"reason": "answers the question in metric", "verdict": "send"})
    assert audit.verdict == "send"
    assert audit.problems == ""


def test_revise_verdict_carries_its_problems_as_one_text():
    """`problems` is one string, not a list of two-field objects: the nested
    shape asks a small local model to hold a container, a per-item schema and
    two required keys in mind while it is also judging the reply."""
    audit = ReplyAudit.model_validate({
        "reason": "the second question is unanswered",
        "problems": ("does not say what 12 feet is in meters — the reply "
                     "only converts to centimeters"),
        "verdict": "revise",
    })
    assert audit.verdict == "revise"
    assert audit.problems.startswith("does not say")


def test_a_list_of_problems_is_now_a_schema_error():
    """The previous shape must not silently pass: a model that still emits a
    list gets a retry from the structured-output layer, not a half-parsed
    verdict."""
    with pytest.raises(Exception):
        ReplyAudit.model_validate({
            "reason": "r", "verdict": "revise",
            "problems": [{"problem": "p", "evidence": "e"}]})


def test_the_findings_are_declared_before_the_verdict():
    """Field order is the contract: a grammar-constrained model fills the
    fields in the order the schema declares them, so the auditor states what it
    found before it commits to a call. With `reason` leading it had to
    summarize a decision it had not made yet, and local models answered with
    the verdict word itself — 19 of 23 audits in one live database. The
    second-opinion verdict has always been ordered this way."""
    assert list(ReplyAudit.model_json_schema()["properties"]) == [
        "problems", "reason", "verdict"]


def test_an_unknown_verdict_is_rejected_by_the_schema():
    """The verdict is typed, so the old "is this string exactly OK, or is it
    a narration ending in OK" parsing has nowhere to live."""
    with pytest.raises(Exception):
        ReplyAudit.model_validate({"reason": "fine", "verdict": "OK"})


def test_the_system_prompt_states_the_job_and_names_no_dialect():
    """The auditor's job belongs in its system prompt, and the prompt names
    no dialect or example word — those get parroted into replies."""
    low = REPLY_AUDIT_TURN_INSTRUCTIONS.lower()
    assert "audit" in low or "review" in low
    for word in ("british", "american", "colour", "anticlockwise", "car park"):
        assert word not in low


def test_the_auditor_treats_a_source_preamble_as_a_defect():
    """The auditor used to bless an opening that credited the memory system —
    it read as diligence, so the habit passed the one check that could have
    stopped it. It is now a named defect, fenced off from tone nitpicking so
    the carve-out does not reopen the style complaints check 6 sits beside."""
    p = REPLY_AUDIT_TURN_INSTRUCTIONS
    assert "6. Against how it addresses the user." in p
    assert "the reply withholds the answer to narrate its own plumbing" in " ".join(
        p.split())
    assert "check 6 is not an\nopening to raise them" in p
    assert "Do not raise style preferences" in p


def test_the_auditor_checks_the_reply_answers_the_requests_subject():
    """A reply that described the ASSISTANT passed an audit of a request asking
    about the USER: the auditor read a fluent answer to an adjacent question as
    a complete one. Subject alignment is now its own named check, not something
    left implicit inside "does it answer all of it"."""
    p = " ".join(REPLY_AUDIT_TURN_INSTRUCTIONS.split())
    assert "2. Against WHO OR WHAT the request is about." in p
    assert "Name the subject the request asks about" in p
    assert "the user's question about themselves answered about the assistant" in p
    assert "Quote the sentence that shows the mismatched subject." in p


# --- the reply capability -----------------------------------------------------


def test_reply_carries_the_message_alone():
    """No prefixes: they encoded a writing order, and there is no longer an
    order to encode. `message` matches `question`, the other terminal arg."""
    cap = CAPABILITIES[AssistantActionName.REPLY]
    assert cap.required_args == ("message",)
    assert "2_audit" not in cap.description
    assert "1_message" not in cap.description
    assert '{"message": "..."}' in cap.description


def test_the_argument_order_machinery_is_gone():
    """Four layers of defence around a property two calls make impossible to
    violate. If any of these come back, the split was undone."""
    for attr in ("REPLY_ARG_ORDER", "AUDIT_ORDER_ERROR",
                 "_audit_order_error", "_audit_rejection"):
        assert not hasattr(AssistantAgent, attr), attr


# --- the call ------------------------------------------------------------------


@pytest.fixture
def audit_call(monkeypatch):
    """Drive the real `_reply_audit` with a scripted structured call.

    Yields a dict: set `["verdict"]` to the ReplyAudit the model returns, or
    `["raise"]` to make the call fail. `["prompts"]` records what was sent.
    """
    import agents.query_filter_router as router

    box: dict = {"verdict": None, "raise": None, "prompts": []}

    def fake_structured_llm_call(_name, _uuids, system_prompt, user_prompt,
                                 _model, usage_out=None):
        box["prompts"].append((system_prompt, user_prompt))
        if box["raise"] is not None:
            raise box["raise"]
        return box["verdict"], uuid4()

    monkeypatch.setattr(router, "structured_llm_call", fake_structured_llm_call)
    monkeypatch.setattr(router, "resolve_model_uuids",
                        lambda _c: ([uuid4()], "own"))
    return box


def test_a_send_verdict_approves_the_message(app_ctx, audit_call):
    audit_call["verdict"] = ReplyAudit(reason="sound", verdict="send")
    ok, payload = _agent()._reply_audit(
        _reply("12 feet is 3.6576 meters."), messages=[], scratchpad=[])
    assert ok is True
    assert payload["verdict"] == "send"
    assert payload["problems"] == ""


def test_a_revise_verdict_blocks_the_message(app_ctx, audit_call):
    audit_call["verdict"] = ReplyAudit(
        reason="one part unanswered",
        problems=("no metric value given — the reply stops after restating "
                  "the question"),
        verdict="revise",
    )
    ok, payload = _agent()._reply_audit(
        _reply("You asked about 12 feet."), messages=[], scratchpad=[])
    assert ok is False
    assert payload["problems"].startswith("no metric value given")


def test_the_audit_fails_open_when_the_call_raises(app_ctx, audit_call):
    """A checker outage must not swallow the operator's answer: a turn that
    produces nothing is worse than one that produces an unaudited reply."""
    audit_call["raise"] = RuntimeError("auditor unreachable")
    ok, payload = _agent()._reply_audit(
        _reply("12 feet is 3.6576 meters."), messages=[], scratchpad=[])
    assert ok is True
    assert "auditor unreachable" in payload["error"]


def test_the_audit_fails_open_when_no_model_group_is_bound(
        app_ctx, monkeypatch):
    import agents.query_filter_router as router

    monkeypatch.setattr(router, "resolve_model_uuids", lambda _c: (None, None))
    ok, payload = _agent()._reply_audit(
        _reply("12 feet is 3.6576 meters."), messages=[], scratchpad=[])
    assert ok is True
    assert payload["skipped"] == "no_model_group"


def test_the_auditor_sees_the_evidence_and_not_the_argument(
        app_ctx, audit_call):
    """The observation/reasoning split: a step's observation is the evidence
    the message must match, while the reasoning that produced the message is
    the rationalization the separate call exists to escape."""
    from agents.assistant import AssistantTurnStep

    audit_call["verdict"] = ReplyAudit(reason="sound", verdict="send")
    scratchpad = [AssistantTurnStep(
        step_index=0, action="python_run",
        args={"code": "print(12 * 0.3048)"},
        status="ok", observation="3.6576",
        reason="RATIONALIZATION-CANARY: convert using the metric factor",
    )]
    _agent()._reply_audit(
        _reply("12 feet is 3.6576 meters."),
        messages=[{"sender_type": "human", "text": "how much is 12 feet?"}],
        scratchpad=scratchpad)
    _system, user_prompt = audit_call["prompts"][0]
    assert "12 feet is 3.6576 meters." in user_prompt   # the message
    assert "how much is 12 feet?" in user_prompt        # the request
    assert "3.6576" in user_prompt                      # the observation
    assert "RATIONALIZATION-CANARY" not in user_prompt  # not the reasoning


# --- the loop -----------------------------------------------------------------


def _audited_run(room, monkeypatch, *audits):
    """Run one turn whose audit seam returns `audits` in order. Returns the
    messages posted to the room."""
    room_uuid, message_uuid = room
    agent = _agent()
    replies = [_reply(f"answer {i}") for i in range(len(audits))]
    agent._decide_next_step = scripted_decisions(*replies)
    queue = list(audits)

    def fake_audit(self, decision, *, messages, scratchpad):
        return queue.pop(0)

    monkeypatch.setattr(AssistantAgent, "_reply_audit", fake_audit)
    agent.handle(
        uuid4(),
        {"room_uuid": str(room_uuid), "message_uuid": str(message_uuid)})
    return agent, db.list_room_messages(room_uuid)


def test_a_send_verdict_posts_the_message(room, monkeypatch):
    _agent_, messages = _audited_run(
        room, monkeypatch, (True, {"verdict": "send", "problems": ""}))
    assert any(m["text"] == "answer 0" for m in messages)


def test_a_revise_verdict_bounces_the_reply_and_posts_nothing_yet(
        room, monkeypatch):
    """The bounced message never reaches the room; the loop gets another
    decide step carrying the auditor's problems as corrective text."""
    _agent_, messages = _audited_run(
        room, monkeypatch,
        (False, {"verdict": "revise", "reason": "incomplete",
                 "problems": ("the second question is unanswered — no "
                              "metric value appears")}),
        (True, {"verdict": "send", "problems": ""}))
    assert not any(m["text"] == "answer 0" for m in messages)
    assert any(m["text"] == "answer 1" for m in messages)


def test_the_bounced_step_records_the_auditor_problems(room, monkeypatch):
    agent, _messages = _audited_run(
        room, monkeypatch,
        (False, {"verdict": "revise", "reason": "incomplete",
                 "problems": ("the second question is unanswered — no "
                              "metric value appears")}),
        (True, {"verdict": "send", "problems": ""}))
    steps = db.list_assistant_steps(agent._run.uuid)
    rejected = [s for s in steps if s.phase == "failed"]
    assert rejected, "the bounced reply must leave a failed step in the trace"
    assert "the second question is unanswered" in (rejected[0].error or "")


def test_a_revise_with_no_problems_still_bounces(room, monkeypatch):
    """A verdict is a verdict. An auditor that says revise without saying why
    still blocks the message; the loop substitutes a placeholder complaint so
    the next step is not handed an empty instruction."""
    agent, messages = _audited_run(
        room, monkeypatch,
        (False, {"verdict": "revise", "reason": "unsure", "problems": ""}),
        (True, {"verdict": "send", "problems": ""}))
    assert not any(m["text"] == "answer 0" for m in messages)
    steps = db.list_assistant_steps(agent._run.uuid)
    rejected = [s for s in steps if s.phase == "failed"]
    assert rejected and (rejected[0].error or "").strip()


def test_past_the_cap_the_reply_ships_despite_the_audit(room, monkeypatch):
    """An auditor that never says send must not burn the step limit and fail
    the turn. Unchanged from the self-audit contract."""
    revise = (False, {"verdict": "revise", "reason": "no",
                      "problems": "still wrong — everywhere"})
    audits = [revise] * (AssistantAgent.MAX_AUDIT_REJECTIONS + 1)
    _agent_, messages = _audited_run(room, monkeypatch, *audits)
    posted = [m["text"] for m in messages
              if str(m["text"]).startswith("answer ")]
    assert posted, "past the cap the reply must ship anyway"


def test_the_audit_gets_its_own_trace_row(room, monkeypatch):
    """Its own step row, like the classifier's: without one the audit has no
    model, no duration and no cost of its own, and nobody can ask whether it
    earns its latency."""
    agent, _messages = _audited_run(
        room, monkeypatch,
        (True, {"verdict": "send", "reason": "sound", "problems": "",
                "model_uuid": str(uuid4()), "system_prompt": "sys",
                "user_prompt": "usr"}))
    steps = db.list_assistant_steps(agent._run.uuid)
    audits = [s for s in steps
              if s.action == AssistantAgent.REPLY_AUDIT_ACTION]
    assert len(audits) == 1
    assert audits[0].system_prompt == "sys"
    assert audits[0].user_prompt == "usr"
    assert "send" in (audits[0].observation_preview or "")


def test_the_audit_row_carries_its_token_cost(room, monkeypatch):
    """The row recorded the clock but not the tokens, so the audit — which
    reads the reply, the request, the observations and the settings — looked
    free next to every other model call in the same trace."""
    agent, _messages = _audited_run(
        room, monkeypatch,
        (True, {"verdict": "send", "reason": "sound", "problems": "",
                "model_uuid": str(uuid4()), "system_prompt": "sys",
                "user_prompt": "usr",
                "usage": {"input": 4321, "output": 87, "ms": 23401}}))
    audits = [s for s in db.list_assistant_steps(agent._run.uuid)
              if s.action == AssistantAgent.REPLY_AUDIT_ACTION]

    assert (audits[0].input_tokens, audits[0].output_tokens) == (4321, 87)
    assert audits[0].duration_ms == 23401


def test_a_bounced_audit_is_traced_too(room, monkeypatch):
    """The bounce is the interesting case: a persistently revising auditor is
    only diagnosable if every verdict left a row."""
    agent, _messages = _audited_run(
        room, monkeypatch,
        (False, {"verdict": "revise", "reason": "incomplete",
                 "problems": "unanswered — none"}),
        (True, {"verdict": "send", "reason": "sound", "problems": ""}))
    steps = db.list_assistant_steps(agent._run.uuid)
    audits = [s for s in steps
              if s.action == AssistantAgent.REPLY_AUDIT_ACTION]
    assert len(audits) == 2
    assert "revise" in (audits[0].observation_preview or "")


def test_a_clarifying_question_is_not_audited(room, monkeypatch):
    """Only replies are audited — a clarifying question has no formatting
    surface worth a bounced step."""
    room_uuid, message_uuid = room
    agent = _agent()
    agent._decide_next_step = scripted_decisions(AssistantStepDecision(
        reason="need more", action=AssistantActionName.ASK_CLARIFYING_QUESTION,
        args={"question": "feet of what?"}))
    calls: list = []
    monkeypatch.setattr(
        AssistantAgent, "_reply_audit",
        lambda self, decision, **kw: calls.append(decision) or (True, {}))
    agent.handle(
        uuid4(),
        {"room_uuid": str(room_uuid), "message_uuid": str(message_uuid)})
    assert calls == []
