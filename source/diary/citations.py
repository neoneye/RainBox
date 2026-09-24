"""Citation locators: `diary:<revision_uuid>:<byte_start>-<byte_end>`.

A citation names bytes in one immutable revision, so it stays meaningful
after any later sync. Syntax is checked here; whether the revision exists and
may be read is the reader's question.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

_LOCATOR = re.compile(
    r"^diary:([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}):"
    r"(\d{1,12})-(\d{1,12})$")


@dataclass(frozen=True)
class Citation:
    revision_uuid: UUID
    byte_start: int
    byte_end: int

    def __str__(self) -> str:
        return format_citation(self.revision_uuid, self.byte_start, self.byte_end)


def format_citation(revision_uuid: UUID, byte_start: int, byte_end: int) -> str:
    return f"diary:{revision_uuid}:{byte_start}-{byte_end}"


def parse_citation(text: str) -> Citation | None:
    """None for anything that is not a well-formed locator with start < end."""
    if not isinstance(text, str):
        return None
    m = _LOCATOR.match(text.strip())
    if not m:
        return None
    start, end = int(m[2]), int(m[3])
    if end <= start:
        return None
    return Citation(UUID(m[1]), start, end)
