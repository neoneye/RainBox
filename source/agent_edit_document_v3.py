"""EditDocumentAgentV3 — high-level patch ops + EOF marker.

Sibling of EditDocumentAgentV1 and EditDocumentAgentV2. Receives the
same {document, instructions} payload and produces the same {status,
comment, patches} journal result shape, but the LLM-facing patch
language differs:

  v1/v2 — one op (replace_lines) where appends and inserts use
          "inverted" line ranges (start_line > end_line). Weak models
          frequently misuse this.
  v3    — four high-level ops as a discriminated union:
          replace_lines, insert_before, append_text, append_newline.
          Each high-level op normalizes internally to the v2
          replace_lines canonical form so validate_patches and
          patch_apply.apply_patches are reused unchanged.

Fully self-contained per the shell_v1/shell_v2 precedent.

See docs/superpowers/specs/2026-05-30-edit-document-agent-v3-design.md.
"""

from typing import Annotated, Any, Literal, Union
from uuid import UUID

from pydantic import BaseModel, Field

from agent import StatusSender, StructuredLLMAgent


EDIT_DOCUMENT_V3_SYSTEM_PROMPT: str = """\
You are a document-editing planner. You receive a document with
line-number prefixes (and an "EOF is after line N." marker) and an
instruction describing a change. Produce a list of patches that,
applied bottom-to-top, realize the change.

Each patch has an "op" field selecting one of 4 operations:

1. replace_lines — replace one or more lines (or delete with empty replacement)
   Required fields: start_line (>=1), end_line (>= start_line), replacement, intent
   To replace line N:
     {"op":"replace_lines","start_line":N,"end_line":N,"replacement":"...","intent":"..."}
   To delete lines N..M:
     {"op":"replace_lines","start_line":N,"end_line":M,"replacement":"","intent":"..."}

2. insert_before — insert text before a line, without removing it
   Required fields: line (>=1), text, intent
   To insert before line N:
     {"op":"insert_before","line":N,"text":"...","intent":"..."}
   To insert a blank line before line N:
     {"op":"insert_before","line":N,"text":"","intent":"..."}

3. append_text — append text after EOF (after the last line of the document)
   Required fields: text, intent
   Example:
     {"op":"append_text","text":"new content","intent":"..."}
   Multi-line append: text may contain \\n.

4. append_newline — append exactly one blank line after EOF
   Required fields: intent (only)
   Example:
     {"op":"append_newline","intent":"..."}

Rules:
- Patches must not overlap (no two patches share a line).
- Always include `intent` (one short sentence per patch).
- The rendered document shows "EOF is after line N." so you don't have
  to count lines for appends.

Output exactly one JSON object: {"patches":[...],"status":...,"comment":...}.
No prose, no markdown fences.

Choose `status`:
- "done"     — your patches fully realize the instruction.
- "partial"  — your patches realize some but not all of the instruction.
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
EOF is after line 2.

Instruction: mark the TODO as done.

Output:
{"patches":[{"op":"replace_lines","start_line":1,"end_line":1,"replacement":"# DONE: write greeting","intent":"Mark TODO complete."}],"status":"done","comment":"Marked the TODO on line 1 as DONE."}

Example 2 — append text, done.
Document (3 lines):
   1: apples
   2: bananas
   3: cherries
EOF is after line 3.

Instruction: append 'dates' to the list.

Output:
{"patches":[{"op":"append_text","text":"dates","intent":"Append 'dates' after the last line."}],"status":"done","comment":"Appended 'dates' after the last line."}

Example 3 — append newline, done.
Document (4 lines):
   1: comment
   2:
   3:
   4:
EOF is after line 4.

Instruction: append a newline to the file.

Output:
{"patches":[{"op":"append_newline","intent":"Append one blank line at EOF."}],"status":"done","comment":"Appended one blank line at the end of the file."}
"""


# ----- LLM-facing patch ops (discriminated union) -----------------------------

class ReplaceLinesPatch(BaseModel):
    op: Literal["replace_lines"]
    start_line: int = Field(ge=1, description="1-based inclusive.")
    end_line: int = Field(
        ge=1,
        description="1-based inclusive end line. Must be >= start_line.",
    )
    replacement: str = Field(
        description=(
            'New text for lines start_line..end_line. Empty string '
            'deletes the range.'
        )
    )
    intent: str = Field(description="One short sentence describing the patch.")


class InsertBeforePatch(BaseModel):
    op: Literal["insert_before"]
    line: int = Field(ge=1, description="Insert before this 1-based line.")
    text: str = Field(
        description=(
            'Text to insert. May contain \\n for multi-line. Empty '
            'string inserts a single blank line.'
        )
    )
    intent: str = Field(description="One short sentence describing the patch.")


class AppendTextPatch(BaseModel):
    op: Literal["append_text"]
    text: str = Field(
        description='Text to append after the last line. May contain \\n.'
    )
    intent: str = Field(description="One short sentence describing the patch.")


class AppendNewlinePatch(BaseModel):
    op: Literal["append_newline"]
    intent: str = Field(description="One short sentence describing the patch.")


PatchV3 = Annotated[
    Union[ReplaceLinesPatch, InsertBeforePatch, AppendTextPatch, AppendNewlinePatch],
    Field(discriminator="op"),
]


