"""Tests for agent_edit_document_v2.

v2 mirrors v1's tests (schema, validator, renderer, handle) and adds
three v2-specific schema tests for the `status` / `comment` fields. Like
v1, schema/validator/renderer tests are pure functions; handle() tests
stub `_structured_call` so they don't need LM Studio.

    python -m pytest test_agent_edit_document_v2.py -v
"""

import pytest
from pydantic import ValidationError

from agent_edit_document_v2 import (
    EDIT_DOCUMENT_V2_SYSTEM_PROMPT,
    EditDocumentAgentV2,
    EditPlanV2,
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


def test_editplan_v2_accepts_done_with_patches():
    plan = EditPlanV2(
        status="done",
        comment="Renamed foo to bar.",
        patches=[
            Patch(op="replace_lines", start_line=1, end_line=1, replacement="bar", intent="rename"),
        ],
    )
    assert plan.status == "done"
    assert plan.comment == "Renamed foo to bar."
    assert len(plan.patches) == 1


def test_editplan_v2_accepts_partial_with_comment():
    plan = EditPlanV2(
        status="partial",
        comment="Renamed foo on line 1; could not locate bar.",
        patches=[
            Patch(op="replace_lines", start_line=1, end_line=1, replacement="X", intent="rename foo"),
        ],
    )
    assert plan.status == "partial"


def test_editplan_v2_accepts_done_with_zero_patches():
    # Instruction resolves to a no-op (e.g. "remove TODO" when no TODO exists).
    plan = EditPlanV2(
        status="done",
        comment="No TODO line found; nothing to remove.",
        patches=[],
    )
    assert plan.status == "done"
    assert plan.patches == []


def test_editplan_v2_accepts_unclear_with_zero_patches():
    plan = EditPlanV2(
        status="unclear",
        comment="Instruction refers to 'the helper' but no function is marked as a helper.",
        patches=[],
    )
    assert plan.status == "unclear"
    assert plan.patches == []


def test_editplan_v2_rejects_empty_comment():
    with pytest.raises(ValidationError):
        EditPlanV2(status="done", comment="", patches=[])


def test_editplan_v2_rejects_unknown_status():
    with pytest.raises(ValidationError):
        EditPlanV2(status="maybe", comment="x", patches=[])  # pyright: ignore[reportArgumentType]


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
    with pytest.raises(ValueError, match="end_line"):
        validate_patches([_p(3, 1)], document_line_count=5)


def test_validate_accepts_blank_line_insert_mid_document():
    # end_line < start_line + empty replacement = "insert one blank line"
    # at the pure-insert position. Used to be rejected as no-op; the
    # encoding now produces a real blank-line insertion.
    validate_patches(
        [_p(3, 2, replacement="")],
        document_line_count=5,
    )


def test_validate_accepts_blank_line_insert_before_line_one():
    validate_patches(
        [_p(1, 0, replacement="")],
        document_line_count=5,
    )


def test_validate_accepts_blank_line_append_at_end():
    # The case that motivated the relaxation: append-position with empty
    # replacement means "append a blank line".
    validate_patches(
        [_p(6, 5, replacement="")],
        document_line_count=5,
    )


def test_validate_accepts_single_line_deletion():
    # The supported deletion encoding: end_line == start_line.
    validate_patches([_p(3, 3, replacement="")], document_line_count=5)


def test_validate_rejects_overlapping_patches():
    with pytest.raises(ValueError, match="overlap"):
        validate_patches(
            [_p(2, 4), _p(3, 5)],
            document_line_count=10,
        )


def test_validate_rejects_two_inserts_at_same_line():
    with pytest.raises(ValueError, match="overlap"):
        validate_patches(
            [_p(3, 2), _p(3, 2)],
            document_line_count=10,
        )


def test_validate_rejects_pure_insert_then_regular_patch_at_same_line():
    with pytest.raises(ValueError, match="overlap"):
        validate_patches(
            [_p(3, 2), _p(3, 3)],
            document_line_count=10,
        )


def test_validate_accepts_adjacent_non_overlapping_patches():
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
    out = render_document_with_line_numbers("a\n")
    assert out == "   1: a\n   2: "


def test_render_empty_document():
    assert render_document_with_line_numbers("") == ""


def test_system_prompt_mentions_replace_lines_and_no_overlap():
    assert "replace_lines" in EDIT_DOCUMENT_V2_SYSTEM_PROMPT
    assert "overlap" in EDIT_DOCUMENT_V2_SYSTEM_PROMPT.lower()


def test_system_prompt_documents_status_and_comment():
    # v2-specific: the prompt must explain the new fields so the model
    # populates them correctly.
    assert "status" in EDIT_DOCUMENT_V2_SYSTEM_PROMPT
    assert '"done"' in EDIT_DOCUMENT_V2_SYSTEM_PROMPT
    assert '"partial"' in EDIT_DOCUMENT_V2_SYSTEM_PROMPT
    assert '"unclear"' in EDIT_DOCUMENT_V2_SYSTEM_PROMPT
    assert "comment" in EDIT_DOCUMENT_V2_SYSTEM_PROMPT


import db
from agent_config import EDIT_DOCUMENT_V2_UUID


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


def _stub_agent(app_ctx, monkeypatch, response_plan: EditPlanV2):
    """Return an EditDocumentAgentV2 whose _structured_call returns the given
    plan instead of hitting LM Studio. Avoids any model-group dependency.
    The stub accepts the optional `validator` kwarg added in v1 but does NOT
    invoke it — happy-path tests pass valid plans."""
    agent = EditDocumentAgentV2(
        agent_uuid=EDIT_DOCUMENT_V2_UUID, name="edit_document_v2", send=lambda _: None
    )
    agent.model_group_uuid = None
    agent.candidate_model_uuids = []
    monkeypatch.setattr(
        agent, "_structured_call",
        lambda _user_prompt, validator=None: response_plan,
    )
    return agent


def test_handle_returns_status_comment_and_patches(app_ctx, monkeypatch):
    plan = EditPlanV2(
        status="done",
        comment="Marked TODO complete.",
        patches=[
            Patch(
                op="replace_lines", start_line=1, end_line=1,
                replacement="DONE", intent="mark done",
            )
        ],
    )
    agent = _stub_agent(app_ctx, monkeypatch, plan)
    result = agent.handle(
        journal_id=0,
        payload={"document": "TODO\nprint('x')", "instructions": "mark TODO as done"},
    )
    assert result == {
        "ok": True,
        "status": "done",
        "comment": "Marked TODO complete.",
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


def test_handle_returns_unclear_with_empty_patches(app_ctx, monkeypatch):
    plan = EditPlanV2(
        status="unclear",
        comment="Instruction refers to 'the helper' but no function is marked as helper.",
        patches=[],
    )
    agent = _stub_agent(app_ctx, monkeypatch, plan)
    result = agent.handle(
        journal_id=0,
        payload={"document": "def foo():\n    pass", "instructions": "rename the helper"},
    )
    assert result["status"] == "unclear"
    assert result["comment"].startswith("Instruction refers")
    assert result["patches"] == []


def test_handle_raises_on_missing_document(app_ctx, monkeypatch):
    plan = EditPlanV2(status="done", comment="noop", patches=[])
    agent = _stub_agent(app_ctx, monkeypatch, plan)
    with pytest.raises(ValueError, match="document"):
        agent.handle(journal_id=0, payload={"instructions": "x"})


def test_handle_raises_on_missing_instructions(app_ctx, monkeypatch):
    plan = EditPlanV2(status="done", comment="noop", patches=[])
    agent = _stub_agent(app_ctx, monkeypatch, plan)
    with pytest.raises(ValueError, match="instructions"):
        agent.handle(journal_id=0, payload={"document": "x"})


def test_handle_accepts_empty_document_for_generate_from_scratch(app_ctx, monkeypatch):
    # Empty document means "generate content from scratch". The model is
    # expected to emit a single insertion at start_line=1, end_line=0.
    plan = EditPlanV2(
        status="done",
        comment="Wrote a hello-world program from scratch.",
        patches=[
            Patch(
                op="replace_lines", start_line=1, end_line=0,
                replacement='print("hello world")', intent="initial content",
            )
        ],
    )
    agent = _stub_agent(app_ctx, monkeypatch, plan)
    result = agent.handle(
        journal_id=0,
        payload={"document": "", "instructions": "write a hello world program"},
    )
    assert result["status"] == "done"
    assert result["patches"][0]["replacement"] == 'print("hello world")'


def test_handle_accepts_whitespace_only_document(app_ctx, monkeypatch):
    # Whitespace-only document is treated as a literal one-line document
    # (line 1 is the whitespace); the agent does not pre-trim. Validator
    # rules apply to a 1-line document.
    plan = EditPlanV2(
        status="done",
        comment="Replaced the whitespace line with a comment.",
        patches=[
            Patch(
                op="replace_lines", start_line=1, end_line=1,
                replacement="# placeholder", intent="replace blank",
            )
        ],
    )
    agent = _stub_agent(app_ctx, monkeypatch, plan)
    result = agent.handle(
        journal_id=0,
        payload={"document": "   ", "instructions": "add a comment"},
    )
    assert result["status"] == "done"


def test_handle_raises_when_validator_rejects_all_models(app_ctx, monkeypatch):
    """When _structured_call's validator rejects every model in the bound
    group, the final RuntimeError propagates out of handle()."""
    bad_plan = EditPlanV2(
        status="done",
        comment="patches are out of range",
        patches=[
            Patch(
                op="replace_lines", start_line=1, end_line=99,
                replacement="x", intent="i",
            )
        ],
    )

    def stub(user_prompt, validator=None):
        try:
            if validator is not None:
                validator(bad_plan)
            return bad_plan
        except Exception as e:
            raise RuntimeError(
                f"agent edit_document_v2: all models failed; last error: {e}"
            ) from e

    agent = EditDocumentAgentV2(
        agent_uuid=EDIT_DOCUMENT_V2_UUID, name="edit_document_v2", send=lambda _: None
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
    good_plan = EditPlanV2(
        status="done",
        comment="mark done",
        patches=[
            Patch(
                op="replace_lines", start_line=1, end_line=1,
                replacement="DONE", intent="mark done",
            )
        ],
    )

    def stub(user_prompt, validator=None):
        captured["validator"] = validator
        return good_plan

    agent = EditDocumentAgentV2(
        agent_uuid=EDIT_DOCUMENT_V2_UUID, name="edit_document_v2", send=lambda _: None
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
    bad = EditPlanV2(
        status="done",
        comment="bad",
        patches=[Patch(op="replace_lines", start_line=1, end_line=99,
                       replacement="x", intent="i")]
    )
    with pytest.raises(ValueError, match="end_line"):
        captured["validator"](bad)  # type: ignore[operator]


def test_user_prompt_includes_line_numbers_and_instructions(app_ctx, monkeypatch):
    plan = EditPlanV2(status="done", comment="noop", patches=[])
    agent = _stub_agent(app_ctx, monkeypatch, plan)
    prompt = agent.user_prompt(
        {"document": "alpha\nbeta", "instructions": "change beta to gamma"}
    )
    assert "   1: alpha" in prompt
    assert "   2: beta" in prompt
    assert "change beta to gamma" in prompt
