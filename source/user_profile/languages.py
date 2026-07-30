"""Declared profile languages as prompt candidates.

The rows a profile declares under `languages.rows` are the candidate set the
response-language classifier scores. Rendering them lives here rather than on
the classifier's agent so the /profile export and the prompt read the same
function — a second implementation would let the export claim a prompt shape
the assistant does not actually send.
"""

from typing import Any

from user_profile.formatting import valid_language_tag


def declared_language_candidates(
    profile: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Return the validated semantic part of ``languages.rows``.

    Row order, level, stance and note can all carry useful evidence. Order is
    the list's own order and is not numbered into the rows: an index field
    beside a JSON array restates what the array already says. Invalid tags
    stop at the same shared prompt boundary as every other language consumer
    instead of being copied into a code-owned prompt sentence.
    """
    from language_tags import effective_language_rows

    candidates: list[dict[str, Any]] = []
    for row in effective_language_rows((profile or {}).get("data") or {}):
        code = valid_language_tag(row.get("tag"))
        if code is None:
            continue
        candidates.append({
            "code": code,
            "level": str(row.get("level") or "").strip(),
            "stance": str(row.get("stance") or "").strip(),
            "note": str(row.get("note") or "").strip(),
        })
    return candidates
