"""Tests for the assistant's side of the response-language gate: the switch,
the resolver that decides a turn's reply language from message text alone,
and the resolved step row it writes.
"""

from uuid import uuid4

import pytest

import db
from agents.assistant import (
    AssistantAgent,
    ResponseLanguageClassification,
    ResponseLanguageItem,
)
from agents.config import ASSISTANT_UUID


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    yield app
    db.session.rollback()
    ctx.pop()


@pytest.fixture(autouse=True)
def _restore_gate_setting(app_ctx):
    """set_setting commits, so a test in this file that flips the gate
    changes it for every later test in the session and for the next run of
    the suite. Capture the row before each test and put it back after,
    regardless of what the test sets it to or how it fails."""
    row = db.session.query(db.AppSetting).filter_by(
        key="assistant.response_language_gate").one_or_none()
    saved = row.value if row is not None else None
    try:
        yield
    finally:
        db.session.rollback()
        row = db.session.query(db.AppSetting).filter_by(
            key="assistant.response_language_gate").one_or_none()
        if row is not None:
            row.value = saved
        db.session.commit()


def _agent() -> AssistantAgent:
    return AssistantAgent(
        agent_uuid=ASSISTANT_UUID, name="assistant", send=lambda _: None)


def test_the_gate_is_registered_and_defaults_off(app_ctx):
    """Default off: the gate ships dormant and the operator turns it on when
    they want to compare runs."""
    assert "assistant.response_language_gate" in db.SETTINGS
    setting = db.SETTINGS["assistant.response_language_gate"]
    assert setting.type == "bool"
    assert setting.default is False


def test_the_switch_reads_off_when_unset(app_ctx):
    db.set_setting("assistant.response_language_gate", False)
    assert _agent()._response_language_gate_enabled() is False


def test_the_switch_reads_on_when_set(app_ctx):
    db.set_setting("assistant.response_language_gate", True)
    assert _agent()._response_language_gate_enabled() is True


def test_an_unreadable_switch_reads_off(app_ctx, monkeypatch):
    """Off means the classifier runs, which is today's behaviour. A switch that
    cannot be read must not silently start skipping model calls."""
    def boom(_key):
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(db, "get_setting", boom)
    assert _agent()._response_language_gate_enabled() is False


@pytest.fixture
def room(app_ctx):
    human = db.get_human_user()
    assert human is not None
    chatroom = db.create_chatroom(
        f"language-gate-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])
    try:
        yield chatroom
    finally:
        db.session.rollback()
        db.session.query(db.AssistantRun).filter(
            db.AssistantRun.room_uuid == chatroom.uuid).delete()
        db.session.query(db.Chatroom).filter(
            db.Chatroom.uuid == chatroom.uuid).delete()
        db.session.commit()


# A first message with no history and no pinned language, on which the
# unrestricted detector's own top guess has nothing to be compared against --
# `resolve()` cannot settle this without a model (`first_message_unmatched`).
UNRESOLVABLE_FIRST_MESSAGE = [
    {"sender_type": "human",
     "text": "This is a first message with nothing else to compare it to."},
]


def test_an_asking_turn_still_calls_the_classifier(room, monkeypatch):
    """When the resolver cannot settle the language on its own, the turn
    proceeds exactly as it always has: the model is asked and the row records
    what it returned."""
    db.set_setting("assistant.response_language_gate", True)
    agent = _agent()
    agent._run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=ASSISTANT_UUID)
    monkeypatch.setattr(
        agent, "_request_response_language_classification",
        lambda **kw: ResponseLanguageClassification(
            reason="English.",
            languages=[ResponseLanguageItem(code="en-GB", score=5)]))

    agent._run_response_language_classifier(
        step_index=0, messages=UNRESOLVABLE_FIRST_MESSAGE, profile=None)
    db.session.commit()

    row = (
        db.session.query(db.AssistantStep)
        .filter(db.AssistantStep.run_uuid == agent._run.uuid)
        .order_by(db.AssistantStep.id.desc())
        .first()
    )
    assert row.phase == "observed"
    assert row.args["gate"]["ask"] is True
    assert row.args["gate"]["trigger"] == "first_message_unmatched"
    assert "gate_replaced_call" not in row.args


