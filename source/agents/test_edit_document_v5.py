"""Tests for agent_edit_document_v5.

v5 exposes two LLM-facing patch ops as a discriminated union (replace
and insert) and compiles each to an internal canonical NormalizedPatch
before validation, EOF-normalization, and journal serialization. On
top of the patch vocabulary it also keeps v4's logical-line view of the
document.

    python -m pytest test_agent_edit_document_v5.py -v
"""

import pytest
from uuid import uuid4
from pydantic import ValidationError

from agents.edit_document_v5 import (
    EDIT_DOCUMENT_V5_SYSTEM_PROMPT,
    EditDocumentAgentV5,
    EditPlan,
    InsertPatch,
    NormalizedPatch,
    ReplacePatch,
    apply_eof_policy,
    logical_line_count,
    logical_lines,
    normalize_patch,
    render_document_with_line_numbers,
    validate_patches,
)
from agents.patch_apply import apply_patches


# ----- Schema layer -----------------------------------------------------------

def test_replace_patch_start_line_zero_rejected_at_normalize():
    # ReplacePatch itself does not enforce start_line >= 1 at the
    # LLM-facing layer; the rule is caught when normalize_patch builds
    # a NormalizedPatch whose start_line has ge=1.
    p = ReplacePatch(op="replace", start_line=0, end_line=1, replacement="x", intent="i")
    with pytest.raises(ValidationError):
        normalize_patch(p)


def test_replace_patch_rejects_end_line_zero():
    # v5's ReplacePatch is replace/delete only — no inverted ranges.
    with pytest.raises(ValidationError):
        ReplacePatch(op="replace", start_line=1, end_line=0, replacement="x", intent="i")


def test_replace_patch_rejects_end_line_less_than_start_line():
    # An LLM trying to "fake" an append via end_line=start_line-1 must
    # be rejected so the model-group fallback can retry. The pydantic
    # model validator surfaces the misuse with a message pointing the
    # model at the insert op.
    with pytest.raises(ValidationError, match="insert"):
        ReplacePatch(op="replace", start_line=4, end_line=3, replacement="x", intent="i")


def test_replace_patch_minimal():
    p = ReplacePatch(op="replace", start_line=2, end_line=3, replacement="x", intent="i")
    assert p.op == "replace"
    assert p.start_line == 2


def test_insert_patch_minimal():
    p = InsertPatch(op="insert", before_line=3, text="x", intent="i")
    assert p.op == "insert"
    assert p.before_line == 3


def test_insert_patch_accepts_empty_text_for_blank_line():
    p = InsertPatch(op="insert", before_line=3, text="", intent="i")
    assert p.text == ""


def test_insert_patch_before_line_zero_rejected_at_normalize():
    # Same pattern as the ReplacePatch counterpart: InsertPatch is
    # permissive at the LLM-facing layer; the rule lives in
    # NormalizedPatch's ge=1 on start_line, applied during normalize.
    p = InsertPatch(op="insert", before_line=0, text="x", intent="i")
    with pytest.raises(ValidationError):
        normalize_patch(p)


def test_edit_plan_accepts_done_with_one_replace():
    plan = EditPlan(
        patches=[ReplacePatch(op="replace", start_line=1, end_line=1,
                              replacement="X", intent="i")],
        status="done",
        comment="ok",
    )
    assert len(plan.patches) == 1


def test_edit_plan_accepts_done_with_mixed_op_patches():
    plan = EditPlan(
        patches=[
            ReplacePatch(op="replace", start_line=1, end_line=1, replacement="X", intent="i"),
            InsertPatch(op="insert", before_line=3, text="Y", intent="i"),
        ],
        status="done",
        comment="ok",
    )
    assert plan.patches[0].op == "replace"
    assert plan.patches[1].op == "insert"


def test_edit_plan_accepts_unclear_with_zero_patches():
    plan = EditPlan(
        patches=[],
        status="unclear",
        comment="Instruction refers to something not in the document.",
    )
    assert plan.status == "unclear"


def test_edit_plan_rejects_empty_comment():
    with pytest.raises(ValidationError):
        EditPlan(patches=[], status="done", comment="")


def test_edit_plan_rejects_unknown_status():
    with pytest.raises(ValidationError):
        EditPlan(patches=[], status="maybe", comment="x")  # pyright: ignore[reportArgumentType]


def test_edit_plan_rejects_unknown_op_in_patches():
    with pytest.raises(ValidationError):
        EditPlan(
            patches=[{"op": "drop_table", "intent": "evil"}],  # pyright: ignore[reportArgumentType]
            status="done",
            comment="x",
        )


