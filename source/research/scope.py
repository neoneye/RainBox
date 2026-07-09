"""Stage 0: disambiguate the query before any research happens.

Query terms often name several distinct things (a standard, a connector, a
product line, a software component sharing the name); researching all of
them at once produces a report that mixes incompatible meanings while
looking coherent. One structured call chooses an explicit scope; the
rendered scope block then travels in the user message of every downstream
stage, and the report opens with a Scope section so the reader sees which
interpretation they got."""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from research import prompts
from research.caller import Caller


class ScopeModel(BaseModel):
    meanings: list[str] = Field(
        description="Distinct plausible meanings of the query's key terms."
    )
    chosen_scope: str = Field(
        description="What this report will cover, in one or two sentences."
    )
    excluded: list[str] = Field(
        description="Related meanings that are out of scope (at most a side note)."
    )
    analysis_request: str = Field(
        default="",
        description="The analytical question the query asks beyond facts "
        "(empty when the query only asks for facts).",
    )


# The prompt bans hypothetical framing, but small models keep emitting it
# ("a (hypothetical or upcoming) film ..."), and a hypothetical scope poisons
# every downstream stage. Enforced in code, like the other hygiene rules.
_HYPOTHETICAL_PAREN_RE = re.compile(r"\(\s*hypothetical[^)]*\)\s*", re.IGNORECASE)
_HYPOTHETICAL_WORD_RE = re.compile(
    r"\b(?:hypothetical(?:ly)?|possibly\s+nonexistent)\s*", re.IGNORECASE
)


def _scrub_hypothetical(text: str) -> str:
    cleaned = _HYPOTHETICAL_PAREN_RE.sub("", text)
    cleaned = _HYPOTHETICAL_WORD_RE.sub("", cleaned)
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def resolve_scope(caller: Caller, query: str) -> ScopeModel:
    result = caller.structured(prompts.SCOPE_SYSTEM, query, ScopeModel)
    assert isinstance(result, ScopeModel)
    result.chosen_scope = _scrub_hypothetical(result.chosen_scope)
    return result


def scope_block(scope: ScopeModel) -> str:
    """The scope as a user-message block for downstream prompts."""
    excluded = "; ".join(s.strip() for s in scope.excluded if s.strip())
    return (
        f"SCOPE: {scope.chosen_scope.strip()}\n"
        f"OUT OF SCOPE: {excluded or 'nothing noted'}"
    )


def scope_markdown(scope: ScopeModel) -> str:
    """The scope as the report's Scope section."""
    lines = [scope.chosen_scope.strip()]
    excluded = "; ".join(s.strip() for s in scope.excluded if s.strip())
    if excluded:
        lines += ["", f"Out of scope: {excluded}."]
    return "\n".join(lines)