def test_an_asked_turn_with_no_model_group_bound_is_not_read_as_a_resolved_row(
        room, monkeypatch):
    """The resolver asks (nothing to settle it on its own) and the classifier
    then finds no model group bound. That row shares `phase == "skipped"`
    with a resolved row and, since the resolution is now recorded on the ask
    path too, also carries a `gate` key in `args`. Neither must make it read
    as the resolver having replaced the call: nothing ran, so it must render
    as "never made", with no duration."""
    db.set_setting("assistant.response_language_gate", True)
    agent = _agent()
    agent._run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=ASSISTANT_UUID)
    monkeypatch.setattr(
        agent, "_request_response_language_classification", lambda **kw: None)

    agent._run_response_language_classifier(
        step_index=0, messages=UNRESOLVABLE_FIRST_MESSAGE, profile=None)
    db.session.commit()

    row = (
        db.session.query(db.AssistantStep)
        .filter(db.AssistantStep.run_uuid == agent._run.uuid)
        .order_by(db.AssistantStep.id.desc())
        .first()
    )
    assert row.phase == "skipped"
    assert row.reason == "no model group is bound"
    # The resolver DID ask — a decision is on the row — but it never replaced
    # this call, so the explicit marker must be absent.
    assert row.args["gate"]["ask"] is True
    assert "gate_replaced_call" not in row.args
    assert row.duration_ms is None

    from webapp.assistant_log_view import log_view

    view = log_view(agent._run, [row])
    event = next(e for e in view["events"] if e["kind"] == "skipped")
    assert event["duration_ms"] is None
    assert "never made" in event["detail_html"]
    assert "no model group is bound" in event["detail_html"]


def test_a_constructed_classification_ranks_the_resolved_language_first():
    """The resolved language leads; the profile's other declared languages
    follow in their own preference order."""
    agent = _agent()
    profile = {"data": {"languages": {"rows": [
        {"tag": "en-US", "level": "native", "stance": "prefer", "note": ""},
        {"tag": "da", "level": "native", "stance": "neutral", "note": ""},
    ]}}}
    result = agent._constructed_classification("da", profile)
    assert [item.code for item in result.languages] == ["da", "en-US"]
    assert "da" in agent._format_reply_language_markdown(result)


def test_a_constructed_classification_uses_the_declared_variant():
    """A base subtag from the detector becomes the profile's exact declared tag,
    so `en` resolves to `en-US` rather than losing the variant."""
    agent = _agent()
    profile = {"data": {"languages": {"rows": [
        {"tag": "en-US", "level": "native", "stance": "prefer", "note": ""},
    ]}}}
    result = agent._constructed_classification("en", profile)
    assert result.languages[0].code == "en-US"


def test_a_constructed_classification_says_it_was_not_a_model():
    """The reason travels into the prompt and onto the trace; it must not read
    as a model's verdict."""
    agent = _agent()
    result = agent._constructed_classification("en", None)
    assert result.languages[0].code == "en"
    assert "detect" in result.reason.lower()


def test_a_constructed_classification_with_six_languages_keeps_declared_order():
    """Six is the first declared-list length where the 1..5 score range runs
    out: with six candidates the last two both clamp to score 1, so their
    relative order can no longer come from the scores and instead depends on
    `_format_reply_language_markdown`'s tie-break on original index. That
    index is only correct if `_constructed_classification` still places the
    resolved language first and the rest in their declared order even once
    the scores stop being distinct."""
    agent = _agent()
    profile = {"data": {"languages": {"rows": [
        {"tag": "en-US", "level": "native", "stance": "prefer", "note": ""},
        {"tag": "da", "level": "native", "stance": "neutral", "note": ""},
        {"tag": "de", "level": "fluent", "stance": "neutral", "note": ""},
        {"tag": "fr", "level": "fluent", "stance": "neutral", "note": ""},
        {"tag": "es", "level": "intermediate", "stance": "neutral", "note": ""},
        {"tag": "it", "level": "beginner", "stance": "neutral", "note": ""},
    ]}}}
    result = agent._constructed_classification("de", profile)
    assert [item.code for item in result.languages] == [
        "de", "en-US", "da", "fr", "es", "it"]
    assert [item.score for item in result.languages][-2:] == [1, 1]

    markdown = agent._format_reply_language_markdown(result)
    rendered_section = markdown.split("## Languages", 1)[1]
    rendered_codes = [
        line[len("- `"):-1]
        for line in rendered_section.splitlines()
        if line.startswith("- `")
    ]
    assert rendered_codes == ["de", "en-US", "da", "fr", "es", "it"]


# --- resolution wired into the turn ---------------------------------------

EN_MESSAGES = [
    {"sender_type": "human", "text": "can you say something funny about AI"},
    {"sender_type": "agent", "text": "Here is a joke about testing."},
    {"sender_type": "human", "text": "explain the joke, the skeleton one"},
]

DA_PROSE_TEXT = ("Jeg vil gerne have at du svarer pa dansk naar jeg skriver "
                 "pa dansk til dig.")
