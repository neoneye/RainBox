"""EditDocumentAgentV2 — patch planner that also emits status + comment.

Sibling of EditDocumentAgentV1. Receives the same {document,
instructions} payload, runs the same replace_lines patch planner, and
adds two fields to the response — a Literal status ("done"/"partial"/
"unclear") and a required non-empty comment — so an orchestrator can
branch on outcome without re-parsing.

Fully self-contained: Patch, validate_patches, and the line-number
renderer are duplicated here rather than imported from v1, following the
shell_v1/shell_v2 precedent.

See docs/superpowers/specs/2026-05-30-edit-document-agent-v2-design.md.
"""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from agents.base import StatusSender, StructuredLLMAgent


EDIT_DOCUMENT_V2_SYSTEM_PROMPT: str = """\
You are a document-editing planner. You receive a document with line-number
prefixes and an instruction describing a change. Produce a list of
`replace_lines` patches that, applied bottom-to-top, realize the change.

Rules:
- `start_line` and `end_line` are 1-based, inclusive, and refer to the line
  numbers shown in the document.
- To REPLACE one line N: `start_line=N`, `end_line=N`, `replacement="<new text>"`.
- To REPLACE lines N..M (M >= N): `start_line=N`, `end_line=M`, `replacement="<new text>"`.
- To DELETE line N: `start_line=N`, `end_line=N`, `replacement=""`.
  To DELETE lines N..M (M >= N): `start_line=N`, `end_line=M`, `replacement=""`.
  In every deletion, `end_line >= start_line`. NEVER use `end_line < start_line`
  for a deletion — that encoding is reserved for pure insertion (see below).
- To INSERT new text before line N (N >= 1), without removing any line:
  `start_line=N`, `end_line=N-1`, `replacement="<new text>"`.
- To INSERT a BLANK line before line N: same encoding with `replacement=""`
  — i.e. `start_line=N`, `end_line=N-1`, `replacement=""`. (Empty
  replacement at a pure-insert position inserts one blank line.)
- To APPEND after the last line of an N-line document:
  `start_line=N+1`, `end_line=N`, `replacement="<new text>"`.
- To APPEND a BLANK line at the end: `start_line=N+1`, `end_line=N`,
  `replacement=""`.
- For an EMPTY document (no lines shown), generate content from scratch with
  a single insertion: `start_line=1`, `end_line=0`,
  `replacement="<entire new content>"`. The `replacement` may span multiple
  lines (use literal `\n` between them in the JSON string).
- Patches must not overlap (no two patches may share a line).
- Always include `intent` — one short sentence per patch.

Output exactly one JSON object: {"patches": [...],"status": ..., "comment": ...}.
No prose, no markdown fences.

Choose `status`:
- "done"     — your patches fully realize the instruction.
- "partial"  — your patches realize some but not all of the instruction
               (e.g., a referenced symbol wasn't in the document, or
               the instruction mentioned context outside this document).
- "unclear"  — the instruction is ambiguous, missing required context,
               or asks for something outside the document's scope.
               `patches` should usually be empty in this case.

`comment` is always required (non-empty). One sentence. Describe what
you did for "done", what you skipped and why for "partial", and what's
ambiguous and what would resolve it for "unclear".

Example 1 — replace, done.
Document (2 lines):
   1: # TODO: write greeting
   2: print("hello")

Instruction: mark the TODO as done.

Output:
{"patches":[{"op":"replace_lines","start_line":1,"end_line":1,"replacement":"# DONE: write greeting","intent":"Mark TODO complete."}],"status":"done","comment":"Marked the TODO on line 1 as DONE."}

Example 2 — delete, done.
Document (4 lines):
   1: alpha
   2: beta
   3: beta
   4: gamma

Instruction: remove the duplicate beta on line 3.

Output:
{"patches":[{"op":"replace_lines","start_line":3,"end_line":3,"replacement":"","intent":"Remove duplicate beta."}],"status":"done","comment":"Deleted the duplicate beta on line 3."}

Example 3 — append, done.
Document (3 lines):
   1: apples
   2: bananas
   3: cherries

Instruction: append 'dates' to the list.

Output:
{"patches":[{"op":"replace_lines","start_line":4,"end_line":3,"replacement":"dates","intent":"Append 'dates' after line 3."}],"status":"done","comment":"Appended 'dates' after the last line."}
"""


class Patch(BaseModel):
    op: Literal["replace_lines"]
    start_line: int = Field(ge=1, description="Inclusive line number.")
    end_line: int = Field(
        ge=0,
        description=(
            "Inclusive end line. Equal to start_line for single-line "
            "replace. Equal to start_line - 1 for a pure insert before "
            "start_line (0 when inserting before line 1)."
        ),
    )
    replacement: str = Field(
        description=(
            'New text for the range. Empty string for a deletion (which '
            'requires end_line >= start_line); insertions (end_line < '
            'start_line) must supply non-empty text.'
        )
    )
    intent: str = Field(description="One short sentence describing the patch.")


class EditPlanV2(BaseModel):
    patches: list[Patch] = Field(
        description=(
            "List of non-overlapping replace_lines patches that, applied "
            "bottom-to-top, realize the requested change. May be empty when "
            "status is 'unclear' or when no edits were needed for 'done'."
        )
    )
    status: Literal["done", "partial", "unclear"] = Field(
        description=(
            'Outcome classification for the orchestrator: '
            '"done" — instructions fully realized; '
            '"partial" — only some of the requested change applied; '
            '"unclear" — instructions ambiguous or out of scope, planner punted.'
        )
    )
    comment: str = Field(
        min_length=1,
        description=(
            "Always non-empty. For 'done', a one-line summary of what was "
            "changed. For 'partial', name what was skipped and why. For "
            "'unclear', describe what was ambiguous and what extra "
            "information would resolve it."
        ),
    )


