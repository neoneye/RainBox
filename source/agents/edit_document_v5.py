"""EditDocumentAgentV5 — replace_lines patch planner.

Given a document and an instruction, emit a list of non-overlapping
``replace_lines`` patches that, applied bottom-to-top by
``patch_apply.apply_patches``, realize the change. This agent does not
apply the patches itself.

Two behavioral details worth knowing about as a reader of this module:

Logical-line view
-----------------

A single trailing newline at the end of the document is folded into the
EOF marker rather than displayed as a separate blank line. So
``"alpha"`` and ``"alpha\\n"`` both render as one logical line. Extra
trailing newlines remain as explicit blank lines.

EOF normalization
-----------------

After the LLM emits patches, an EOF-policy pass mutates non-empty
replacements that touch EOF when the original document lacked a
trailing newline, appending ``"\\n"`` so the applier emits one.
Interior edits, deletions, and blank-line inserts are left alone. The
applier needs no change — the mutation is baked into the patches.

Possible future direction
-------------------------

The current rule is fine for code files but loses control for
byte-sensitive formats (snapshots, ``.env`` files, golden-file tests).
An explicit ``{"op": "set_trailing_newline", "enabled": false}`` op
could compile to a zero-or-one trailing-``"\\n"`` adjustment. Not
implemented — kept behind the implicit rule until a benchmark demands
it.
"""

from typing import Annotated, Any, Literal, Union
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from agents.base import StatusSender, StructuredLLMAgent


EDIT_DOCUMENT_V5_SYSTEM_PROMPT: str = """\
You are a document-editing planner. You receive a document with
line-number prefixes and an instruction describing a change. 
Produce a list of patches that, applied bottom-to-top, realize the change.

Each patch has an "op" field selecting one of two operations:

1. replace — replace or delete one or more EXISTING lines
   Required fields: op="replace", start_line, end_line, replacement, intent
   Constraint: end_line MUST be >= start_line.
   - To REPLACE line N:           start_line=N, end_line=N, replacement="<new text>"
   - To REPLACE lines N..M:       start_line=N, end_line=M, replacement="<new text>"   (M >= N)
   - To DELETE line N:            start_line=N, end_line=N, replacement=""
   - To DELETE lines N..M:        start_line=N, end_line=M, replacement=""             (M >= N)
   The replacement may span multiple lines (use literal \\n between them).

2. insert — add new text without removing anything
   Required fields: op="insert", before_line (>=1), text, intent
   `before_line` names the 1-based line the new text should appear in front of.
   Use this op for EVERY add-without-remove case:
   - To INSERT before line N:                                before_line=N,   text="<new text>"
   - To INSERT a BLANK line before line N:                   before_line=N,   text=""
   - To APPEND after the last line of an N-line document:    before_line=N+1, text="<new text>"
   - To APPEND a BLANK line at the end of an N-line doc:     before_line=N+1, text=""
   - To generate content for an EMPTY document (0 lines):    before_line=1,   text="<entire new content>"
   The text may span multiple lines (use literal \\n between them).

Rules:
- Patches must not overlap (no two patches share a line; no two
  inserts at the same line; an insert at line N may not coexist with
  a replace covering line N).
- Always include `intent` — one short sentence per patch.
- The document is shown in "logical lines": a trailing newline at the
  end of the file is not displayed as a separate blank line. You do not
  need to manage trailing newlines — they are normalized for you.

Output exactly one JSON object: {"patches": [...],"status": ..., "comment": ...}.
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
{"patches":[{"op":"replace","start_line":1,"end_line":1,"replacement":"# DONE: write greeting","intent":"Mark TODO complete."}],"status":"done","comment":"Marked the TODO on line 1 as DONE."}

Example 2 — delete, done.
Document (4 lines):
   1: alpha
   2: beta
   3: beta
   4: gamma
EOF is after line 4.

Instruction: remove the duplicate beta on line 3.

Output:
{"patches":[{"op":"replace","start_line":3,"end_line":3,"replacement":"","intent":"Remove duplicate beta."}],"status":"done","comment":"Deleted the duplicate beta on line 3."}

Example 3 — append, done.
Document (3 lines):
   1: apples
   2: bananas
   3: cherries
EOF is after line 3.

Instruction: append 'dates' to the list.

Output:
{"patches":[{"op":"insert","before_line":4,"text":"dates","intent":"Append 'dates' after line 3."}],"status":"done","comment":"Appended 'dates' after the last line."}

Example 4 — insert mid-document, done.
Document (3 lines):
   1: apples
   2: cherries
   3: dates
EOF is after line 3.

Instruction: add 'bananas' between apples and cherries.

Output:
{"patches":[{"op":"insert","before_line":2,"text":"bananas","intent":"Insert 'bananas' before line 2."}],"status":"done","comment":"Inserted 'bananas' before cherries."}
"""


