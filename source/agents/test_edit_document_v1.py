"""Tests for agent_edit_document_v1.

Schema and validator tests are pure functions — no database or LM Studio
needed. The integration test (added in a later task) follows the
test_agent_followup.py convention: skip if no model group is bound.

    python -m pytest test_agent_edit_document_v1.py -v
"""

import pytest
from pydantic import ValidationError

from agents.edit_document_v1 import (
    EDIT_DOCUMENT_SYSTEM_PROMPT,
    EditDocumentAgentV1,
    EditPlan,
    Patch,
    render_document_with_line_numbers,
    validate_patches,
)


def test_patch_minimal_replace():
    p = Patch(op="replace_lines", start_line=3, end_line=3, replacement="x", intent="i")
    assert p.op == "replace_lines"
    assert p.start_line == 3
    assert p.end_line == 3
    assert p.replacement == "x"
    assert p.intent == "i"


def test_patch_allows_end_line_zero_for_insert_before_line_one():
    p = Patch(op="replace_lines", start_line=1, end_line=0, replacement="x", intent="i")
    assert p.end_line == 0


def test_patch_rejects_start_line_zero():
    with pytest.raises(ValidationError):
        Patch(op="replace_lines", start_line=0, end_line=1, replacement="", intent="i")


def test_patch_rejects_negative_end_line():
    with pytest.raises(ValidationError):
        Patch(op="replace_lines", start_line=1, end_line=-1, replacement="", intent="i")


def test_patch_rejects_unknown_op():
    with pytest.raises(ValidationError):
        Patch(op="insert", start_line=1, end_line=1, replacement="x", intent="i")  # pyright: ignore[reportArgumentType]


def test_patch_requires_intent():
    with pytest.raises(ValidationError):
        Patch(op="replace_lines", start_line=1, end_line=1, replacement="x")  # pyright: ignore[reportCallIssue]


def test_edit_plan_accepts_empty_patches():
    plan = EditPlan(patches=[])
    assert plan.patches == []


def test_edit_plan_accepts_multiple_patches():
    plan = EditPlan(
        patches=[
            Patch(op="replace_lines", start_line=1, end_line=1, replacement="a", intent="i1"),
            Patch(op="replace_lines", start_line=3, end_line=4, replacement="b", intent="i2"),
        ]
    )
    assert len(plan.patches) == 2


def _p(start, end, replacement="x", intent="i"):
    return Patch(
        op="replace_lines",
        start_line=start,
        end_line=end,
        replacement=replacement,
        intent=intent,
    )


def test_validate_accepts_simple_replace():
    validate_patches([_p(2, 3)], document_line_count=5)


def test_validate_accepts_pure_insertion_mid_document():
    # Pure insertion before line 3 in a 5-line document.
    validate_patches([_p(3, 2)], document_line_count=5)


def test_validate_accepts_insertion_before_line_one():
    validate_patches([_p(1, 0)], document_line_count=5)


def test_validate_accepts_append_after_last_line():
    # Append after the last line of a 5-line document.
    validate_patches([_p(6, 5)], document_line_count=5)


def test_validate_accepts_deletion():
    validate_patches([_p(2, 3, replacement="")], document_line_count=5)


def test_validate_accepts_empty_patch_list():
    validate_patches([], document_line_count=5)


def test_validate_rejects_start_line_past_end():
    # 5-line document; start_line=7 leaves a gap (6 is the append position).
    with pytest.raises(ValueError, match="start_line"):
        validate_patches([_p(7, 7)], document_line_count=5)


def test_validate_rejects_end_line_past_document():
    with pytest.raises(ValueError, match="end_line"):
        validate_patches([_p(3, 6)], document_line_count=5)


def test_validate_rejects_end_before_start_minus_one():
    # end_line = start_line - 2 is invalid (only start_line - 1 is allowed
    # as the pure-insert encoding).
    with pytest.raises(ValueError, match="end_line"):
        validate_patches([_p(3, 1)], document_line_count=5)


def test_validate_rejects_overlapping_patches():
    with pytest.raises(ValueError, match="overlap"):
        validate_patches(
            [_p(2, 4), _p(3, 5)],
            document_line_count=10,
        )


def test_validate_rejects_two_inserts_at_same_line():
    # Two pure-insert patches both targeting line 3 would be ambiguous in
    # apply order; the spec says patches must not overlap, which catches this.
    with pytest.raises(ValueError, match="overlap"):
        validate_patches(
            [_p(3, 2), _p(3, 2)],
            document_line_count=10,
        )


