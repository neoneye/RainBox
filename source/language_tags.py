"""Small, deterministic language-tag boundary shared by profile storage and
prompt rendering.

This is deliberately a safe BCP-47 subset rather than a complete registry
validator. Uncommon tags that fit the structural subset remain valid; model or
operator text that is not a tag never crosses into code-owned instructions.
"""

import re
from typing import Any

_LANGUAGE_RE = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,3}")
MAX_LANGUAGE_TAG_CHARS = 35


def canonical_language_tag(raw: Any) -> str | None:
    """Return a canonical tag, or ``None`` when ``raw`` is outside the safe
    subset.

    The primary subtag is lowercase, a two-letter region is uppercase, a
    four-letter script is title case, and other subtags are lowercase.
    """
    text = str(raw or "").strip()
    if not text or len(text) > MAX_LANGUAGE_TAG_CHARS:
        return None
    if not _LANGUAGE_RE.fullmatch(text):
        return None
    parts = text.split("-")
    out = [parts[0].lower()]
    for sub in parts[1:]:
        if len(sub) == 2 and sub.isalpha():
            out.append(sub.upper())
        elif len(sub) == 4 and sub.isalpha():
            out.append(sub.title())
        else:
            out.append(sub.lower())
    return "-".join(out)


def legacy_language_rows(
    data: dict[str, Any] | None,
) -> list[dict[str, str]]:
    """Best-effort row projection from the two legacy flat fields.

    Their order is preserved, but neither becomes ``prefer`` because the old
    fields never separated language knowledge from response preference.
    """
    data = data or {}
    specs = (
        ("language", "native",
         "Migrated from legacy primary language; review level and stance."),
        ("language_2", "intermediate",
         "Migrated from legacy secondary language; review level and stance."),
    )
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for key, level, note in specs:
        tag = canonical_language_tag(data.get(key))
        if tag is None or tag.casefold() in seen:
            continue
        seen.add(tag.casefold())
        rows.append(
            {"tag": tag, "level": level, "stance": "neutral", "note": note})
    return rows


def effective_language_rows(
    data: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Read authoritative rows, falling back only for pre-migration blobs."""
    data = data or {}
    if "languages" in data:
        subtree = data.get("languages")
        if not isinstance(subtree, dict):
            return []
        rows = subtree.get("rows")
        if not isinstance(rows, list):
            return []
        return [row for row in rows if isinstance(row, dict)]
    return legacy_language_rows(data)
