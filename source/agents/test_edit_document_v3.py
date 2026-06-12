"""Tests for agent_edit_document_v3.

v3 mirrors v2's test structure: schema/validator/renderer/handle. The
schema layer is the meaningful difference — four LLM-facing patch ops as
a discriminated union, plus a NormalizedPatch internal form.

    python -m pytest test_agent_edit_document_v3.py -v
"""

import pytest
from pydantic import ValidationError

from agents.edit_document_v3 import (
    EDIT_DOCUMENT_V3_SYSTEM_PROMPT,
    AppendNewlinePatch,
    AppendTextPatch,
    EditDocumentAgentV3,
    EditPlanV3,
    InsertBeforePatch,
    NormalizedPatch,
    ReplaceLinesPatch,
    normalize_patch,
    render_document_with_line_numbers,
    validate_patches,
)


def test_replace_lines_patch_minimal():
    p = ReplaceLinesPatch(
        op="replace_lines", start_line=3, end_line=3,
        replacement="x", intent="i",
    )
    assert p.op == "replace_lines"


def test_replace_lines_patch_rejects_start_line_zero():
    with pytest.raises(ValidationError):
        ReplaceLinesPatch(
            op="replace_lines", start_line=0, end_line=1,
            replacement="x", intent="i",
        )


def test_replace_lines_patch_rejects_end_line_zero():
    # v3 narrows end_line to ge=1 (no more inverted ranges in this op);
    # the insert/append affordances have their own ops.
    with pytest.raises(ValidationError):
        ReplaceLinesPatch(
            op="replace_lines", start_line=1, end_line=0,
            replacement="x", intent="i",
        )


def test_insert_before_patch_minimal():
    p = InsertBeforePatch(op="insert_before", line=3, text="x", intent="i")
    assert p.op == "insert_before"
    assert p.line == 3


def test_insert_before_patch_accepts_empty_text_for_blank_line():
    # Empty text = "insert one blank line before this line".
    p = InsertBeforePatch(op="insert_before", line=3, text="", intent="i")
    assert p.text == ""


def test_insert_before_patch_rejects_line_zero():
    with pytest.raises(ValidationError):
        InsertBeforePatch(op="insert_before", line=0, text="x", intent="i")


def test_append_text_patch_minimal():
    p = AppendTextPatch(op="append_text", text="x", intent="i")
    assert p.op == "append_text"
    assert p.text == "x"


def test_append_newline_patch_minimal():
    # No payload beyond intent — pure intent op.
    p = AppendNewlinePatch(op="append_newline", intent="i")
    assert p.op == "append_newline"


def test_edit_plan_v3_accepts_done_with_one_replace_lines():
    plan = EditPlanV3(
        patches=[ReplaceLinesPatch(
            op="replace_lines", start_line=1, end_line=1,
            replacement="X", intent="i",
        )],
        status="done",
        comment="Replaced line 1.",
    )
    assert len(plan.patches) == 1


def test_edit_plan_v3_accepts_done_with_mixed_op_patches():
    plan = EditPlanV3(
        patches=[
            ReplaceLinesPatch(op="replace_lines", start_line=1, end_line=1,
                              replacement="X", intent="i"),
            AppendTextPatch(op="append_text", text="Y", intent="i"),
        ],
        status="done",
        comment="Replaced and appended.",
    )
    assert plan.patches[0].op == "replace_lines"
    assert plan.patches[1].op == "append_text"


def test_edit_plan_v3_accepts_unclear_with_zero_patches():
    plan = EditPlanV3(
        patches=[],
        status="unclear",
        comment="Instruction refers to something not in the document.",
    )
    assert plan.status == "unclear"


def test_edit_plan_v3_rejects_empty_comment():
    with pytest.raises(ValidationError):
        EditPlanV3(patches=[], status="done", comment="")


def test_edit_plan_v3_rejects_unknown_status():
    with pytest.raises(ValidationError):
        EditPlanV3(patches=[], status="maybe", comment="x")  # pyright: ignore[reportArgumentType]


