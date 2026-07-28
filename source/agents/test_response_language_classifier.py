"""Tests for the assistant's response-language classifier and Markdown bridge."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

import db
from agents.assistant import (
    RESPONSE_LANGUAGE_CLASSIFIER_SYSTEM_PROMPT,
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
        audit="OK",
    )


def _reply() -> AssistantStepDecision:
    return AssistantStepDecision(
        reason="answer ready",
        action=AssistantActionName.REPLY,
        args={
            "1_message": "Good night — and equivalents around the world.",
            "2_audit": "OK",
        },
    )


def test_schema_enforces_likert_bounds_and_canonical_language_codes():
    result = ResponseLanguageClassification(
        reason="English request.",
        languages=[ResponseLanguageItem(code="EN-gb", score=5)],
        audit="OK",
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
            audit="OK",
        )
    with pytest.raises(ValidationError, match="invalid language code"):
        ResponseLanguageClassification(
            reason="invalid",
            languages=[ResponseLanguageItem(code="English", score=5)],
            audit="problem",
        )


def test_classifier_has_a_structured_output_model_binding():
    entry = agent_config["response_language_classifier"]
    assert entry["uuid"] == RESPONSE_LANGUAGE_CLASSIFIER_UUID
    assert entry["requires_structured_output"] is True
    assert entry["next"] is None


def test_prompt_scores_all_profile_rows_and_omits_assistant_history():
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
    assert "Je vais répondre en français." not in prompt
    assert '"code": "en-GB"' in prompt
    assert '"code": "da"' in prompt
    assert "copy every declared profile-language code exactly" in prompt
    assert "compatible preferred profile variant" in prompt
    assert 'assistant_messages="omitted"' in prompt


def test_system_prompt_uses_planexe_likert_and_distinguishes_content_language():
    prompt = RESPONSE_LANGUAGE_CLASSIFIER_SYSTEM_PROMPT
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


def test_preferred_profile_variant_refines_broad_model_output_and_flags_audit():
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
        audit="OK",
    )
    result = agent._reconcile_response_language_profile_variants(
        broad, profile)
    assert [(item.code, item.score) for item in result.languages] == [
        ("en-GB", 5),
        ("da", 1),
    ]
    assert result.audit != "OK"
    assert "normalized it to declared profile variant 'en-GB'" in result.audit


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
        audit="uncertain variant",
    )
    result = agent._reconcile_response_language_profile_variants(
        broad, profile)
    assert result.languages[0].code == "en"
    assert "omitted declared profile code(s): en-GB, en-US" in result.audit


def test_markdown_sorts_by_score_stably_and_omits_scores():
    classification = ResponseLanguageClassification(
        reason="The request asks for multilingual narration.",
        languages=[
            ResponseLanguageItem(code="fr", score=4),
            ResponseLanguageItem(code="en-GB", score=5),
            ResponseLanguageItem(code="es", score=4),
            ResponseLanguageItem(code="da", score=1),
        ],
        audit="OK",
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
        "- `da`\n\n"
        "## Audit\n"
        "OK"
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
    assert [row.action for row in rows] == [
        "response_language_classifier",
        "reply",
    ]
    assert [row.step_index for row in rows] == [0, 0]
    assert rows[0].phase == "observed"
    assert '"score": 5' in (rows[0].observation_preview or "")
    assert '"audit": "OK"' in (rows[0].observation_preview or "")


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
                        validator=None):
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
        system_prompt = call["system_prompt"]
        assert decide_prompt.count("<reply_language_markdown") == 1
        assert _classification().reason in decide_prompt
        assert decide_prompt.index("- `en-GB`") < decide_prompt.index("- `da`")
        assert "score" not in decide_prompt.casefold()
        assert (decide_prompt.index("</current_request>")
                < decide_prompt.index("<reply_language_markdown")
                < decide_prompt.index("<conversation_history"))
        assert (
            '<source rank="3">reply_language_markdown' in system_prompt)
        assert "scores are intentionally omitted" in system_prompt


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
    assert [row.action for row in rows] == [
        "response_language_classifier",
        "reply",
    ]
    assert rows[0].phase == "failed"
    assert "scorer unavailable" in (rows[0].error or "")
    assert rows[1].phase == "final"