# ----- Patch schema -----------------------------------------------------------

class ReplacePatch(BaseModel):
    op: Literal["replace"]
    start_line: int = Field(description="range start inclusive.")
    end_line: int = Field(description="range end inclusive.")
    replacement: str = Field(
        description=(
            'New text for lines start_line..end_line. Empty string '
            'deletes the range. May contain \\n for multi-line.'
        )
    )
    intent: str = Field(description="One short sentence describing the patch.")

    @model_validator(mode="after")
    def _end_line_at_or_after_start(self) -> "ReplacePatch":
        if self.end_line < self.start_line:
            raise ValueError(
                f"end_line ({self.end_line}) must be >= start_line "
                f"({self.start_line}); use the 'insert' op to add new "
                f"lines without replacing existing ones"
            )
        return self


class InsertPatch(BaseModel):
    op: Literal["insert"]
    before_line: int = Field(
        description=(
            "Insert the text before this 1-based line number. Use "
            "before_line=N+1 (where N is the document's logical line "
            "count) to append after the last line. For an empty "
            "document, use before_line=1."
        ),
    )
    text: str = Field(
        description=(
            'Text to insert. May contain \\n for multi-line. Empty '
            'string inserts a single blank line.'
        )
    )
    intent: str = Field(description="One short sentence describing the patch.")


Patch = Annotated[
    Union[ReplacePatch, InsertPatch],
    Field(discriminator="op"),
]


class EditPlan(BaseModel):
    patches: list[Patch] = Field(
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


# ----- Internal canonical form ------------------------------------------------

class NormalizedPatch(BaseModel):
    """Canonical patch form used by validate_patches, apply_eof_policy,
    and the journal output. ReplacePatch and InsertPatch both compile to
    this shape via normalize_patch."""
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=0)  # 0 for insert-before-line-1
    replacement: str
    intent: str


def normalize_patch(patch: Patch) -> NormalizedPatch:
    """Compile one LLM-facing patch op down to the canonical form.

      - ReplacePatch(S, E, R)        -> (start_line=S, end_line=E, replacement=R)
      - InsertPatch(before_line=L, text=T) -> (start_line=L, end_line=L-1, replacement=T)

    ``intent`` is copied through unchanged.
    """
    if isinstance(patch, ReplacePatch):
        return NormalizedPatch(
            start_line=patch.start_line,
            end_line=patch.end_line,
            replacement=patch.replacement,
            intent=patch.intent,
        )
    if isinstance(patch, InsertPatch):
        return NormalizedPatch(
            start_line=patch.before_line,
            end_line=patch.before_line - 1,
            replacement=patch.text,
            intent=patch.intent,
        )


# ----- Logical-line helpers ---------------------------------------------------

def logical_lines(document: str) -> list[str]:
    """Return the document as a list of logical lines.

    A single trailing newline is stripped before splitting so that
    ``"alpha"`` and ``"alpha\\n"`` both yield ``["alpha"]``. Additional
    trailing newlines remain as explicit blank lines.
    """
    if document == "":
        return []
    body = document[:-1] if document.endswith("\n") else document
    return body.split("\n")


def logical_line_count(document: str) -> int:
    return len(logical_lines(document))


def apply_eof_policy(
    patches: list[NormalizedPatch],
    document_line_count: int,
    original_had_trailing_newline: bool,
) -> list[NormalizedPatch]:
    """Mutate patches in place so that EOF-touching edits produce a
    trailing newline when the original document lacked one.

    The applier (``patch_apply.apply_patches``) joins the post-edit line
    list with ``"\\n"``. A trailing ``""`` in the line list becomes the
    final ``"\\n"``. So appending ``"\\n"`` to a non-empty replacement
    that lands at EOF makes ``apply_patches`` emit the trailing newline
    without any code change on the applier side.

    Rules (single condition):
      - Patch touches EOF (``end_line >= document_line_count``).
      - Replacement is non-empty (empty deletions and blank-line inserts
        already produce the right thing).
      - Replacement does not already end with ``"\\n"``.
      - Original document lacked a trailing newline.

    When the original already had a trailing newline, the raw-split line
    list ends with ``""`` and ``apply_patches`` naturally preserves that
    final ``"\\n"`` for any patch that doesn't excise it. Nothing to do.
    """
    if original_had_trailing_newline:
        return patches
    n = document_line_count
    for p in patches:
        if (
            p.end_line >= n
            and p.replacement != ""
            and not p.replacement.endswith("\n")
        ):
            p.replacement = p.replacement + "\n"
    return patches