def test_edit_plan_v3_rejects_unknown_op_in_patches():
    # The discriminated union must reject unknown op values.
    with pytest.raises(ValidationError):
        EditPlanV3(
            patches=[{"op": "drop_table", "intent": "evil"}],  # pyright: ignore[reportArgumentType]
            status="done",
            comment="x",
        )


def test_normalized_patch_minimal():
    p = NormalizedPatch(
        op="replace_lines", start_line=3, end_line=3,
        replacement="x", intent="i",
    )
    assert p.op == "replace_lines"


def test_normalized_patch_accepts_end_line_zero_for_insert_before_line_one():
    p = NormalizedPatch(
        op="replace_lines", start_line=1, end_line=0,
        replacement="x", intent="i",
    )
    assert p.end_line == 0


def test_normalize_replace_lines_passes_through():
    src = ReplaceLinesPatch(
        op="replace_lines", start_line=3, end_line=4,
        replacement="X", intent="i",
    )
    out = normalize_patch(src, document_line_count=10)
    assert out == NormalizedPatch(
        op="replace_lines", start_line=3, end_line=4,
        replacement="X", intent="i",
    )


def test_normalize_insert_before_uses_line_minus_one_as_end():
    # InsertBeforePatch(line=3) -> (start=3, end=2): the canonical
    # pure-insert range.
    src = InsertBeforePatch(op="insert_before", line=3, text="X", intent="i")
    out = normalize_patch(src, document_line_count=10)
    assert out == NormalizedPatch(
        op="replace_lines", start_line=3, end_line=2,
        replacement="X", intent="i",
    )


def test_normalize_insert_before_line_one_uses_end_line_zero():
    src = InsertBeforePatch(op="insert_before", line=1, text="X", intent="i")
    out = normalize_patch(src, document_line_count=10)
    assert out.start_line == 1
    assert out.end_line == 0


def test_normalize_insert_before_with_empty_text_normalizes_to_empty_replacement():
    # Empty text on insert_before remains empty in normalized form (the
    # apply layer turns this into "insert one blank line").
    src = InsertBeforePatch(op="insert_before", line=3, text="", intent="i")
    out = normalize_patch(src, document_line_count=10)
    assert out.replacement == ""
    assert out.start_line == 3
    assert out.end_line == 2


def test_normalize_append_text_uses_n_plus_one_as_start():
    # AppendTextPatch on a 5-line doc -> (start=6, end=5).
    src = AppendTextPatch(op="append_text", text="X", intent="i")
    out = normalize_patch(src, document_line_count=5)
    assert out == NormalizedPatch(
        op="replace_lines", start_line=6, end_line=5,
        replacement="X", intent="i",
    )


def test_normalize_append_text_on_empty_doc():
    # 0-line doc -> (start=1, end=0).
    src = AppendTextPatch(op="append_text", text="X", intent="i")
    out = normalize_patch(src, document_line_count=0)
    assert out.start_line == 1
    assert out.end_line == 0
    assert out.replacement == "X"


def test_normalize_append_newline_uses_empty_replacement():
    src = AppendNewlinePatch(op="append_newline", intent="i")
    out = normalize_patch(src, document_line_count=5)
    assert out == NormalizedPatch(
        op="replace_lines", start_line=6, end_line=5,
        replacement="", intent="i",
    )


def test_normalize_preserves_intent_across_all_ops():
    intent = "the model's exact intent string"
    for src in [
        ReplaceLinesPatch(op="replace_lines", start_line=1, end_line=1,
                          replacement="X", intent=intent),
        InsertBeforePatch(op="insert_before", line=1, text="X", intent=intent),
        AppendTextPatch(op="append_text", text="X", intent=intent),
        AppendNewlinePatch(op="append_newline", intent=intent),
    ]:
        out = normalize_patch(src, document_line_count=5)
        assert out.intent == intent