_DA_EN_PROFILE = {"data": {"languages": {"rows": [
    {"tag": "en-US", "level": "native", "stance": "prefer", "note": ""},
    {"tag": "da", "level": "native", "stance": "neutral", "note": ""},
]}}}


def test_the_resolver_is_not_consulted_when_the_switch_is_off(room, monkeypatch):
    import agents.response_language_gate as gate

    db.set_setting("assistant.response_language_gate", False)
    called = []
    monkeypatch.setattr(gate, "resolve", lambda **kw: called.append(kw))
    assert _agent()._resolve_response_language(EN_MESSAGES, None) is None
    assert called == []


def test_only_the_operator_s_messages_are_read(room):
    """A reply is written in whatever language a previous decision chose, so
    letting replies vote would let one wrong decision justify itself.

    `profile=None` is deliberate: with a profile pinning both languages
    already present, the slot set is full either way and including or
    excluding the agent's reply produces the same outcome (measured: both
    read `ask=False trigger='resolved' language='da' slots=('en','da')`),
    so that combination cannot prove the filter does anything. With no
    pinned languages the slots are built from history alone, and the
    agent's English reply becomes observable the moment it is allowed to
    vote."""
    db.set_setting("assistant.response_language_gate", True)
    messages = [
        {"sender_type": "human", "text": DA_PROSE_TEXT},
        {"sender_type": "agent",
         "text": "This English reply must not count as evidence about what "
                 "language the operator is writing in at all."},
        {"sender_type": "human", "text": "det virker ikke rigtigt"},
    ]
    r = _agent()._resolve_response_language(messages, None)
    assert r is not None
    assert r.slots == ("da",)


def test_the_request_is_not_part_of_its_own_history(room):
    """The last message is the request being judged, so it must not also
    count as history the request is compared against.

    `profile=None` is deliberate: with `_DA_EN_PROFILE` pinning `en` and
    `da`, the English request lands inside the slot set whether or not it
    was folded into its own history (measured: both read `ask=False
    trigger='resolved' language='en'`), so that combination cannot prove
    the exclusion does anything. With no pinned languages, excluding the
    request leaves a Danish-only slot set the English request falls
    outside of (`ask=True trigger='outside_slots'`); including it lets the
    request vote itself into the slots and then match them
    (`ask=False trigger='resolved'`)."""
    db.set_setting("assistant.response_language_gate", True)
    messages = [
        {"sender_type": "human", "text": "det virker ikke rigtigt her"},
        {"sender_type": "human", "text": "can you say something funny about AI"},
    ]
    r = _agent()._resolve_response_language(messages, None)
    assert r is not None
    assert r.ask is True
    assert r.trigger == "outside_slots"


def test_an_ordinary_turn_records_a_resolved_row(room, monkeypatch):
    """The row is the point: the operator reads runs, and a turn that resolved
    without a model must say so, in place, with what it read."""
    db.set_setting("assistant.response_language_gate", True)
    agent = _agent()
    agent._run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=ASSISTANT_UUID)

    def fail(*a, **kw):
        raise AssertionError("the classifier must not run on a resolved turn")

    monkeypatch.setattr(
        agent, "_request_response_language_classification", fail)

    agent._run_response_language_classifier(
        step_index=0, messages=EN_MESSAGES, profile=_DA_EN_PROFILE)
    db.session.commit()

    row = (db.session.query(db.AssistantStep)
           .filter(db.AssistantStep.run_uuid == agent._run.uuid)
           .order_by(db.AssistantStep.id.desc()).first())
    assert row.phase == "skipped"
    assert row.action == "response_language_classifier"
    assert row.args["gate"]["trigger"] == "resolved"
    assert row.args["gate"]["language"] == "en-US"
    # The marker the renderers key on: without it a resolved row renders as
    # a call that was never made.
    assert row.args["gate_replaced_call"] is True
    assert row.system_prompt is None and row.user_prompt is None
    assert row.duration_ms is not None and row.duration_ms < 1000
    # No model was recorded on a row that had no call.
    assert row.model_uuid is None
    # The language it proceeded in, not merely that it declined to ask.
    assert "en-US" in (row.observation_preview or "")
    assert agent._response_language_classification is not None
    assert agent._response_language_classification.languages[0].code == "en-US"
    assert "en-US" in agent._reply_language_markdown

    from webapp.assistant_log_view import log_view

    view = log_view(agent._run, [row])
    event = next(e for e in view["events"] if e["kind"] == "skipped")
    # This is the gate's row, not the "never made" shape it shares a phase
    # with -- a resolved row that lost the marker would render as the
    # opposite of what actually happened.
    assert "never made" not in event["detail_html"]
