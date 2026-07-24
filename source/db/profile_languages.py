"""Multilingual profile subtree stored under ``data["languages"]["rows"]``.

Language competence (``level``) and reply preference (``stance``) are
orthogonal. Rows carry server-owned stable ids and semantic-change timestamps,
matching the calibration editor's concurrency and autosave contract.

There is no flat-field fallback: this subtree is the sole language source.
"""

import json
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from db.models import db
from language_tags import (
    MAX_LANGUAGE_TAG_CHARS,
    canonical_language_tag,
    effective_language_rows,
)

MAX_LANGUAGE_ROWS = 100
MAX_LANGUAGE_NOTE_CHARS = 400
MAX_LANGUAGES_BYTES = 64 * 1024

LANGUAGE_LEVELS = ("native", "fluent", "intermediate", "beginner")
LANGUAGE_STANCES = ("prefer", "neutral", "avoid")

_SEMANTIC_KEYS = ("tag", "level", "stance", "note")
_ALLOWED_KEYS = frozenset(("id", *_SEMANTIC_KEYS))

__all__ = [
    "LANGUAGE_LEVELS",
    "LANGUAGE_STANCES",
    "MAX_LANGUAGE_NOTE_CHARS",
    "MAX_LANGUAGE_ROWS",
    "MAX_LANGUAGES_BYTES",
    "ProfileLanguagesError",
    "languages_get",
    "languages_put",
    "refresh_language_identity",
    "validate_language_rows",
]


class ProfileLanguagesError(ValueError):
    """A languages snapshot failed row, enum, uniqueness, or size validation."""


