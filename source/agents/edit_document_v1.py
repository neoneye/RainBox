"""EditDocumentAgentV1 — a StructuredLLMAgent that returns line-range patches.

The agent receives {document, instructions} in its inbox payload, asks the
LLM for a list of non-overlapping `replace_lines` patches that realize the
instruction, validates the patches against the document's line count, and
returns them in the journal result. The agent does NOT apply the patches —
application is the caller's concern.

See docs/superpowers/specs/2026-05-29-edit-document-agent-design.md for the
design rationale.
"""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from agents.base import StatusSender, StructuredLLMAgent


EDIT_DOCUMENT_SYSTEM_PROMPT: str = """\
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
  The `replacement` must be NON-EMPTY here; an empty replacement combined with
  `end_line < start_line` is a no-op and is forbidden.
- To APPEND after the last line of an N-line document:
  `start_line=N+1`, `end_line=N`, `replacement="<new text>"` (also non-empty).
- Patches must not overlap (no two patches may share a line).
- Always include `intent` — one short sentence per patch.

Output exactly one JSON object: {"patches": [ ... ]}. No prose, no markdown
fences.

Example 1 — replace.
Document:
   1: # TODO: write greeting
   2: print("hello")

Instruction: mark the TODO as done.

Output:
{"patches":[{"op":"replace_lines","start_line":1,"end_line":1,"replacement":"# DONE: write greeting","intent":"Mark TODO complete."}]}

Example 2 — delete.
Document:
   1: alpha
   2: beta
   3: beta
   4: gamma

Instruction: remove the duplicate beta on line 3.

Output:
{"patches":[{"op":"replace_lines","start_line":3,"end_line":3,"replacement":"","intent":"Remove duplicate beta."}]}
"""


class Patch(BaseModel):
    op: Literal["replace_lines"]
    start_line: int = Field(ge=1, description="1-based inclusive line number.")
    end_line: int = Field(
        ge=0,
        description=(
            "1-based inclusive end line. Equal to start_line for single-line "
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


class EditPlan(BaseModel):
    patches: list[Patch] = Field(
        description=(
            "List of non-overlapping replace_lines patches that, applied "
            "bottom-to-top, realize the requested change."
        )
    )


def validate_patches(patches: list[Patch], document_line_count: int) -> None:
    """Raise ValueError on the first invalid patch or overlap.

    Rules:
      - 1 <= p.start_line <= document_line_count + 1
      - p.start_line - 1 <= p.end_line <= document_line_count
      - The pure-insert encoding (end_line == start_line - 1, including
        end_line == 0 for insert-before-line-1) requires a non-empty
        replacement; an empty replacement with end_line < start_line is a
        no-op and is rejected. Deletions use end_line >= start_line.
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
        if p.end_line < p.start_line and p.replacement == "":
            raise ValueError(
                f"patch {i}: end_line {p.end_line} < start_line {p.start_line} "
                f"with empty replacement is a no-op; use end_line >= start_line "
                f"for a deletion, or supply non-empty replacement for an insertion"
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


class EditDocumentAgentV1(StructuredLLMAgent):
    """Plans line-range patches for a document given a free-form instruction.

    Inbox payload shape:
        {"document": "<full text>", "instructions": "<what to change>"}

    Journal result shape:
        {"ok": True, "patches": [<Patch.model_dump()>, ...]}

    Does not apply the patches. See module docstring and design spec.
    """

    def __init__(self, agent_uuid: UUID, name: str, send: StatusSender) -> None:
        super().__init__(
            agent_uuid,
            name,
            send,
            system_prompt=EDIT_DOCUMENT_SYSTEM_PROMPT,
            response_model=EditPlan,
        )

    def _extract_payload(self, payload: dict[str, Any]) -> tuple[str, str]:
        document = payload.get("document")
        instructions = payload.get("instructions")
        if not isinstance(document, str) or not document.strip():
            raise ValueError(
                "edit_document payload missing or blank 'document'"
            )
        if not isinstance(instructions, str) or not instructions.strip():
            raise ValueError(
                "edit_document payload missing or blank 'instructions'"
            )
        return document, instructions

    def user_prompt(self, payload: dict[str, Any]) -> str:
        document, instructions = self._extract_payload(payload)
        rendered = render_document_with_line_numbers(document)
        return (
            f"Document:\n{rendered}\n\nWhat to change:\n{instructions}"
        )

    def handle(self, journal_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        document, _instructions = self._extract_payload(payload)
        line_count = 0 if document == "" else len(document.split("\n"))

        def _validate(plan: BaseModel) -> None:
            # Runs inside _structured_call's per-model try-block: if the
            # patches are out of range or overlap, raising here causes the
            # fallback loop to try the next model in the bound group.
            assert isinstance(plan, EditPlan)
            validate_patches(plan.patches, document_line_count=line_count)

        plan = self._structured_call(self.user_prompt(payload), validator=_validate)
        assert isinstance(plan, EditPlan)
        return {
            "ok": True,
            "patches": [p.model_dump() for p in plan.patches],
        }
