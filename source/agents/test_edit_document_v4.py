"""Tests for agent_edit_document_v4.

v4 uses v2's single-op replace_lines encoding (with inverted ranges for
insert/append) and adds two things on top: a logical-line view of the
document and an EOF-normalization pass on the returned patches.

    python -m pytest test_agent_edit_document_v4.py -v
"""

import pytest
from pydantic import ValidationError

from agents.edit_document_v4 import (
    EDIT_DOCUMENT_V4_SYSTEM_PROMPT,
    EditDocumentAgentV4,
    EditPlan,
    ReplaceLinesPatch,
    apply_eof_policy,
    logical_line_count,
    logical_lines,
    render_document_with_line_numbers,
    validate_patches,
)
from agents.patch_apply import apply_patches


# ----- Schema layer -----------------------------------------------------------

def test_replace_lines_patch_rejects_start_line_zero():
    with pytest.raises(ValidationError):
        ReplaceLinesPatch(start_line=0, end_line=1, replacement="x", intent="i")


def test_replace_lines_patch_accepts_end_line_zero_for_insert_before_line_one():
    p = ReplaceLinesPatch(start_line=1, end_line=0, replacement="x", intent="i")
    assert p.end_line == 0


def test_replace_lines_patch_rejects_negative_end_line():
    with pytest.raises(ValidationError):
        ReplaceLinesPatch(start_line=2, end_line=-1, replacement="x", intent="i")


def test_edit_plan_v4_accepts_done_with_one_patch():
    plan = EditPlan(
        patches=[ReplaceLinesPatch(
            start_line=1, end_line=1, replacement="X", intent="i",
        )],
        status="done",
        comment="ok",
    )
    assert len(plan.patches) == 1


def test_edit_plan_v4_accepts_unclear_with_zero_patches():
    plan = EditPlan(
        patches=[],
        status="unclear",
        comment="Instruction refers to something not in the document.",
    )
    assert plan.status == "unclear"


def test_edit_plan_v4_rejects_empty_comment():
    with pytest.raises(ValidationError):
        EditPlan(patches=[], status="done", comment="")


def test_edit_plan_v4_rejects_unknown_status():
    with pytest.raises(ValidationError):
        EditPlan(patches=[], status="maybe", comment="x")  # pyright: ignore[reportArgumentType]


# ----- Logical-line model -----------------------------------------------------

def test_logical_lines_empty_document():
    assert logical_lines("") == []


def test_logical_lines_single_line_no_trailing_newline():
    assert logical_lines("alpha") == ["alpha"]


def test_logical_lines_single_line_with_trailing_newline():
    # The trailing "\n" is folded into the EOF marker, not shown as a
    # separate blank line. "alpha" and "alpha\n" both have 1 logical line.
    assert logical_lines("alpha\n") == ["alpha"]


def test_logical_lines_multiple_lines_no_trailing_newline():
    assert logical_lines("alpha\nbeta") == ["alpha", "beta"]


def test_logical_lines_multiple_lines_with_trailing_newline():
    assert logical_lines("alpha\nbeta\n") == ["alpha", "beta"]


def test_logical_lines_double_trailing_newline_is_blank_line():
    # The first "\n" is folded; the second "\n" remains as a blank line.
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
    # The key v4 difference vs v3: "alpha\n" and "alpha" render the same.
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


# ----- Validator --------------------------------------------------------------

def _p(start, end, replacement="x", intent="i"):
    return ReplaceLinesPatch(
        start_line=start, end_line=end,
        replacement=replacement, intent=intent,
    )


def test_validate_accepts_simple_replace():
    validate_patches([_p(2, 3)], document_line_count=5)


def test_validate_accepts_pure_insertion_mid_document():
    validate_patches([_p(3, 2)], document_line_count=5)


def test_validate_accepts_insertion_before_line_one():
    validate_patches([_p(1, 0)], document_line_count=5)


def test_validate_accepts_append_after_last_line():
    validate_patches([_p(6, 5)], document_line_count=5)


def test_validate_accepts_deletion():
    validate_patches([_p(2, 3, replacement="")], document_line_count=5)


def test_validate_accepts_empty_patch_list():
    validate_patches([], document_line_count=5)


def test_validate_accepts_blank_line_insert_mid_document():
    validate_patches([_p(3, 2, replacement="")], document_line_count=5)


def test_validate_rejects_start_line_past_end():
    with pytest.raises(ValueError, match="start_line"):
        validate_patches([_p(7, 7)], document_line_count=5)