def _now_stamp() -> str:
    """RFC 3339 UTC with whole seconds and Z."""
    return datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_language_rows(
    rows: Any, existing: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Validate and canonicalize one complete row snapshot.

    Existing ids round-trip; new rows omit ``id``. ``updated_at`` and unknown
    ids are rejected. All-blank rows are dropped, semantic edits restamp only
    the changed row, tags are unique after canonicalization, and at most one
    row may have ``stance=prefer``.
    """
    if not isinstance(rows, list):
        raise ProfileLanguagesError(
            f"'rows' must be a list, got {type(rows).__name__}")
    if len(rows) > 10 * MAX_LANGUAGE_ROWS:
        raise ProfileLanguagesError(
            f"'rows' has {len(rows)} entries; at most "
            f"{10 * MAX_LANGUAGE_ROWS} are accepted per request")

    existing_by_id = {
        str(row.get("id")): row for row in existing if row.get("id")}
    stamp = _now_stamp()
    canonical: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_tags: dict[str, int] = {}
    preferred_at: int | None = None

    for index, raw in enumerate(rows):
        position = f"row {index + 1}"
        if not isinstance(raw, dict):
            raise ProfileLanguagesError(
                f"{position}: entry must be an object, got {type(raw).__name__}")
        if "updated_at" in raw:
            raise ProfileLanguagesError(
                f"{position}: 'updated_at' is server-owned and must not be submitted")
        unknown = set(raw) - _ALLOWED_KEYS
        if unknown:
            raise ProfileLanguagesError(
                f"{position}: unknown key: '{sorted(unknown)[0]}'")
        for key, value in raw.items():
            if value is not None and not isinstance(value, str):
                raise ProfileLanguagesError(
                    f"{position}: '{key}' must be a string, "
                    f"got {type(value).__name__}")

        values = {key: str(raw.get(key) or "").strip()
                  for key in _SEMANTIC_KEYS}
        if not any(values.values()):
            continue
        if not values["tag"]:
            raise ProfileLanguagesError(f"{position}: missing 'tag'")
        tag = canonical_language_tag(values["tag"])
        if tag is None:
            raise ProfileLanguagesError(
                f"{position}: 'tag' must be a BCP-47 language tag of at most "
                f"{MAX_LANGUAGE_TAG_CHARS} characters, got {values['tag']!r}")
        tag_key = tag.casefold()
        if tag_key in seen_tags:
            raise ProfileLanguagesError(
                f"duplicate language tag {tag!r}: row "
                f"{seen_tags[tag_key] + 1} and {position}")
        seen_tags[tag_key] = index

        if not values["level"]:
            raise ProfileLanguagesError(f"{position}: missing 'level'")
        if values["level"] not in LANGUAGE_LEVELS:
            raise ProfileLanguagesError(
                f"{position}: 'level' must be one of {list(LANGUAGE_LEVELS)}, "
                f"got {values['level']!r}")
        if not values["stance"]:
            raise ProfileLanguagesError(f"{position}: missing 'stance'")
        if values["stance"] not in LANGUAGE_STANCES:
            raise ProfileLanguagesError(
                f"{position}: 'stance' must be one of {list(LANGUAGE_STANCES)}, "
                f"got {values['stance']!r}")
        if values["stance"] == "prefer":
            if preferred_at is not None:
                raise ProfileLanguagesError(
                    f"only one language may have stance 'prefer': row "
                    f"{preferred_at + 1} and {position}")
            preferred_at = index
        if len(values["note"]) > MAX_LANGUAGE_NOTE_CHARS:
            raise ProfileLanguagesError(
                f"{position}: 'note' exceeds {MAX_LANGUAGE_NOTE_CHARS} characters")

        row: dict[str, Any] = {
            "tag": tag, "level": values["level"], "stance": values["stance"]}
        if values["note"]:
            row["note"] = re.sub(r"\s+", " ", values["note"]).strip()

        raw_id = raw.get("id")
        if raw_id:
            try:
                row_id = str(UUID(str(raw_id)))
            except ValueError:
                raise ProfileLanguagesError(
                    f"{position}: 'id' is not a uuid: {raw_id!r}") from None
            if row_id not in existing_by_id:
                raise ProfileLanguagesError(
                    f"{position}: unknown row id {row_id}")
            if row_id in seen_ids:
                raise ProfileLanguagesError(
                    f"{position}: duplicate row id {row_id}")
            seen_ids.add(row_id)
            prior = existing_by_id[row_id]
            unchanged = all(
                str(prior.get(key) or "") == str(row.get(key) or "")
                for key in _SEMANTIC_KEYS)
            row["id"] = row_id
            row["updated_at"] = (
                prior.get("updated_at")
                if unchanged and prior.get("updated_at") else stamp)
        else:
            row["id"] = str(uuid4())
            row["updated_at"] = stamp
        canonical.append(row)

    if len(canonical) > MAX_LANGUAGE_ROWS:
        raise ProfileLanguagesError(
            f"at most {MAX_LANGUAGE_ROWS} language rows are stored, "
            f"got {len(canonical)}")
    blob = json.dumps({"rows": canonical}, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")
    if len(blob) > MAX_LANGUAGES_BYTES:
        raise ProfileLanguagesError(
            f"languages exceeds {MAX_LANGUAGES_BYTES} bytes serialized "
            f"({len(blob)})")
    return canonical


def languages_get(profile_uuid: UUID) -> dict[str, Any] | None:
    """Return canonical rows for one user or built-in profile."""
    from db.profile import profile_get

    profile = profile_get(profile_uuid)
    if profile is None:
        return None
    return {
        "builtin": bool(profile.get("builtin")),
        "rows": effective_language_rows(profile.get("data")),
    }


def languages_put(
    profile_uuid: UUID, rows: Any,
) -> list[dict[str, Any]] | None:
    """Replace one profile's authoritative languages snapshot under the
    shared profile row lock.

    An empty snapshot is stored as ``{"rows": []}``.
    """
    from db.profile import profile_mutate_data

    result: dict[str, list[dict[str, Any]]] = {}

    def _mutate(current: dict[str, Any]) -> dict[str, Any]:
        canonical = validate_language_rows(
            rows, effective_language_rows(current))
        result["rows"] = canonical
        current["languages"] = {"rows": canonical}
        return current

    row = profile_mutate_data(profile_uuid, _mutate)
    if row is None:
        return None
    return result["rows"]


def refresh_language_identity(data: dict[str, Any]) -> dict[str, Any]:
    """Give copied language rows fresh concurrency identities."""
    rows = effective_language_rows(data)
    if "languages" not in data:
        return data
    stamp = _now_stamp()
    data["languages"] = {"rows": [
        {**row, "id": str(uuid4()), "updated_at": stamp} for row in rows
    ]}
    return data