def _np(start, end, replacement="x", intent="i"):
    """Helper: build a NormalizedPatch for validator tests."""
    return NormalizedPatch(
        op="replace_lines",
        start_line=start,
        end_line=end,
        replacement=replacement,
        intent=intent,
    )


def test_validate_accepts_simple_replace():
    validate_patches([_np(2, 3)], document_line_count=5)


def test_validate_accepts_pure_insertion_mid_document():
    validate_patches([_np(3, 2)], document_line_count=5)


def test_validate_accepts_insertion_before_line_one():
    validate_patches([_np(1, 0)], document_line_count=5)


def test_validate_accepts_append_after_last_line():
    validate_patches([_np(6, 5)], document_line_count=5)


def test_validate_accepts_deletion():
    validate_patches([_np(2, 3, replacement="")], document_line_count=5)


def test_validate_accepts_empty_patch_list():
    validate_patches([], document_line_count=5)


def test_validate_accepts_blank_line_insert_mid_document():
    # end_line < start_line + empty replacement = "insert one blank line".
    # Per the v2 relaxation (commit 7a339c0f) this is a real operation,
    # not a no-op. v3 inherits the same rule.
    validate_patches([_np(3, 2, replacement="")], document_line_count=5)


def test_validate_accepts_blank_line_append_at_end():
    validate_patches([_np(6, 5, replacement="")], document_line_count=5)


def test_validate_rejects_start_line_past_end():
    with pytest.raises(ValueError, match="start_line"):
        validate_patches([_np(7, 7)], document_line_count=5)


def test_validate_rejects_end_line_past_document():
    with pytest.raises(ValueError, match="end_line"):
        validate_patches([_np(3, 6)], document_line_count=5)


def test_validate_rejects_end_before_start_minus_one():
    with pytest.raises(ValueError, match="end_line"):
        validate_patches([_np(3, 1)], document_line_count=5)


def test_validate_rejects_overlapping_patches():
    with pytest.raises(ValueError, match="overlap"):
        validate_patches(
            [_np(2, 4), _np(3, 5)],
            document_line_count=10,
        )


def test_validate_rejects_two_inserts_at_same_line():
    with pytest.raises(ValueError, match="overlap"):
        validate_patches(
            [_np(3, 2), _np(3, 2)],
            document_line_count=10,
        )


def test_validate_rejects_pure_insert_then_regular_patch_at_same_line():
    with pytest.raises(ValueError, match="overlap"):
        validate_patches(
            [_np(3, 2), _np(3, 3)],
            document_line_count=10,
        )


def test_validate_accepts_adjacent_non_overlapping_patches():
    validate_patches(
        [_np(1, 2), _np(3, 4)],
        document_line_count=10,
    )


def test_validate_message_names_patch_index():
    with pytest.raises(ValueError, match="patch 1"):
        validate_patches(
            [_np(1, 1), _np(3, 6)],
            document_line_count=5,
        )


def test_render_single_line_includes_eof_marker():
    assert render_document_with_line_numbers("hello") == (
        "   1: hello\n"
        "EOF is after line 1."
    )


def test_render_multiple_lines_includes_eof_marker():
    assert render_document_with_line_numbers("a\nb\nc") == (
        "   1: a\n"
        "   2: b\n"
        "   3: c\n"
        "EOF is after line 3."
    )


def test_render_preserves_trailing_blank_line():
    assert render_document_with_line_numbers("a\n") == (
        "   1: a\n"
        "   2: \n"
        "EOF is after line 2."
    )


def test_render_empty_document_shows_eof_after_line_zero():
    assert render_document_with_line_numbers("") == "EOF is after line 0."


def test_system_prompt_mentions_all_four_ops():
    assert "replace_lines" in EDIT_DOCUMENT_V3_SYSTEM_PROMPT
    assert "insert_before" in EDIT_DOCUMENT_V3_SYSTEM_PROMPT
    assert "append_text" in EDIT_DOCUMENT_V3_SYSTEM_PROMPT
    assert "append_newline" in EDIT_DOCUMENT_V3_SYSTEM_PROMPT