class EditPlanV3(BaseModel):
    patches: list[PatchV3] = Field(
        description=(
            "List of non-overlapping patches applied bottom-to-top to "
            "realize the requested change. May be empty when status is "
            "'unclear' or for a 'done' no-op."
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
            "Always non-empty. One sentence describing what you did (or "
            "what you skipped and why, or what's ambiguous)."
        ),
    )


# ----- Internal canonical form (mirrors v2's Patch shape) ---------------------

class NormalizedPatch(BaseModel):
    """Canonical patch form used by validate_patches and
    patch_apply.apply_patches. Equivalent to v2's Patch."""
    op: Literal["replace_lines"]
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=0)  # 0 for insert-before-line-1
    replacement: str
    intent: str


def normalize_patch(patch: PatchV3, document_line_count: int) -> NormalizedPatch:
    """Compile one LLM-facing high-level patch op down to the canonical
    replace_lines form (the same shape v2 uses).

    The mapping:
      - ReplaceLinesPatch(S, E, R)        -> (start_line=S, end_line=E, replacement=R)
      - InsertBeforePatch(line=L, text=T) -> (start_line=L, end_line=L-1, replacement=T)
      - AppendTextPatch(text=T)           -> (start_line=n+1, end_line=n, replacement=T)
      - AppendNewlinePatch                -> (start_line=n+1, end_line=n, replacement="")

    `intent` is copied through unchanged in every case.
    """
    n = document_line_count
    if isinstance(patch, ReplaceLinesPatch):
        return NormalizedPatch(
            op="replace_lines",
            start_line=patch.start_line,
            end_line=patch.end_line,
            replacement=patch.replacement,
            intent=patch.intent,
        )
    if isinstance(patch, InsertBeforePatch):
        return NormalizedPatch(
            op="replace_lines",
            start_line=patch.line,
            end_line=patch.line - 1,
            replacement=patch.text,
            intent=patch.intent,
        )
    if isinstance(patch, AppendTextPatch):
        return NormalizedPatch(
            op="replace_lines",
            start_line=n + 1,
            end_line=n,
            replacement=patch.text,
            intent=patch.intent,
        )
    if isinstance(patch, AppendNewlinePatch):
        return NormalizedPatch(
            op="replace_lines",
            start_line=n + 1,
            end_line=n,
            replacement="",
            intent=patch.intent,
        )


def validate_patches(patches: list[NormalizedPatch], document_line_count: int) -> None:
    """Raise ValueError on the first invalid patch or overlap.

    Operates on the normalized form (after normalize_patch).

    Rules (identical to v2's validate_patches):
      - 1 <= p.start_line <= document_line_count + 1
      - p.start_line - 1 <= p.end_line <= document_line_count
      - Empty replacement: deletes the range when end_line >= start_line;
        inserts a single blank line when end_line < start_line.
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
    """Return the document with 1-based line-number prefixes and an EOF
    marker telling the model where the document ends. The EOF marker
    removes a class of "where do I append?" ambiguity for weak models.

    Empty document renders as just "EOF is after line 0." with no
    numbered lines.
    """
    if document == "":
        return "EOF is after line 0."
    lines = document.split("\n")
    numbered = "\n".join(f"{i + 1:>4}: {line}" for i, line in enumerate(lines))
    return f"{numbered}\nEOF is after line {len(lines)}."


class EditDocumentAgentV3(StructuredLLMAgent):
    """Third sibling of EditDocumentAgentV1. LLM emits one of four
    high-level patch ops; handle() normalizes them to the canonical
    replace_lines form before validation and returning.

    Inbox payload shape:
        {"document": "<full text>", "instructions": "<what to change>"}

    Journal result shape (same as v2):
        {
            "ok": True,
            "status": "done" | "partial" | "unclear",
            "comment": "<non-empty>",
            "patches": [<NormalizedPatch.model_dump()>, ...],
        }

    Does not apply the patches.
    """

    def __init__(self, agent_uuid: UUID, name: str, send: StatusSender) -> None:
        super().__init__(
            agent_uuid,
            name,
            send,
            system_prompt=EDIT_DOCUMENT_V3_SYSTEM_PROMPT,
            response_model=EditPlanV3,
        )

    def _extract_payload(self, payload: dict[str, Any]) -> tuple[str, str]:
        document = payload.get("document")
        instructions = payload.get("instructions")
        if not isinstance(document, str):
            raise ValueError(
                "edit_document_v3 payload missing 'document' (use \"\" to "
                "generate content from scratch)"
            )
        if not isinstance(instructions, str) or not instructions.strip():
            raise ValueError(
                "edit_document_v3 payload missing or blank 'instructions'"
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
            # patches (after normalization) are out of range or overlap,
            # raising here causes the fallback loop to try the next model.
            assert isinstance(plan, EditPlanV3)
            normalized = [normalize_patch(p, line_count) for p in plan.patches]
            validate_patches(normalized, document_line_count=line_count)

        plan = self._structured_call(self.user_prompt(payload), validator=_validate)
        assert isinstance(plan, EditPlanV3)
        normalized = [normalize_patch(p, line_count) for p in plan.patches]
        return {
            "ok": True,
            "status": plan.status,
            "comment": plan.comment,
            "patches": [p.model_dump() for p in normalized],
        }