def validate_patches(patches: list[Patch], document_line_count: int) -> None:
    """Raise ValueError on the first invalid patch or overlap.

    Rules:
      - 1 <= p.start_line <= document_line_count + 1
      - p.start_line - 1 <= p.end_line <= document_line_count
      - Empty replacement: deletes the range when end_line >= start_line;
        inserts a single blank line when end_line < start_line (the pure-
        insert/append position). Both are valid.
      - Sorted by start_line, no two patches share a line:
        prev.end_line < next.start_line.
    """
    n = document_line_count
    for i, p in enumerate(patches):
        if not (1 <= p.start_line <= n + 1):
            raise ValueError(
                f"patch {i}: start_line {p.start_line} out of range "
                f"[1, {n + 1}] for {n}-line document"
            )
        if not (p.start_line - 1 <= p.end_line <= n):
            raise ValueError(
                f"patch {i}: end_line {p.end_line} out of range "
                f"[{p.start_line - 1}, {n}] for start_line={p.start_line}"
            )
    # Overlap check: sort by start_line, then compare adjacent pairs. Carry
    # the patch's original index so the error message points at the second
    # patch in the offending pair (the one that "moves into" the previous).
    indexed = sorted(enumerate(patches), key=lambda ip: ip[1].start_line)
    for (prev_idx, prev), (curr_idx, curr) in zip(indexed, indexed[1:]):
        # Check for range overlap: curr must start after prev ends.
        # For pure inserts, end_line < start_line, but the insertion still
        # occupies a logical point (before start_line). Two pure inserts at
        # the same start_line both target the same insertion point.
        if curr.start_line <= prev.end_line:
            raise ValueError(
                f"patch {curr_idx}: overlaps patch {prev_idx} "
                f"(prev range {prev.start_line}-{prev.end_line}, "
                f"curr range {curr.start_line}-{curr.end_line})"
            )
        # A pure-insert patch (end_line == start_line - 1) targets the
        # insertion point before start_line; any other patch at the same
        # start_line collides with it because the bottom-to-top apply order
        # is then ambiguous.
        if (prev.end_line == prev.start_line - 1 and
            curr.start_line == prev.start_line):
            raise ValueError(
                f"patch {curr_idx}: overlaps patch {prev_idx} "
                f"(prev range {prev.start_line}-{prev.end_line}, "
                f"curr range {curr.start_line}-{curr.end_line})"
            )


def render_document_with_line_numbers(document: str) -> str:
    """Return the document with 1-based line-number prefixes.

    Empty document renders as the empty string. A trailing newline produces
    a final blank line that's still numbered, so the LLM can target it.
    """
    if document == "":
        return ""
    lines = document.split("\n")
    return "\n".join(f"{i + 1:>4}: {line}" for i, line in enumerate(lines))


class EditDocumentAgentV2(StructuredLLMAgent):
    """Sibling of EditDocumentAgentV1 that also emits status + comment.

    Inbox payload shape:
        {"document": "<full text>", "instructions": "<what to change>"}

    Journal result shape:
        {
            "ok": True,
            "status": "done" | "partial" | "unclear",
            "comment": "<non-empty>",
            "patches": [<Patch.model_dump()>, ...],
        }

    Does not apply the patches. See module docstring and design spec.
    """

    def __init__(self, agent_uuid: UUID, name: str, send: StatusSender) -> None:
        super().__init__(
            agent_uuid,
            name,
            send,
            system_prompt=EDIT_DOCUMENT_V2_SYSTEM_PROMPT,
            response_model=EditPlanV2,
        )

    def _extract_payload(self, payload: dict[str, Any]) -> tuple[str, str]:
        document = payload.get("document")
        instructions = payload.get("instructions")
        if not isinstance(document, str):
            raise ValueError(
                "edit_document_v2 payload missing 'document' (use \"\" to "
                "generate content from scratch)"
            )
        if not isinstance(instructions, str) or not instructions.strip():
            raise ValueError(
                "edit_document_v2 payload missing or blank 'instructions'"
            )
        return instructions, document

    def user_prompt(self, payload: dict[str, Any]) -> str:
        instructions, document = self._extract_payload(payload)
        rendered = render_document_with_line_numbers(document)
        line_count = 0 if document == "" else len(document.split("\n"))
        return (
            f"What to change:\n{instructions}\n\n"
            f"Document ({line_count} lines):\n{rendered}"
        )

    def handle(self, journal_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        _instructions, document = self._extract_payload(payload)
        line_count = 0 if document == "" else len(document.split("\n"))

        def _validate(plan: BaseModel) -> None:
            # Runs inside _structured_call's per-model try-block: if the
            # patches are out of range or overlap, raising here causes the
            # fallback loop to try the next model in the bound group.
            assert isinstance(plan, EditPlanV2)
            validate_patches(plan.patches, document_line_count=line_count)

        plan = self._structured_call(self.user_prompt(payload), validator=_validate)
        assert isinstance(plan, EditPlanV2)
        return {
            "ok": True,
            "status": plan.status,
            "comment": plan.comment,
            "patches": [p.model_dump() for p in plan.patches],
        }