def test_system_prompt_mentions_eof_marker():
    # Model needs to know about the EOF marker convention or it can't
    # use append_text / append_newline confidently.
    assert "EOF" in EDIT_DOCUMENT_V3_SYSTEM_PROMPT


def test_system_prompt_documents_status_and_comment():
    assert "status" in EDIT_DOCUMENT_V3_SYSTEM_PROMPT
    assert '"done"' in EDIT_DOCUMENT_V3_SYSTEM_PROMPT
    assert '"partial"' in EDIT_DOCUMENT_V3_SYSTEM_PROMPT
    assert '"unclear"' in EDIT_DOCUMENT_V3_SYSTEM_PROMPT
    assert "comment" in EDIT_DOCUMENT_V3_SYSTEM_PROMPT


import db
from agents.config import EDIT_DOCUMENT_V3_UUID


@pytest.fixture
def app_ctx():
    """Push a Flask app context — the agent's _structured_call path needs
    one because it touches db.session via resolved_model_kwargs."""
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    try:
        yield app
    finally:
        ctx.pop()


def _stub_agent(app_ctx, monkeypatch, response_plan: EditPlanV3):
    """Return an EditDocumentAgentV3 whose _structured_call returns the
    given plan instead of hitting LM Studio. Avoids any model-group
    dependency. The stub accepts the optional `validator` kwarg added in
    v1 but does NOT invoke it — happy-path tests pass valid plans."""
    agent = EditDocumentAgentV3(
        agent_uuid=EDIT_DOCUMENT_V3_UUID, name="edit_document_v3", send=lambda _: None
    )
    agent.model_group_uuid = None
    agent.candidate_model_uuids = []
    monkeypatch.setattr(
        agent, "_structured_call",
        lambda _user_prompt, validator=None: response_plan,
    )
    return agent


def test_handle_returns_status_comment_and_normalized_replace_lines(app_ctx, monkeypatch):
    plan = EditPlanV3(
        patches=[ReplaceLinesPatch(
            op="replace_lines", start_line=1, end_line=1,
            replacement="DONE", intent="mark done",
        )],
        status="done",
        comment="Replaced line 1.",
    )
    agent = _stub_agent(app_ctx, monkeypatch, plan)
    result = agent.handle(
        journal_id=0,
        payload={"document": "TODO\nprint('x')", "instructions": "mark TODO as done"},
    )
    assert result == {
        "ok": True,
        "status": "done",
        "comment": "Replaced line 1.",
        "patches": [
            {
                "op": "replace_lines",
                "start_line": 1, "end_line": 1,
                "replacement": "DONE", "intent": "mark done",
            }
        ],
    }


def test_handle_normalizes_append_text(app_ctx, monkeypatch):
    # 5-line doc; AppendTextPatch normalizes to (start=6, end=5).
    doc = "a\nb\nc\nd\ne"
    plan = EditPlanV3(
        patches=[AppendTextPatch(op="append_text", text="X", intent="append X")],
        status="done",
        comment="Appended X.",
    )
    agent = _stub_agent(app_ctx, monkeypatch, plan)
    result = agent.handle(
        journal_id=0,
        payload={"document": doc, "instructions": "append X"},
    )
    assert result["patches"] == [
        {
            "op": "replace_lines",
            "start_line": 6, "end_line": 5,
            "replacement": "X", "intent": "append X",
        }
    ]


def test_handle_normalizes_append_newline(app_ctx, monkeypatch):
    doc = "comment\n\n\n\n"  # 5 lines after split
    plan = EditPlanV3(
        patches=[AppendNewlinePatch(op="append_newline", intent="append blank")],
        status="done",
        comment="Appended one blank line.",
    )
    agent = _stub_agent(app_ctx, monkeypatch, plan)
    result = agent.handle(
        journal_id=0,
        payload={"document": doc, "instructions": "append a newline"},
    )
    assert result["patches"] == [
        {
            "op": "replace_lines",
            "start_line": 6, "end_line": 5,
            "replacement": "", "intent": "append blank",
        }
    ]