def test_validate_rejects_noop_empty_insert():
    # end_line < start_line is the pure-insert encoding; combined with an
    # empty replacement it's a no-op. Small models keep emitting this when
    # asked to delete a line — reject so model-group fallback retries.
    with pytest.raises(ValueError, match="no-op"):
        validate_patches(
            [_p(3, 2, replacement="")],
            document_line_count=5,
        )


def test_validate_rejects_noop_insert_before_line_one():
    # Same rule at the start-of-document edge.
    with pytest.raises(ValueError, match="no-op"):
        validate_patches(
            [_p(1, 0, replacement="")],
            document_line_count=5,
        )


def test_validate_accepts_single_line_deletion():
    # The supported deletion encoding: end_line == start_line.
    validate_patches([_p(3, 3, replacement="")], document_line_count=5)


def test_validate_rejects_pure_insert_then_regular_patch_at_same_line():
    # A pure insert at line 3 and a replace at line 3 conflict — the apply
    # order is ambiguous. The bug here was input-order dependent: the
    # reverse order is already caught by the prev.end_line >= curr.start_line
    # check.
    with pytest.raises(ValueError, match="overlap"):
        validate_patches(
            [_p(3, 2), _p(3, 3)],
            document_line_count=10,
        )


def test_validate_accepts_adjacent_non_overlapping_patches():
    # Patch ending at line 2 and the next starting at line 3 do not overlap.
    validate_patches(
        [_p(1, 2), _p(3, 4)],
        document_line_count=10,
    )


def test_validate_message_names_patch_index():
    with pytest.raises(ValueError, match="patch 1"):
        validate_patches(
            [_p(1, 1), _p(3, 6)],
            document_line_count=5,
        )


def test_render_single_line():
    assert render_document_with_line_numbers("hello") == "   1: hello"


def test_render_multiple_lines():
    out = render_document_with_line_numbers("a\nb\nc")
    assert out == "   1: a\n   2: b\n   3: c"


def test_render_preserves_trailing_blank_line():
    # "a\n" splits into ["a", ""] which means the document has 2 lines
    # logically; the renderer should reflect that so the LLM can target
    # the trailing blank line for append/replace.
    out = render_document_with_line_numbers("a\n")
    assert out == "   1: a\n   2: "


def test_render_empty_document():
    # An empty document has zero lines; the renderer returns an empty string.
    assert render_document_with_line_numbers("") == ""


def test_system_prompt_mentions_replace_lines_and_no_overlap():
    assert "replace_lines" in EDIT_DOCUMENT_SYSTEM_PROMPT
    assert "overlap" in EDIT_DOCUMENT_SYSTEM_PROMPT.lower()


import db
from agents.config import EDIT_DOCUMENT_V1_UUID


@pytest.fixture
def app_ctx():
    """Push a Flask app context (the StructuredLLMAgent path expects one
    because resolved_model_kwargs / db.session are accessed during call).
    Schema/validator tests don't need this fixture; only handle() does."""
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    try:
        yield app
    finally:
        ctx.pop()


def _stub_agent(app_ctx, monkeypatch, response_plan: EditPlan):
    """Return an EditDocumentAgentV1 whose _structured_call returns the given
    plan instead of hitting LM Studio. Avoids any model-group dependency."""
    agent = EditDocumentAgentV1(
        agent_uuid=EDIT_DOCUMENT_V1_UUID, name="edit_document_v1", send=lambda m: None
    )
    # Skip ModelGroupAgent.setup so we don't require a bound group.
    agent.model_group_uuid = None
    agent.candidate_model_uuids = []
    # Accept the validator kwarg added in the fallback-fix change. The stub
    # does NOT invoke it — happy-path tests intentionally pass good plans.
    monkeypatch.setattr(
        agent, "_structured_call",
        lambda _user_prompt, validator=None: response_plan,
    )
    return agent


