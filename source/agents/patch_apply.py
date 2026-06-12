"""Apply a list of replace_lines patches to a document.

Mirror of the encoding `validate_patches` (in agent_edit_document_v1.py and
its v2 sibling) accepts. The two responsibilities are deliberately split:
agents return patch lists; this module turns them into a post-edit
document. Used by the benchmark page and available for any future caller
that wants to materialize the edit.

Encoding rules (must match validate_patches):
  - REPLACE lines N..M with text (M >= N): replacement is the new text.
  - DELETE  lines N..M (M >= N): replacement is "".
  - INSERT  before line N (N >= 1): start_line=N, end_line=N-1.
  - APPEND  after line N (N == document_line_count): start_line=N+1,
            end_line=N.
  - Pure inserts must supply non-empty replacement (validate_patches
    rejects an empty replacement with end_line < start_line as a no-op).

Patches are applied bottom-to-top by start_line so earlier patches' line
numbers stay valid. The patch list is taken as-is (typically already
validated by validate_patches before this function is called).
"""

from typing import Any


def apply_patches(document: str, patches: list[dict[str, Any]]) -> str:
    """Return the document with all patches applied. See module docstring."""
    if not patches:
        return document

    # Render the document as a list of lines. Match render_document_with_
    # line_numbers' empty-document convention: empty input has zero lines,
    # not one empty line.
    lines: list[str] = [] if document == "" else document.split("\n")

    # Bottom-to-top: largest start_line first, so earlier-line patches still
    # address the correct positions after later-line patches have changed
    # the list length.
    ordered = sorted(patches, key=lambda p: p["start_line"], reverse=True)

    for p in ordered:
        start = p["start_line"]
        end = p["end_line"]
        replacement = p["replacement"]
        if replacement == "":
            # end >= start: delete the [start, end] range.
            # end <  start: pure-insert position with empty text — insert
            #               one blank line. This is the only way to express
            #               "append/insert a blank line" with the encoding.
            new_lines: list[str] = [""] if end < start else []
        else:
            # Non-empty replacement is spliced in, possibly producing
            # multiple lines (the model is allowed to use \n inside).
            new_lines = replacement.split("\n")
        lines[start - 1 : end] = new_lines

    return "\n".join(lines)