# ----- Logical-line model -----------------------------------------------------

def test_logical_lines_empty_document():
    assert logical_lines("") == []


def test_logical_lines_single_line_no_trailing_newline():
    assert logical_lines("alpha") == ["alpha"]


def test_logical_lines_single_line_with_trailing_newline():
    assert logical_lines("alpha\n") == ["alpha"]


def test_logical_lines_multiple_lines_no_trailing_newline():
    assert logical_lines("alpha\nbeta") == ["alpha", "beta"]


def test_logical_lines_multiple_lines_with_trailing_newline():
    assert logical_lines("alpha\nbeta\n") == ["alpha", "beta"]


def test_logical_lines_double_trailing_newline_is_blank_line():
    assert logical_lines("alpha\n\n") == ["alpha", ""]


def test_logical_line_count_matches_logical_lines_len():
    for doc in ["", "x", "x\n", "x\ny", "x\ny\n", "x\n\n"]:
        assert logical_line_count(doc) == len(logical_lines(doc))


# ----- Renderer ---------------------------------------------------------------

def test_render_empty_document_shows_eof_after_line_zero():
    assert render_document_with_line_numbers("") == "EOF is after line 0."


def test_render_single_line_includes_eof_marker():
    assert render_document_with_line_numbers("hello") == (
        "   1: hello\n"
        "EOF is after line 1."
    )


def test_render_folds_single_trailing_newline():
    assert render_document_with_line_numbers("alpha\n") == (
        "   1: alpha\n"
        "EOF is after line 1."
    )


def test_render_keeps_explicit_blank_lines():
    assert render_document_with_line_numbers("alpha\n\n") == (
        "   1: alpha\n"
        "   2: \n"
        "EOF is after line 2."
    )


def test_render_multiple_lines_with_trailing_newline():
    assert render_document_with_line_numbers("a\nb\nc\n") == (
        "   1: a\n"
        "   2: b\n"
        "   3: c\n"
        "EOF is after line 3."
    )


# ----- normalize_patch --------------------------------------------------------

def test_normalize_replace_passes_through():
    src = ReplacePatch(op="replace", start_line=3, end_line=4,
                      replacement="X", intent="i")
    out = normalize_patch(src)
    assert out == NormalizedPatch(
        start_line=3, end_line=4, replacement="X", intent="i",
    )


def test_normalize_insert_uses_line_minus_one_as_end():
    src = InsertPatch(op="insert", before_line=3, text="X", intent="i")
    out = normalize_patch(src)
    assert out == NormalizedPatch(
        start_line=3, end_line=2, replacement="X", intent="i",
    )


def test_normalize_insert_line_one_uses_end_line_zero():
    src = InsertPatch(op="insert", before_line=1, text="X", intent="i")
    out = normalize_patch(src)
    assert out.start_line == 1
    assert out.end_line == 0


def test_normalize_insert_with_empty_text_keeps_empty_replacement():
    src = InsertPatch(op="insert", before_line=3, text="", intent="i")
    out = normalize_patch(src)
    assert out.replacement == ""
    assert out.start_line == 3
    assert out.end_line == 2


def test_normalize_preserves_intent_across_both_ops():
    intent = "exact intent string"
    for src in [
        ReplacePatch(op="replace", start_line=1, end_line=1, replacement="X", intent=intent),
        InsertPatch(op="insert", before_line=1, text="X", intent=intent),
    ]:
        out = normalize_patch(src)
        assert out.intent == intent


# ----- Validator --------------------------------------------------------------