def test_handle_returns_patches(app_ctx, monkeypatch):
    plan = EditPlan(
        patches=[
            Patch(
                op="replace_lines", start_line=1, end_line=1,
                replacement="DONE", intent="mark done",
            )
        ]
    )
    agent = _stub_agent(app_ctx, monkeypatch, plan)
    result = agent.handle(
        journal_id=0,
        payload={"document": "TODO\nprint('x')", "instructions": "mark TODO as done"},
    )
    assert result == {
        "ok": True,
        "patches": [
            {
                "op": "replace_lines",
                "start_line": 1,
                "end_line": 1,
                "replacement": "DONE",
                "intent": "mark done",
            }
        ],
    }


def test_handle_raises_on_missing_document(app_ctx, monkeypatch):
    agent = _stub_agent(app_ctx, monkeypatch, EditPlan(patches=[]))
    with pytest.raises(ValueError, match="document"):
        agent.handle(journal_id=0, payload={"instructions": "x"})


def test_handle_raises_on_missing_instructions(app_ctx, monkeypatch):
    agent = _stub_agent(app_ctx, monkeypatch, EditPlan(patches=[]))
    with pytest.raises(ValueError, match="instructions"):
        agent.handle(journal_id=0, payload={"document": "x"})


def test_handle_raises_on_blank_document(app_ctx, monkeypatch):
    agent = _stub_agent(app_ctx, monkeypatch, EditPlan(patches=[]))
    with pytest.raises(ValueError, match="document"):
        agent.handle(journal_id=0, payload={"document": "   ", "instructions": "x"})


def test_handle_raises_when_validator_rejects_all_models(app_ctx, monkeypatch):
    """When _structured_call's validator rejects every model in the bound
    group, the final RuntimeError propagates out of handle()."""
    bad_plan = EditPlan(
        patches=[
            Patch(
                op="replace_lines", start_line=1, end_line=99,
                replacement="x", intent="i",
            )
        ]
    )

    def stub(user_prompt, validator=None):
        # Simulate _structured_call's behavior: invoke validator on each
        # candidate; if it raises for every candidate, raise RuntimeError
        # as the real code does at end-of-loop.
        try:
            if validator is not None:
                validator(bad_plan)
            return bad_plan
        except Exception as e:
            raise RuntimeError(
                f"agent edit_document: all models failed; last error: {e}"
            ) from e

    agent = EditDocumentAgentV1(
        agent_uuid=EDIT_DOCUMENT_V1_UUID, name="edit_document_v1", send=lambda _: None
    )
    agent.model_group_uuid = None
    agent.candidate_model_uuids = []
    monkeypatch.setattr(agent, "_structured_call", stub)

    with pytest.raises(RuntimeError, match="end_line"):
        agent.handle(
            journal_id=0,
            payload={"document": "a\nb", "instructions": "edit it"},
        )


def test_handle_passes_validator_to_structured_call(app_ctx, monkeypatch):
    """Regression guard: handle() must pass a validator into _structured_call
    so that bad-LLM output triggers model-group fallback."""
    captured: dict[str, object] = {}
    good_plan = EditPlan(
        patches=[
            Patch(
                op="replace_lines", start_line=1, end_line=1,
                replacement="DONE", intent="mark done",
            )
        ]
    )

    def stub(user_prompt, validator=None):
        captured["validator"] = validator
        return good_plan

    agent = EditDocumentAgentV1(
        agent_uuid=EDIT_DOCUMENT_V1_UUID, name="edit_document_v1", send=lambda _: None
    )
    agent.model_group_uuid = None
    agent.candidate_model_uuids = []
    monkeypatch.setattr(agent, "_structured_call", stub)

    result = agent.handle(
        journal_id=0,
        payload={"document": "TODO", "instructions": "mark done"},
    )

    assert result["ok"] is True
    assert callable(captured["validator"])
    # The captured validator should reject patches out of range for a
    # 1-line document. (We don't pass the real document here; the validator
    # closes over line_count=1 derived from the payload above.)
    bad = EditPlan(
        patches=[Patch(op="replace_lines", start_line=1, end_line=99,
                       replacement="x", intent="i")]
    )
    with pytest.raises(ValueError, match="end_line"):
        captured["validator"](bad)  # type: ignore[operator]


def test_user_prompt_includes_line_numbers_and_instructions(app_ctx, monkeypatch):
    agent = _stub_agent(app_ctx, monkeypatch, EditPlan(patches=[]))
    prompt = agent.user_prompt(
        {"document": "alpha\nbeta", "instructions": "change beta to gamma"}
    )
    assert "   1: alpha" in prompt
    assert "   2: beta" in prompt
    assert "change beta to gamma" in prompt