def validate_patches(patches: list[NormalizedPatch], document_line_count: int) -> None:
    """Raise ValueError on the first invalid patch or overlap.
    ``document_line_count`` is the logical line count.

    Rules:
      - ``1 <= start_line <= document_line_count + 1``
      - ``start_line - 1 <= end_line <= document_line_count``
      - Empty replacement: deletes the range when ``end_line >=
        start_line``; inserts a single blank line when ``end_line <
        start_line`` (the pure-insert/append position).
      - Patches must not share any line. Two pure-inserts at the same
        ``start_line`` target the same insertion point and also count
        as an overlap.
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
    indexed = sorted(enumerate(patches), key=lambda ip: ip[1].start_line)
    for (prev_idx, prev), (curr_idx, curr) in zip(indexed, indexed[1:]):
        if curr.start_line <= prev.end_line:
            raise ValueError(
                f"patch {curr_idx}: overlaps patch {prev_idx} "
                f"(prev range {prev.start_line}-{prev.end_line}, "
                f"curr range {curr.start_line}-{curr.end_line})"
            )
        if (prev.end_line == prev.start_line - 1 and
            curr.start_line == prev.start_line):
            raise ValueError(
                f"patch {curr_idx}: overlaps patch {prev_idx} "
                f"(prev range {prev.start_line}-{prev.end_line}, "
                f"curr range {curr.start_line}-{curr.end_line})"
            )


def render_document_with_line_numbers(document: str) -> str:
    """Return the document with 1-based logical-line prefixes and an EOF
    marker. A single trailing newline is invisible (folded into the
    final line's terminator); additional trailing newlines remain as
    explicit blank lines.

    Empty document renders as just "EOF is after line 0." with no
    numbered lines.
    """
    lines = logical_lines(document)
    if not lines:
        return "EOF is after line 0."
    numbered = "\n".join(f"{i + 1:>4}: {line}" for i, line in enumerate(lines))
    return f"{numbered}\nEOF is after line {len(lines)}."


class EditDocumentAgentV5(StructuredLLMAgent):
    """Replace_lines patch planner with logical-line view and EOF
    normalization.

    Inbox payload shape:
        {"document": "<full text>", "instructions": "<what to change>"}

    Journal result shape:
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
            system_prompt=EDIT_DOCUMENT_V5_SYSTEM_PROMPT,
            response_model=EditPlan,
        )

    def _extract_payload(self, payload: dict[str, Any]) -> tuple[str, str]:
        document = payload.get("document")
        instructions = payload.get("instructions")
        if not isinstance(document, str):
            raise ValueError(
                "edit_document_v5 payload missing 'document' (use \"\" to "
                "generate content from scratch)"
            )
        if not isinstance(instructions, str) or not instructions.strip():
            raise ValueError(
                "edit_document_v5 payload missing or blank 'instructions'"
            )
        return instructions, document

    def user_prompt(self, payload: dict[str, Any]) -> str:
        instructions, document = self._extract_payload(payload)
        rendered = render_document_with_line_numbers(document)
        line_count = logical_line_count(document)
        return (
            f"What to change:\n{instructions}\n\n"
            f"Document ({line_count} lines):\n{rendered}"
        )

    def handle(self, journal_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        _instructions, document = self._extract_payload(payload)
        line_count = logical_line_count(document)
        had_trailing_newline = document.endswith("\n")

        def _validate(plan: BaseModel) -> None:
            assert isinstance(plan, EditPlan)
            normalized = [normalize_patch(p) for p in plan.patches]
            validate_patches(normalized, document_line_count=line_count)

        plan = self._structured_call(self.user_prompt(payload), validator=_validate)
        assert isinstance(plan, EditPlan)
        normalized = [normalize_patch(p) for p in plan.patches]
        apply_eof_policy(
            normalized,
            document_line_count=line_count,
            original_had_trailing_newline=had_trailing_newline,
        )
        return {
            "ok": True,
            "status": plan.status,
            "comment": plan.comment,
            "patches": [p.model_dump() for p in normalized],
        }