def test_handle_normalizes_insert_before(app_ctx, monkeypatch):
    plan = EditPlanV3(
        patches=[InsertBeforePatch(op="insert_before", line=3, text="X", intent="insert X")],
        status="done",
        comment="Inserted X before line 3.",
    )
    agent = _stub_agent(app_ctx, monkeypatch, plan)
    result = agent.handle(
        journal_id=0,
        payload={"document": "a\nb\nc\nd\ne", "instructions": "insert X before line 3"},
    )
    assert result["patches"] == [
        {
            "op": "replace_lines",
            "start_line": 3, "end_line": 2,
            "replacement": "X", "intent": "insert X",
        }
    ]


def test_handle_raises_on_missing_document(app_ctx, monkeypatch):
    plan = EditPlanV3(patches=[], status="done", comment="noop")
    agent = _stub_agent(app_ctx, monkeypatch, plan)
    with pytest.raises(ValueError, match="document"):
        agent.handle(journal_id=0, payload={"instructions": "x"})


def test_handle_raises_on_missing_instructions(app_ctx, monkeypatch):
    plan = EditPlanV3(patches=[], status="done", comment="noop")
    agent = _stub_agent(app_ctx, monkeypatch, plan)
    with pytest.raises(ValueError, match="instructions"):
        agent.handle(journal_id=0, payload={"document": "x"})


def test_handle_raises_when_validator_rejects_all_models(app_ctx, monkeypatch):
    """When _structured_call's validator rejects every model in the bound
    group, the final RuntimeError propagates out of handle()."""
    bad_plan = EditPlanV3(
        patches=[ReplaceLinesPatch(
            op="replace_lines", start_line=1, end_line=99,
            replacement="x", intent="i",
        )],
        status="done",
        comment="patches are out of range",
    )

    def stub(user_prompt, validator=None):
        try:
            if validator is not None:
                validator(bad_plan)
            return bad_plan
        except Exception as e:
            raise RuntimeError(
                f"agent edit_document_v3: all models failed; last error: {e}"
            ) from e

    agent = EditDocumentAgentV3(
        agent_uuid=EDIT_DOCUMENT_V3_UUID, name="edit_document_v3", send=lambda _: None
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
    so that bad LLM output triggers model-group fallback."""
    captured: dict[str, object] = {}
    good_plan = EditPlanV3(
        patches=[ReplaceLinesPatch(
            op="replace_lines", start_line=1, end_line=1,
            replacement="X", intent="i",
        )],
        status="done",
        comment="ok",
    )

    def stub(user_prompt, validator=None):
        captured["validator"] = validator
        return good_plan

    agent = EditDocumentAgentV3(
        agent_uuid=EDIT_DOCUMENT_V3_UUID, name="edit_document_v3", send=lambda _: None
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
    bad = EditPlanV3(
        patches=[ReplaceLinesPatch(
            op="replace_lines", start_line=1, end_line=99,
            replacement="x", intent="i",
        )],
        status="done",
        comment="bad",
    )
    with pytest.raises(ValueError, match="end_line"):
        captured["validator"](bad)  # type: ignore[operator]


def test_user_prompt_includes_line_numbers_eof_and_instructions(app_ctx, monkeypatch):
    plan = EditPlanV3(patches=[], status="done", comment="noop")
    agent = _stub_agent(app_ctx, monkeypatch, plan)
    prompt = agent.user_prompt(
        {"document": "alpha\nbeta", "instructions": "change beta to gamma"}
    )
    assert "   1: alpha" in prompt
    assert "   2: beta" in prompt
    assert "EOF is after line 2." in prompt
    assert "change beta to gamma" in prompt