def test_validate_rejects_end_line_past_document():
    with pytest.raises(ValueError, match="end_line"):
        validate_patches([_p(3, 6)], document_line_count=5)


def test_validate_rejects_end_before_start_minus_one():
    with pytest.raises(ValueError, match="end_line"):
        validate_patches([_p(3, 1)], document_line_count=5)


def test_validate_rejects_overlapping_patches():
    with pytest.raises(ValueError, match="overlap"):
        validate_patches([_p(2, 4), _p(3, 5)], document_line_count=10)


def test_validate_rejects_two_inserts_at_same_line():
    with pytest.raises(ValueError, match="overlap"):
        validate_patches([_p(3, 2), _p(3, 2)], document_line_count=10)


def test_validate_rejects_pure_insert_then_regular_patch_at_same_line():
    with pytest.raises(ValueError, match="overlap"):
        validate_patches([_p(3, 2), _p(3, 3)], document_line_count=10)


def test_validate_accepts_adjacent_non_overlapping_patches():
    validate_patches([_p(1, 2), _p(3, 4)], document_line_count=10)


# ----- EOF policy -------------------------------------------------------------

def test_eof_policy_no_op_when_original_had_trailing_newline():
    # When the original document already ends with "\n", the raw-split
    # line list ends with "" and apply_patches preserves the trailing
    # newline automatically. apply_eof_policy is a no-op here.
    patches = [_p(1, 1, replacement="beta"), _p(3, 2, replacement="gamma")]
    out = apply_eof_policy(patches, document_line_count=2,
                           original_had_trailing_newline=True)
    assert out[0].replacement == "beta"
    assert out[1].replacement == "gamma"


def test_eof_policy_appends_newline_when_replace_touches_eof():
    patches = [_p(1, 1, replacement="beta")]
    apply_eof_policy(patches, document_line_count=1,
                     original_had_trailing_newline=False)
    assert patches[0].replacement == "beta\n"


def test_eof_policy_appends_newline_when_append_text_touches_eof():
    patches = [_p(2, 1, replacement="more")]
    apply_eof_policy(patches, document_line_count=1,
                     original_had_trailing_newline=False)
    assert patches[0].replacement == "more\n"


def test_eof_policy_skips_replacement_already_ending_with_newline():
    patches = [_p(1, 1, replacement="beta\n")]
    apply_eof_policy(patches, document_line_count=1,
                     original_had_trailing_newline=False)
    assert patches[0].replacement == "beta\n"


def test_eof_policy_skips_empty_replacement():
    # Empty replacement at a pure-insert position is a blank-line
    # insert; at a replace position it is a deletion. Either way,
    # apply_patches already produces the right result without mutation.
    patches = [_p(2, 1, replacement=""), _p(1, 1, replacement="")]
    apply_eof_policy(patches, document_line_count=1,
                     original_had_trailing_newline=False)
    assert patches[0].replacement == ""
    assert patches[1].replacement == ""


def test_eof_policy_skips_interior_patch():
    patches = [_p(2, 2, replacement="X")]
    apply_eof_policy(patches, document_line_count=5,
                     original_had_trailing_newline=False)
    assert patches[0].replacement == "X"


# ----- Integration with patch_apply -------------------------------------------

def _eof(document: str, patches: list[ReplaceLinesPatch]) -> list[ReplaceLinesPatch]:
    apply_eof_policy(
        patches,
        document_line_count=logical_line_count(document),
        original_had_trailing_newline=document.endswith("\n"),
    )
    return patches


def test_integration_replace_final_line_adds_trailing_newline():
    document = "alpha"
    patches = _eof(document, [_p(1, 1, replacement="beta", intent="i")])
    assert apply_patches(document, [p.model_dump() for p in patches]) == "beta\n"


def test_integration_append_text_to_file_without_trailing_newline():
    document = "alpha"
    # Append after a 1-line doc = (start=2, end=1).
    patches = _eof(document, [_p(2, 1, replacement="more", intent="i")])
    assert apply_patches(document, [p.model_dump() for p in patches]) == "alpha\nmore\n"


def test_integration_append_text_to_file_with_trailing_newline():
    document = "alpha\n"
    patches = _eof(document, [_p(2, 1, replacement="more", intent="i")])
    assert apply_patches(document, [p.model_dump() for p in patches]) == "alpha\nmore\n"