def _np(start, end, replacement="x", intent="i"):
    return NormalizedPatch(
        start_line=start, end_line=end,
        replacement=replacement, intent=intent,
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
    validate_patches([_np(3, 2, replacement="")], document_line_count=5)


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
        validate_patches([_np(2, 4), _np(3, 5)], document_line_count=10)


def test_validate_rejects_two_inserts_at_same_line():
    with pytest.raises(ValueError, match="overlap"):
        validate_patches([_np(3, 2), _np(3, 2)], document_line_count=10)


def test_validate_rejects_pure_insert_then_regular_patch_at_same_line():
    with pytest.raises(ValueError, match="overlap"):
        validate_patches([_np(3, 2), _np(3, 3)], document_line_count=10)


def test_validate_accepts_adjacent_non_overlapping_patches():
    validate_patches([_np(1, 2), _np(3, 4)], document_line_count=10)


# ----- EOF policy -------------------------------------------------------------

def test_eof_policy_no_op_when_original_had_trailing_newline():
    patches = [_np(1, 1, replacement="beta"), _np(3, 2, replacement="gamma")]
    out = apply_eof_policy(patches, document_line_count=2,
                           original_had_trailing_newline=True)
    assert out[0].replacement == "beta"
    assert out[1].replacement == "gamma"


def test_eof_policy_appends_newline_when_replace_touches_eof():
    patches = [_np(1, 1, replacement="beta")]
    apply_eof_policy(patches, document_line_count=1,
                     original_had_trailing_newline=False)
    assert patches[0].replacement == "beta\n"


def test_eof_policy_appends_newline_when_insert_at_eof():
    # Insert(line=2) on a 1-line doc -> NormalizedPatch(start=2, end=1).
    patches = [_np(2, 1, replacement="more")]
    apply_eof_policy(patches, document_line_count=1,
                     original_had_trailing_newline=False)
    assert patches[0].replacement == "more\n"


def test_eof_policy_skips_replacement_already_ending_with_newline():
    patches = [_np(1, 1, replacement="beta\n")]
    apply_eof_policy(patches, document_line_count=1,
                     original_had_trailing_newline=False)
    assert patches[0].replacement == "beta\n"


def test_eof_policy_skips_empty_replacement():
    patches = [_np(2, 1, replacement=""), _np(1, 1, replacement="")]
    apply_eof_policy(patches, document_line_count=1,
                     original_had_trailing_newline=False)
    assert patches[0].replacement == ""
    assert patches[1].replacement == ""


def test_eof_policy_skips_interior_patch():
    patches = [_np(2, 2, replacement="X")]
    apply_eof_policy(patches, document_line_count=5,
                     original_had_trailing_newline=False)
    assert patches[0].replacement == "X"


# ----- Integration with patch_apply -------------------------------------------

def _pipeline(document: str, patches: list) -> str:
    """Normalize -> validate -> EOF policy -> apply. Mirrors handle()."""
    n = logical_line_count(document)
    normalized = [normalize_patch(p) for p in patches]
    validate_patches(normalized, document_line_count=n)
    apply_eof_policy(
        normalized,
        document_line_count=n,
        original_had_trailing_newline=document.endswith("\n"),
    )
    return apply_patches(document, [p.model_dump() for p in normalized])


def test_integration_replace_final_line_adds_trailing_newline():
    assert _pipeline(
        "alpha",
        [ReplacePatch(op="replace", start_line=1, end_line=1, replacement="beta", intent="i")],
    ) == "beta\n"


def test_integration_insert_append_to_file_without_trailing_newline():
    assert _pipeline(
        "alpha",
        [InsertPatch(op="insert", before_line=2, text="more", intent="i")],
    ) == "alpha\nmore\n"


def test_integration_insert_append_to_file_with_trailing_newline():
    assert _pipeline(
        "alpha\n",
        [InsertPatch(op="insert", before_line=2, text="more", intent="i")],
    ) == "alpha\nmore\n"


def test_integration_insert_blank_line_to_file_without_trailing_newline():
    assert _pipeline(
        "alpha",
        [InsertPatch(op="insert", before_line=2, text="", intent="i")],
    ) == "alpha\n"


def test_integration_insert_blank_line_to_file_with_trailing_newline():
    assert _pipeline(
        "alpha\n",
        [InsertPatch(op="insert", before_line=2, text="", intent="i")],
    ) == "alpha\n\n"


def test_integration_interior_edit_preserves_no_trailing_newline():
    assert _pipeline(
        "alpha\nbeta\ngamma",
        [ReplacePatch(op="replace", start_line=2, end_line=2, replacement="BETA", intent="i")],
    ) == "alpha\nBETA\ngamma"


def test_integration_interior_edit_preserves_trailing_newline():
    assert _pipeline(
        "alpha\nbeta\ngamma\n",
        [ReplacePatch(op="replace", start_line=2, end_line=2, replacement="BETA", intent="i")],
    ) == "alpha\nBETA\ngamma\n"


def test_integration_insert_before_first_line():
    assert _pipeline(
        "alpha\nbeta",
        [InsertPatch(op="insert", before_line=1, text="zero", intent="i")],
    ) == "zero\nalpha\nbeta"


def test_integration_replacement_already_has_trailing_newline_not_doubled():
    assert _pipeline(
        "alpha",
        [InsertPatch(op="insert", before_line=2, text="more\n", intent="i")],
    ) == "alpha\nmore\n"


def test_integration_empty_document_with_insert():
    assert _pipeline(
        "",
        [InsertPatch(op="insert", before_line=1, text="hello", intent="i")],
    ) == "hello\n"


def test_integration_mixed_replace_and_insert():
    # Both patches are interior (replace at line 2, insert at line 1);
    # neither touches EOF on a 3-line doc, so the original no-trailing-
    # newline state is preserved.
    assert _pipeline(
        "a\nb\nc",
        [
            ReplacePatch(op="replace", start_line=2, end_line=2, replacement="B", intent="i"),
            InsertPatch(op="insert", before_line=1, text="zero", intent="i"),
        ],
    ) == "zero\na\nB\nc"


# ----- System prompt ----------------------------------------------------------

def test_system_prompt_mentions_both_ops():
    assert '"replace"' in EDIT_DOCUMENT_V5_SYSTEM_PROMPT
    assert '"insert"' in EDIT_DOCUMENT_V5_SYSTEM_PROMPT


def test_system_prompt_mentions_eof_marker():
    assert "EOF" in EDIT_DOCUMENT_V5_SYSTEM_PROMPT


def test_system_prompt_documents_status_and_comment():
    assert "status" in EDIT_DOCUMENT_V5_SYSTEM_PROMPT
    assert '"done"' in EDIT_DOCUMENT_V5_SYSTEM_PROMPT
    assert '"partial"' in EDIT_DOCUMENT_V5_SYSTEM_PROMPT
    assert '"unclear"' in EDIT_DOCUMENT_V5_SYSTEM_PROMPT
    assert "comment" in EDIT_DOCUMENT_V5_SYSTEM_PROMPT


# ----- handle() / agent wiring ------------------------------------------------

import db
from agents.config import EDIT_DOCUMENT_V5_UUID


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


def _stub_agent(app_ctx, monkeypatch, response_plan: EditPlan):
    agent = EditDocumentAgentV5(
        agent_uuid=EDIT_DOCUMENT_V5_UUID, name="edit_document_v5", send=lambda _: None
    )
    agent.model_group_uuid = None
    agent.candidate_model_uuids = []
    monkeypatch.setattr(
        agent, "_structured_call",
        lambda _user_prompt, validator=None: response_plan,
    )
    return agent


def test_handle_returns_normalized_replace(app_ctx, monkeypatch):
    plan = EditPlan(
        patches=[ReplacePatch(op="replace", start_line=1, end_line=1,
                              replacement="DONE", intent="mark done")],
        status="done",
        comment="Replaced line 1.",
    )
    agent = _stub_agent(app_ctx, monkeypatch, plan)
    result = agent.handle(
        journal_id=uuid4(),
        payload={"document": "TODO\nprint('x')", "instructions": "mark TODO as done"},
    )
    # Interior edit, no trailing newline -> no EOF mutation.
    # Journal patches use the canonical (no-op-field) shape.
    assert result == {
        "ok": True,
        "status": "done",
        "comment": "Replaced line 1.",
        "patches": [
            {"start_line": 1, "end_line": 1, "replacement": "DONE", "intent": "mark done"}
        ],
    }


def test_handle_normalizes_insert_to_canonical(app_ctx, monkeypatch):
    # Insert(line=3) -> canonical (start=3, end=2).
    plan = EditPlan(
        patches=[InsertPatch(op="insert", before_line=3, text="X", intent="insert X")],
        status="done",
        comment="Inserted X before line 3.",
    )
    agent = _stub_agent(app_ctx, monkeypatch, plan)
    result = agent.handle(
        journal_id=uuid4(),
        payload={"document": "a\nb\nc\nd\ne", "instructions": "insert X before line 3"},
    )
    assert result["patches"] == [
        {"start_line": 3, "end_line": 2, "replacement": "X", "intent": "insert X"}
    ]


def test_handle_replace_final_line_no_trailing_newline_adds_one(app_ctx, monkeypatch):
    plan = EditPlan(
        patches=[ReplacePatch(op="replace", start_line=1, end_line=1,
                              replacement="beta", intent="replace")],
        status="done",
        comment="ok",
    )
    agent = _stub_agent(app_ctx, monkeypatch, plan)
    result = agent.handle(
        journal_id=uuid4(),
        payload={"document": "alpha", "instructions": "x"},
    )
    assert result["patches"] == [
        {"start_line": 1, "end_line": 1, "replacement": "beta\n", "intent": "replace"}
    ]


def test_handle_replace_final_line_with_trailing_newline_no_mutation(app_ctx, monkeypatch):
    plan = EditPlan(
        patches=[ReplacePatch(op="replace", start_line=1, end_line=1,
                              replacement="beta", intent="replace")],
        status="done",
        comment="ok",
    )
    agent = _stub_agent(app_ctx, monkeypatch, plan)
    result = agent.handle(
        journal_id=uuid4(),
        payload={"document": "alpha\n", "instructions": "x"},
    )
    assert result["patches"][0]["replacement"] == "beta"


def test_handle_insert_append_to_no_newline_file(app_ctx, monkeypatch):
    plan = EditPlan(
        patches=[InsertPatch(op="insert", before_line=2, text="more", intent="add")],
        status="done",
        comment="ok",
    )
    agent = _stub_agent(app_ctx, monkeypatch, plan)
    result = agent.handle(
        journal_id=uuid4(),
        payload={"document": "alpha", "instructions": "x"},
    )
    assert result["patches"] == [
        {"start_line": 2, "end_line": 1, "replacement": "more\n", "intent": "add"}
    ]


def test_handle_uses_logical_line_count_for_validation(app_ctx, monkeypatch):
    # "alpha\n" has logical_line_count=1, so a replace targeting line 2
    # is out of range. (Raw split sees 2 entries; v5 sees 1.)
    plan = EditPlan(
        patches=[ReplacePatch(op="replace", start_line=2, end_line=2,
                              replacement="X", intent="i")],
        status="done",
        comment="ok",
    )
    bad_agent = EditDocumentAgentV5(
        agent_uuid=EDIT_DOCUMENT_V5_UUID, name="edit_document_v5", send=lambda _: None
    )
    bad_agent.model_group_uuid = None
    bad_agent.candidate_model_uuids = []

    def stub(user_prompt, validator=None):
        try:
            if validator is not None:
                validator(plan)
            return plan
        except Exception as e:
            raise RuntimeError(
                f"agent edit_document_v5: all models failed; last error: {e}"
            ) from e

    monkeypatch.setattr(bad_agent, "_structured_call", stub)
    with pytest.raises(RuntimeError, match="start_line"):
        bad_agent.handle(
            journal_id=uuid4(),
            payload={"document": "alpha\n", "instructions": "x"},
        )


def test_handle_raises_on_missing_document(app_ctx, monkeypatch):
    plan = EditPlan(patches=[], status="done", comment="noop")
    agent = _stub_agent(app_ctx, monkeypatch, plan)
    with pytest.raises(ValueError, match="document"):
        agent.handle(journal_id=uuid4(), payload={"instructions": "x"})


def test_handle_raises_on_missing_instructions(app_ctx, monkeypatch):
    plan = EditPlan(patches=[], status="done", comment="noop")
    agent = _stub_agent(app_ctx, monkeypatch, plan)
    with pytest.raises(ValueError, match="instructions"):
        agent.handle(journal_id=uuid4(), payload={"document": "x"})


def test_handle_passes_validator_to_structured_call(app_ctx, monkeypatch):
    captured: dict[str, object] = {}
    good_plan = EditPlan(
        patches=[ReplacePatch(op="replace", start_line=1, end_line=1,
                              replacement="X", intent="i")],
        status="done",
        comment="ok",
    )

    def stub(user_prompt, validator=None):
        captured["validator"] = validator
        return good_plan

    agent = EditDocumentAgentV5(
        agent_uuid=EDIT_DOCUMENT_V5_UUID, name="edit_document_v5", send=lambda _: None
    )
    agent.model_group_uuid = None
    agent.candidate_model_uuids = []
    monkeypatch.setattr(agent, "_structured_call", stub)
    result = agent.handle(
        journal_id=uuid4(),
        payload={"document": "TODO", "instructions": "mark done"},
    )
    assert result["ok"] is True
    assert callable(captured["validator"])


def test_user_prompt_includes_logical_line_count_and_eof_marker(app_ctx, monkeypatch):
    plan = EditPlan(patches=[], status="done", comment="noop")
    agent = _stub_agent(app_ctx, monkeypatch, plan)
    prompt = agent.user_prompt(
        {"document": "alpha\n", "instructions": "change alpha to beta"}
    )
    assert "Document (1 lines)" in prompt
    assert "   1: alpha" in prompt
    assert "EOF is after line 1." in prompt
    assert "change alpha to beta" in prompt
