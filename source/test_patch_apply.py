"""Tests for patch_apply.apply_patches.

Pure-function tests; no DB / LM Studio.

    python -m pytest test_patch_apply.py -v
"""

from patch_apply import apply_patches


def _p(start, end, replacement, intent="i"):
    """Helper: build a patch dict in the schema validate_patches accepts."""
    return {
        "op": "replace_lines",
        "start_line": start,
        "end_line": end,
        "replacement": replacement,
        "intent": intent,
    }


def test_apply_no_patches_returns_document_unchanged():
    doc = "a\nb\nc"
    assert apply_patches(doc, []) == doc


def test_apply_replace_single_line():
    assert apply_patches("a\nb\nc", [_p(2, 2, "B")]) == "a\nB\nc"


def test_apply_replace_range():
    assert apply_patches(
        "a\nb\nc\nd\ne",
        [_p(2, 4, "X")],
    ) == "a\nX\ne"


def test_apply_replace_with_multi_line_replacement():
    assert apply_patches(
        "a\nb\nc",
        [_p(2, 2, "X\nY")],
    ) == "a\nX\nY\nc"


def test_apply_delete_single_line():
    assert apply_patches("a\nb\nc", [_p(2, 2, "")]) == "a\nc"


def test_apply_delete_range():
    assert apply_patches(
        "a\nb\nc\nd\ne",
        [_p(2, 4, "")],
    ) == "a\ne"


def test_apply_insert_mid_document_pure_insert_encoding():
    # Insert "X" before line 2 in a 3-line doc: start_line=2, end_line=1.
    assert apply_patches("a\nb\nc", [_p(2, 1, "X")]) == "a\nX\nb\nc"


def test_apply_insert_before_line_one():
    # start_line=1, end_line=0, replacement="X" -> prepend.
    assert apply_patches("a\nb", [_p(1, 0, "X")]) == "X\na\nb"


def test_apply_append_after_last_line():
    # 3-line doc; start_line=4, end_line=3, replacement="X" -> append.
    assert apply_patches("a\nb\nc", [_p(4, 3, "X")]) == "a\nb\nc\nX"


def test_apply_multi_patch_applies_bottom_to_top():
    # Two patches against the same doc. The runner must apply them in
    # bottom-to-top order so the earlier patch's line numbers stay valid.
    # Replace line 2 -> "B"; delete line 4. Result: "a\nB\nc\ne".
    out = apply_patches(
        "a\nb\nc\nd\ne",
        [_p(2, 2, "B"), _p(4, 4, "")],
    )
    assert out == "a\nB\nc\ne"


def test_apply_multi_patch_input_order_irrelevant():
    # Same patches, reversed input order, same result.
    out = apply_patches(
        "a\nb\nc\nd\ne",
        [_p(4, 4, ""), _p(2, 2, "B")],
    )
    assert out == "a\nB\nc\ne"


def test_apply_generate_from_empty_document():
    # Empty doc, one insertion at start_line=1, end_line=0 with multi-line
    # replacement (the "write content from scratch" pattern from v2's
    # empty-document handling).
    out = apply_patches(
        "",
        [_p(1, 0, 'print("hello")\nprint("world")')],
    )
    assert out == 'print("hello")\nprint("world")'


def test_apply_preserves_trailing_blank_line_when_replacing_other_line():
    # Document = "a\n" → split into ["a", ""]. Replace line 1 with "X":
    # ["X", ""] → "X\n".
    assert apply_patches("a\n", [_p(1, 1, "X")]) == "X\n"


def test_apply_insert_blank_line_mid_document():
    # Pure-insert position with empty replacement = insert one blank line
    # before line 2 (start_line=2, end_line=1, replacement="").
    assert apply_patches("a\nb\nc", [_p(2, 1, "")]) == "a\n\nb\nc"


def test_apply_insert_blank_line_before_line_one():
    assert apply_patches("a\nb", [_p(1, 0, "")]) == "\na\nb"


def test_apply_append_blank_line():
    # Motivating case: 5-line doc, append-position with empty replacement
    # appends one blank line (start_line=6, end_line=5, replacement="").
    # "comment\n\n\n\n" splits into ["comment","","","",""] (5 lines).
    # Append blank → ["comment","","","","",""] joined "comment\n\n\n\n\n".
    assert apply_patches(
        "comment\n\n\n\n",
        [_p(6, 5, "")],
    ) == "comment\n\n\n\n\n"