def test_integration_append_blank_line_to_file_without_trailing_newline():
    # "alpha" + blank-line append -> "alpha\n" (supplies the missing
    # trailing newline; no extra blank line).
    document = "alpha"
    patches = _eof(document, [_p(2, 1, replacement="", intent="i")])
    assert apply_patches(document, [p.model_dump() for p in patches]) == "alpha\n"


def test_integration_append_blank_line_to_file_with_trailing_newline():
    # "alpha\n" + blank-line append -> "alpha\n\n" (adds a truly blank line).
    document = "alpha\n"
    patches = _eof(document, [_p(2, 1, replacement="", intent="i")])
    assert apply_patches(document, [p.model_dump() for p in patches]) == "alpha\n\n"


def test_integration_interior_edit_preserves_no_trailing_newline():
    document = "alpha\nbeta\ngamma"
    patches = _eof(document, [_p(2, 2, replacement="BETA", intent="i")])
    assert apply_patches(document, [p.model_dump() for p in patches]) == "alpha\nBETA\ngamma"


def test_integration_interior_edit_preserves_trailing_newline():
    document = "alpha\nbeta\ngamma\n"
    patches = _eof(document, [_p(2, 2, replacement="BETA", intent="i")])
    assert apply_patches(document, [p.model_dump() for p in patches]) == "alpha\nBETA\ngamma\n"


def test_integration_insert_before_first_line():
    document = "alpha\nbeta"
    # Insert before line 1 = (start=1, end=0).
    patches = _eof(document, [_p(1, 0, replacement="zero", intent="i")])
    assert apply_patches(document, [p.model_dump() for p in patches]) == "zero\nalpha\nbeta"


def test_integration_replacement_already_has_trailing_newline_not_doubled():
    document = "alpha"
    patches = _eof(document, [_p(2, 1, replacement="more\n", intent="i")])
    assert apply_patches(document, [p.model_dump() for p in patches]) == "alpha\nmore\n"


def test_integration_empty_document_with_append():
    document = ""
    # Empty-doc generation = (start=1, end=0).
    patches = _eof(document, [_p(1, 0, replacement="hello", intent="i")])
    assert apply_patches(document, [p.model_dump() for p in patches]) == "hello\n"


# ----- System prompt ----------------------------------------------------------

def test_system_prompt_mentions_replace_lines():
    assert "replace_lines" in EDIT_DOCUMENT_V4_SYSTEM_PROMPT


def test_system_prompt_mentions_eof_marker():
    assert "EOF" in EDIT_DOCUMENT_V4_SYSTEM_PROMPT


def test_system_prompt_documents_status_and_comment():
    assert "status" in EDIT_DOCUMENT_V4_SYSTEM_PROMPT
    assert '"done"' in EDIT_DOCUMENT_V4_SYSTEM_PROMPT
    assert '"partial"' in EDIT_DOCUMENT_V4_SYSTEM_PROMPT
    assert '"unclear"' in EDIT_DOCUMENT_V4_SYSTEM_PROMPT
    assert "comment" in EDIT_DOCUMENT_V4_SYSTEM_PROMPT


# ----- handle() / agent wiring ------------------------------------------------

import db
from agents.config import EDIT_DOCUMENT_V4_UUID


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
    agent = EditDocumentAgentV4(
        agent_uuid=EDIT_DOCUMENT_V4_UUID, name="edit_document_v4", send=lambda _: None
    )
    agent.model_group_uuid = None
    agent.candidate_model_uuids = []
    monkeypatch.setattr(
        agent, "_structured_call",
        lambda _user_prompt, validator=None: response_plan,
    )
    return agent


def test_handle_returns_status_comment_and_patches(app_ctx, monkeypatch):
    plan = EditPlan(
        patches=[ReplaceLinesPatch(
            start_line=1, end_line=1, replacement="DONE", intent="mark done",
        )],
        status="done",
        comment="Replaced line 1.",
    )
    agent = _stub_agent(app_ctx, monkeypatch, plan)
    result = agent.handle(
        journal_id=0,
        payload={"document": "TODO\nprint('x')", "instructions": "mark TODO as done"},
    )
    # Interior edit, original had no trailing newline -> no mutation.
    assert result == {
        "ok": True,
        "status": "done",
        "comment": "Replaced line 1.",
        "patches": [
            {"start_line": 1, "end_line": 1,
             "replacement": "DONE", "intent": "mark done"}
        ],
    }


