"""Tests for the assistant's response-language classifier and Markdown bridge."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

import db
from agents.assistant import (
    RESPONSE_LANGUAGE_TURN_INSTRUCTIONS,
    AssistantActionName,
    AssistantAgent,
    AssistantStepDecision,
    ResponseLanguageClassification,
    ResponseLanguageItem,
)
from agents.config import (
    ASSISTANT_UUID,
    RESPONSE_LANGUAGE_CLASSIFIER_UUID,
    agent_config,
)


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    yield app
    db.db.session.rollback()
    ctx.pop()


@pytest.fixture
def room(app_ctx):
    human = db.get_human_user()
    assert human is not None
    chatroom = db.create_chatroom(
        f"language-classifier-{uuid4().hex[:8]}",
        human.uuid,
        [ASSISTANT_UUID],
    )
    db.post_chat_message(
        chatroom.uuid,
        human.uuid,
        "Show the ten most common goodnight sayings worldwide and explain "
        "their meanings in English.",
    )
    try:
        yield chatroom
    finally:
        db.db.session.rollback()
        db.db.session.query(db.AssistantRun).filter(
            db.AssistantRun.room_uuid == chatroom.uuid).delete()
        db.db.session.query(db.ChatMessage).filter(
            db.ChatMessage.room_uuid == chatroom.uuid).delete()
        db.db.session.query(db.Chatroom).filter(
            db.Chatroom.uuid == chatroom.uuid).delete()
        db.db.session.commit()


def _agent() -> AssistantAgent:
    return AssistantAgent(
        agent_uuid=ASSISTANT_UUID, name="assistant", send=lambda _: None)


def _classification() -> ResponseLanguageClassification:
    return ResponseLanguageClassification(
        reason=(
            "The request is English and asks for English explanations; "
            "foreign-language sayings are quoted content."
        ),
        languages=[
            ResponseLanguageItem(code="en-GB", score=5),
            ResponseLanguageItem(code="da", score=1),
        ],
    )


def _reply() -> AssistantStepDecision:
    return AssistantStepDecision(
        reason="answer ready",
        action=AssistantActionName.REPLY,
        args={
            "message": "Good night — and equivalents around the world.",
        },
    )


def test_schema_enforces_likert_bounds_and_canonical_language_codes():
    result = ResponseLanguageClassification(
        reason="English request.",
        languages=[ResponseLanguageItem(code="EN-gb", score=5)],
    )
    assert result.languages[0].code == "en-GB"

    for score in (0, 6):
        with pytest.raises(ValidationError):
            ResponseLanguageItem(code="en", score=score)
    with pytest.raises(ValidationError, match="duplicate language code"):
        ResponseLanguageClassification(
            reason="duplicate",
            languages=[
                ResponseLanguageItem(code="en-gb", score=5),
                ResponseLanguageItem(code="EN-GB", score=4),
            ],
        )
    with pytest.raises(ValidationError, match="invalid language code"):
        ResponseLanguageClassification(
            reason="invalid",
            languages=[ResponseLanguageItem(code="English", score=5)],
        )


def test_classifier_has_a_structured_output_model_binding():
    entry = agent_config["response_language_classifier"]
    assert entry["uuid"] == RESPONSE_LANGUAGE_CLASSIFIER_UUID
    assert entry["requires_structured_output"] is True
    assert entry["next"] is None


def test_prompt_scores_all_profile_rows_and_carries_both_roles():
    agent = _agent()
    messages = [
        {"sender_type": "human", "text": "Vi talte dansk."},
        {"sender_type": "agent", "text": "Je vais répondre en français."},
        {"sender_type": "human", "text": "Please explain it in English."},
    ]
    profile = {
        "data": {
            "languages": {
                "rows": [
                    {
                        "tag": "en-gb",
                        "level": "native",
                        "stance": "prefer",
                        "note": "British spelling",
                    },
                    {
                        "tag": "da",
                        "level": "native",
                        "stance": "neutral",
                        "note": "",
                    },
                ]
            }
        }
    }
    prompt = agent._build_response_language_classifier_prompt(
        messages, profile)
    assert "Please explain it in English." in prompt
    assert "Vi talte dansk." in prompt
    # The assistant's turns are included: an earlier reply is the only record
    # of what language the conversation has actually been running in, and
    # withholding it hid that from the one call whose job is to decide the
    # language. The anti-perpetuation guard moved into the prompt — a
    # wrong-language reply loses to the current request rather than being kept
    # out of sight.
    assert "Je vais répondre en français." in prompt
    assert "assistant_messages" not in prompt
    assert "must not perpetuate itself" in RESPONSE_LANGUAGE_TURN_INSTRUCTIONS
    assert '"code": "en-GB"' in prompt
    assert '"code": "da"' in prompt
    assert "copy every declared profile-language code exactly" in prompt
    assert "compatible preferred profile variant" in prompt


def test_system_prompt_uses_planexe_likert_and_distinguishes_content_language():
    prompt = RESPONSE_LANGUAGE_TURN_INSTRUCTIONS
    assert "1 = strong negative" in prompt
    assert "2 = weak negative" in prompt
    assert "3 = neutral" in prompt
    assert "4 = weak positive" in prompt
    assert "5 = strong positive" in prompt
    assert "quoted examples are content rather than reply" in prompt
    assert "A broad explicit target selects the LANGUAGE FAMILY" in prompt
    assert "copy its `code` byte-for-byte" in prompt
    assert "Never shorten, broaden, translate" in prompt
    assert "Use a broad language code when the evidence supports only" not in prompt


def test_preferred_profile_variant_refines_broad_model_output_and_flags_repair():
    """Regression for the live trace where reasoning selected en-GB but the
    structured answer collapsed it to en."""
    agent = _agent()
    profile = {
        "data": {
            "languages": {
                "rows": [
                    {
                        "tag": "en-GB",
                        "level": "intermediate",
                        "stance": "prefer",
                        "note": "primary response language",
                    },
                    {
                        "tag": "da",
                        "level": "native",
                        "stance": "neutral",
                        "note": "",
                    },
                ]
            }
        }
    }
    broad = ResponseLanguageClassification(
        reason="The explicit translation target is English.",
        languages=[
            ResponseLanguageItem(code="en", score=5),
            ResponseLanguageItem(code="da", score=1),
        ],
    )
    result = agent._reconcile_response_language_profile_variants(
        broad, profile)
    assert [(item.code, item.score) for item in result.languages] == [
        ("en-GB", 5),
        ("da", 1),
    ]
    # The repair is appended to the model's own reason, never hidden: the
    # experiment measures upstream scorer quality, so a code-side fix has to
    # stay visible in the trace and in the downstream Markdown.
    assert result.reason.startswith("The explicit translation target is English.")
    assert "normalized it to declared profile variant 'en-GB'" in result.reason


def test_broad_code_stays_broad_without_unambiguous_profile_variant():
    agent = _agent()
    profile = {
        "data": {
            "languages": {
                "rows": [
                    {"tag": "en-GB", "level": "fluent",
                     "stance": "neutral", "note": ""},
                    {"tag": "en-US", "level": "fluent",
                     "stance": "neutral", "note": ""},
                ]
            }
        }
    }
    broad = ResponseLanguageClassification(
        reason="English target.",
        languages=[ResponseLanguageItem(code="en", score=5)],
    )
    result = agent._reconcile_response_language_profile_variants(
        broad, profile)
    assert result.languages[0].code == "en"
    assert "omitted declared profile code(s): en-GB, en-US" in result.reason


def test_markdown_sorts_by_score_stably_and_omits_scores():
    classification = ResponseLanguageClassification(
        reason="The request asks for multilingual narration.",
        languages=[
            ResponseLanguageItem(code="fr", score=4),
            ResponseLanguageItem(code="en-GB", score=5),
            ResponseLanguageItem(code="es", score=4),
            ResponseLanguageItem(code="da", score=1),
        ],
    )
    markdown = AssistantAgent._format_reply_language_markdown(
        classification)
    assert markdown == (
        "## Reason\n"
        "The request asks for multilingual narration.\n\n"
        "## Languages - highest confidence first\n"
        "- `en-GB`\n"
        "- `fr`\n"
        "- `es`\n"
        "- `da`"
    )
    assert "score" not in markdown.casefold()
    # Formatting is a view: it must not reorder the stored structured result.
    assert [item.code for item in classification.languages] == [
        "fr", "en-GB", "es", "da"]


def test_classifier_is_first_observed_step_and_does_not_consume_budget(room):
    agent = _agent()
    order: list[str] = []

    def classify(*, system_prompt, user_prompt):
        order.append("classifier")
        return _classification()

    def decide(**kwargs):
        order.append("decide")
        return _reply()

    agent._request_response_language_classification = classify
    agent._decide_next_step = decide
    result = agent.handle(uuid4(), {"room_uuid": str(room.uuid)})
    assert result["status"] == "finished"
    assert order == ["classifier", "decide"]

    rows = db.list_assistant_steps(result["assistant_run_uuid"])
    # Every call the run made is a row, including the one it could not make:
    # the acceptance criteria are skipped here because a bare test agent binds
    # no model group, and a skip is an outcome the trace has to carry.
    assert [(row.action, row.phase) for row in rows] == [
        ("response_language_classifier", "observed"),
        ("acceptance_criteria", "skipped"),
        ("reply_audit", "observed"),
        ("reply", "final"),
    ]
    assert [row.step_index for row in rows] == [0, 0, 0, 0]
    # The classifier and the audit are calls the loop makes on its own; only
    # the reply is a model decision. Readers use the flag to tell them apart —
    # a code-driven row's action/reason are labels, not something the model
    # chose, and its response is the classification, not a decision.
    assert [row.code_driven for row in rows] == [True, True, True, False]
    assert rows[0].phase == "observed"
    assert '"score": 5' in (rows[0].observation_preview or "")
    assert '"audit"' not in (rows[0].observation_preview or "")


def test_ranked_markdown_is_injected_into_every_later_decide_without_scores(room):
    agent = _agent()
    agent._request_response_language_classification = (
        lambda **_: _classification())
    decide_prompts: list[dict[str, str]] = []
    decisions = [
        AssistantStepDecision(
            reason="probe",
            action=AssistantActionName.MEMORY_QUERY,
            args={"bogus": "force one more decide step"},
        ),
        _reply(),
    ]

    def fake_completion(*, system_prompt, user_prompt, response_model,
                        validator=None, **_kw):
        decide_prompts.append({
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
        })
        return decisions.pop(0)

    agent._structured_completion = fake_completion
    result = agent.handle(uuid4(), {"room_uuid": str(room.uuid)})
    assert result["status"] == "finished"
    assert len(decide_prompts) == 2
    for call in decide_prompts:
        decide_prompt = call["user_prompt"]
        assert decide_prompt.count("<reply_language_markdown") == 1
        assert _classification().reason in decide_prompt
        assert decide_prompt.index("- `en-GB`") < decide_prompt.index("- `da`")
        # Scoped to the injected block itself: turn_instructions (now part of
        # the user prompt) explains the score-free convention in prose, which
        # legitimately uses the word "score" without the block doing so.
        markdown_block = decide_prompt.split(
            "<reply_language_markdown>", 1)[1].split(
            "</reply_language_markdown>", 1)[0]
        assert "score" not in markdown_block.casefold()
        assert (decide_prompt.index("<conversation_history")
                < decide_prompt.index("<reply_language_markdown")
                < decide_prompt.index("</current_user_request>"))
        assert (
            '<source rank="3">reply_language_markdown' in decide_prompt)
        assert "scores are intentionally omitted" in decide_prompt


def test_classifier_output_skips_criteria_but_reaches_second_opinion():
    agent = _agent()
    agent._reply_language_markdown = (
        agent._format_reply_language_markdown(_classification()))
    messages = [{"sender_type": "human", "text": "Translate this to English."}]
    criteria_prompt = agent._build_acceptance_criteria_prompt(messages)
    assert "<reply_language_markdown" not in criteria_prompt
    assert "en-GB" not in criteria_prompt

    decision = AssistantStepDecision(
        reason="calculate",
        action=AssistantActionName.PYTHON_RUN,
        args={"code": "print(1)"},
    )
    reviewer_prompt = agent._build_second_opinion_prompt(
        decision, reasoning=None, messages=messages)
    assert "<reply_language_markdown" in reviewer_prompt
    assert "- `en-GB`" in reviewer_prompt
    assert "score" not in reviewer_prompt.casefold()


def test_classifier_failure_is_traced_and_assistant_continues(room):
    agent = _agent()

    def fail(**kwargs):
        raise RuntimeError("scorer unavailable")

    agent._request_response_language_classification = fail
    agent._decide_next_step = lambda **_: _reply()
    result = agent.handle(uuid4(), {"room_uuid": str(room.uuid)})
    assert result["status"] == "finished"
    rows = db.list_assistant_steps(result["assistant_run_uuid"])
    # A classifier that errored and criteria that never ran are different
    # outcomes, and the trace says which is which — a reply in the wrong
    # language points at one or the other, not at both.
    assert [(row.action, row.phase) for row in rows] == [
        ("response_language_classifier", "failed"),
        ("acceptance_criteria", "skipped"),
        ("reply_audit", "observed"),
        ("reply", "final"),
    ]
    assert "scorer unavailable" in (rows[0].error or "")
    assert rows[1].error is None            # skipped is not a failure


def test_classifier_call_records_its_token_cost_on_the_step_row(room, monkeypatch):
    """The classifier is a model call like any other, so its row carries the
    same in/out/throughput figures — the inspector's io-meta line reads them
    straight off the row, and a step missing them silently reads as free."""
    import agents.query_filter_router as router

    group_uuid, model_uuid = uuid4(), uuid4()

    def fake_resolve(_bindings):
        return group_uuid, "response_language_classifier"

    real_call = router.structured_llm_call

    def fake_call(name, models, system, user, schema, usage_out=None):
        # The reply audit goes through this same helper; leave it alone.
        if schema is not ResponseLanguageClassification:
            return real_call(name, models, system, user, schema,
                             usage_out=usage_out)
        # What structured_llm_call writes for the member that answered.
        if usage_out is not None:
            usage_out.update({"input": 812, "output": 96, "ms": 4200})
        return _classification(), model_uuid

    monkeypatch.setattr(router, "resolve_model_group", fake_resolve)
    monkeypatch.setattr(router, "structured_llm_call", fake_call)
    monkeypatch.setattr(db, "get_model_group_member_uuids", lambda _g: [model_uuid])
    agent = _agent()
    agent._decide_next_step = lambda **_: _reply()
    result = agent.handle(uuid4(), {"room_uuid": str(room.uuid)})
    assert result["status"] == "finished"
    row = next(r for r in db.list_assistant_steps(result["assistant_run_uuid"])
               if r.action == "response_language_classifier")
    assert (row.input_tokens, row.output_tokens) == (812, 96)
    # The answering member's own elapsed time, not the wall-clock across
    # fallbacks — it is what the throughput figure divides by.
    assert row.duration_ms == 4200
    assert row.model_uuid == model_uuid