def test_handle_replace_final_line_no_trailing_newline_adds_one(app_ctx, monkeypatch):
    plan = EditPlan(
        patches=[ReplaceLinesPatch(
            start_line=1, end_line=1, replacement="beta", intent="replace",
        )],
        status="done",
        comment="ok",
    )
    agent = _stub_agent(app_ctx, monkeypatch, plan)
    result = agent.handle(
        journal_id=0,
        payload={"document": "alpha", "instructions": "x"},
    )
    assert result["patches"] == [
        {"start_line": 1, "end_line": 1,
         "replacement": "beta\n", "intent": "replace"}
    ]


def test_handle_replace_final_line_with_trailing_newline_no_mutation(app_ctx, monkeypatch):
    plan = EditPlan(
        patches=[ReplaceLinesPatch(
            start_line=1, end_line=1, replacement="beta", intent="replace",
        )],
        status="done",
        comment="ok",
    )
    agent = _stub_agent(app_ctx, monkeypatch, plan)
    result = agent.handle(
        journal_id=0,
        payload={"document": "alpha\n", "instructions": "x"},
    )
    assert result["patches"][0]["replacement"] == "beta"


def test_handle_append_text_to_no_newline_file(app_ctx, monkeypatch):
    # 1-line doc, append: (start=2, end=1). EOF policy adds "\n".
    plan = EditPlan(
        patches=[ReplaceLinesPatch(
            start_line=2, end_line=1, replacement="more", intent="add",
        )],
        status="done",
        comment="ok",
    )
    agent = _stub_agent(app_ctx, monkeypatch, plan)
    result = agent.handle(
        journal_id=0,
        payload={"document": "alpha", "instructions": "x"},
    )
    assert result["patches"] == [
        {"start_line": 2, "end_line": 1,
         "replacement": "more\n", "intent": "add"}
    ]


def test_handle_uses_logical_line_count_for_validation(app_ctx, monkeypatch):
    # "alpha\n" has logical_line_count=1, so a replace targeting line 2
    # is out of range. (Raw split sees 2 entries; v4 sees 1.)
    plan = EditPlan(
        patches=[ReplaceLinesPatch(
            start_line=2, end_line=2, replacement="X", intent="i",
        )],
        status="done",
        comment="ok",
    )
    bad_agent = EditDocumentAgentV4(
        agent_uuid=EDIT_DOCUMENT_V4_UUID, name="edit_document_v4", send=lambda _: None
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
                f"agent edit_document_v4: all models failed; last error: {e}"
            ) from e

    monkeypatch.setattr(bad_agent, "_structured_call", stub)
    with pytest.raises(RuntimeError, match="start_line"):
        bad_agent.handle(
            journal_id=0,
            payload={"document": "alpha\n", "instructions": "x"},
        )


def test_handle_raises_on_missing_document(app_ctx, monkeypatch):
    plan = EditPlan(patches=[], status="done", comment="noop")
    agent = _stub_agent(app_ctx, monkeypatch, plan)
    with pytest.raises(ValueError, match="document"):
        agent.handle(journal_id=0, payload={"instructions": "x"})


def test_handle_raises_on_missing_instructions(app_ctx, monkeypatch):
    plan = EditPlan(patches=[], status="done", comment="noop")
    agent = _stub_agent(app_ctx, monkeypatch, plan)
    with pytest.raises(ValueError, match="instructions"):
        agent.handle(journal_id=0, payload={"document": "x"})


def test_handle_passes_validator_to_structured_call(app_ctx, monkeypatch):
    captured: dict[str, object] = {}
    good_plan = EditPlan(
        patches=[ReplaceLinesPatch(
            start_line=1, end_line=1, replacement="X", intent="i",
        )],
        status="done",
        comment="ok",
    )

    def stub(user_prompt, validator=None):
        captured["validator"] = validator
        return good_plan

    agent = EditDocumentAgentV4(
        agent_uuid=EDIT_DOCUMENT_V4_UUID, name="edit_document_v4", send=lambda _: None
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


def test_user_prompt_includes_logical_line_count_and_eof_marker(app_ctx, monkeypatch):
    plan = EditPlan(patches=[], status="done", comment="noop")
    agent = _stub_agent(app_ctx, monkeypatch, plan)
    prompt = agent.user_prompt(
        {"document": "alpha\n", "instructions": "change alpha to beta"}
    )
    # "alpha\n" must show as 1 logical line (v3 would have shown 2).
    assert "Document (1 lines)" in prompt
    assert "   1: alpha" in prompt
    assert "EOF is after line 1." in prompt
    assert "change alpha to beta" in prompt
