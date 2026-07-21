"""Knowledge-calibration subtree: per-topic self-declared calibration rows
stored under a profile's `data["calibration"]["topics"]`.

The axes are deliberately orthogonal — `level` (expertise), `stance`
(prefer/avoid), and `depth` (explanation style) answer different questions;
usage recency shades `level` and lives in the free-text `note`. Rows carry
server-owned stable ids (rename/reorder/diff stay unambiguous) and an
`updated_at` UTC instant that changes only when the row's semantic fields
change — reordering restamps nothing.

Writes are last-acknowledged-write-wins WITHIN the subtree (a single-operator
preference list, same call the flat fields and /prompt content already made);
ACROSS subtrees every write goes through db.profile.profile_mutate_data's row
lock so a flat autosave can never race a calibration commit and resurrect an
old subtree. Not routed through the flat registry-field PUT.
"""

import json
import re
import unicodedata
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

MAX_CALIBRATION_ROWS = 100
MAX_TOPIC_CHARS = 80
MAX_NOTE_CHARS = 400
MAX_CALIBRATION_BYTES = 64 * 1024  # canonical UTF-8 JSON of the whole subtree

CALIBRATION_LEVELS = ("expert", "intermediate", "beginner", "none")
CALIBRATION_STANCES = ("prefer", "neutral", "avoid")
CALIBRATION_DEPTHS = ("concise", "standard", "teach")

# The editable row fields, in canonical serialization order.
_SEMANTIC_KEYS = ("topic", "level", "stance", "depth", "note")
_ALLOWED_KEYS = frozenset(("id", *_SEMANTIC_KEYS))


class ProfileCalibrationError(ValueError):
    """A calibration snapshot failed validation (unknown key, bad enum value,
    duplicate topic, oversized text, client-supplied server field, …).
    Mapped to HTTP 400 with the offending row named."""


def _now_stamp() -> str:
    """RFC 3339 UTC with whole seconds and Z."""
    return datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def topic_key(topic: str) -> str:
    """The duplicate-detection key: NFKC-normalized, whitespace-collapsed,
    trimmed, casefolded — " PostgreSQL ", "postgresql", and visually
    equivalent Unicode forms are one topic without lowercasing what the
    operator sees."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", topic).strip()).casefold()


def _display_topic(topic: str) -> str:
    """Stored display form: trimmed, internal whitespace collapsed, case kept."""
    return re.sub(r"\s+", " ", topic.strip())


def calibration_rows(data: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The stored topic rows from one profile `data` blob; an absent or
    malformed subtree reads as no topics."""
    subtree = (data or {}).get("calibration")
    if not isinstance(subtree, dict):
        return []
    topics = subtree.get("topics")
    if not isinstance(topics, list):
        return []
    return [row for row in topics if isinstance(row, dict)]


def validate_calibration_topics(
    topics: Any, existing: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Validate one complete topic snapshot against the stored rows and
    return the canonical row list (ids assigned, stamps carried or advanced).

    Raises ProfileCalibrationError naming the offending row. Rules: existing
    row ids round-trip, new rows omit `id`; client-supplied `updated_at`,
    unknown ids, and unknown keys are rejected; blank optional values are
    removed; a row whose editable values are all blank is dropped before
    validation, one with content but no topic or level is an error; topics
    are unique by casefolded key with both conflicting positions named.
    `updated_at` advances only when a row's semantic fields changed —
    order-only changes restamp nothing."""
    if not isinstance(topics, list):
        raise ProfileCalibrationError(
            f"'topics' must be a list, got {type(topics).__name__}")
    existing_by_id = {str(row.get("id")): row for row in existing if row.get("id")}
    stamp = _now_stamp()
    canonical: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_topics: dict[str, int] = {}

    for index, raw in enumerate(topics):
        position = f"row {index + 1}"
        if not isinstance(raw, dict):
            raise ProfileCalibrationError(
                f"{position}: entry must be an object, got {type(raw).__name__}")
        if "updated_at" in raw:
            raise ProfileCalibrationError(
                f"{position}: 'updated_at' is server-owned and must not be submitted")
        unknown = set(raw) - _ALLOWED_KEYS
        if unknown:
            raise ProfileCalibrationError(
                f"{position}: unknown key: '{sorted(unknown)[0]}'")
        for key in raw:
            if raw[key] is not None and not isinstance(raw[key], str):
                raise ProfileCalibrationError(
                    f"{position}: '{key}' must be a string, "
                    f"got {type(raw[key]).__name__}")

        values = {k: str(raw.get(k) or "").strip() for k in _SEMANTIC_KEYS}
        if not any(values.values()):
            continue  # an all-blank row is dropped before validation
        topic = _display_topic(values["topic"])
        if not topic:
            raise ProfileCalibrationError(f"{position}: missing 'topic'")
        if len(topic) > MAX_TOPIC_CHARS:
            raise ProfileCalibrationError(
                f"{position}: 'topic' exceeds {MAX_TOPIC_CHARS} characters")
        if not values["level"]:
            raise ProfileCalibrationError(f"{position}: missing 'level'")
        if values["level"] not in CALIBRATION_LEVELS:
            raise ProfileCalibrationError(
                f"{position}: 'level' must be one of {list(CALIBRATION_LEVELS)}, "
                f"got {values['level']!r}")
        if values["stance"] and values["stance"] not in CALIBRATION_STANCES:
            raise ProfileCalibrationError(
                f"{position}: 'stance' must be one of {list(CALIBRATION_STANCES)}, "
                f"got {values['stance']!r}")
        if values["depth"] and values["depth"] not in CALIBRATION_DEPTHS:
            raise ProfileCalibrationError(
                f"{position}: 'depth' must be one of {list(CALIBRATION_DEPTHS)}, "
                f"got {values['depth']!r}")
        if len(values["note"]) > MAX_NOTE_CHARS:
            raise ProfileCalibrationError(
                f"{position}: 'note' exceeds {MAX_NOTE_CHARS} characters")

        key = topic_key(topic)
        if key in seen_topics:
            raise ProfileCalibrationError(
                f"duplicate topic {topic!r}: row {seen_topics[key] + 1} and "
                f"{position} name the same topic")
        seen_topics[key] = index

        row: dict[str, Any] = {"topic": topic, "level": values["level"]}
        for optional in ("stance", "depth", "note"):
            if values[optional]:
                row[optional] = values[optional]

        raw_id = raw.get("id")
        if raw_id:
            try:
                row_id = str(UUID(str(raw_id)))
            except ValueError:
                raise ProfileCalibrationError(
                    f"{position}: 'id' is not a uuid: {raw_id!r}") from None
            if row_id not in existing_by_id:
                raise ProfileCalibrationError(
                    f"{position}: unknown row id {row_id}")
            if row_id in seen_ids:
                raise ProfileCalibrationError(
                    f"{position}: duplicate row id {row_id}")
            seen_ids.add(row_id)
            prior = existing_by_id[row_id]
            unchanged = all(
                str(prior.get(k) or "") == str(row.get(k) or "")
                for k in _SEMANTIC_KEYS)
            row["id"] = row_id
            row["updated_at"] = (prior.get("updated_at") if unchanged
                                 and prior.get("updated_at") else stamp)
        else:
            row["id"] = str(uuid4())
            row["updated_at"] = stamp
        canonical.append(row)

    if len(canonical) > MAX_CALIBRATION_ROWS:
        raise ProfileCalibrationError(
            f"at most {MAX_CALIBRATION_ROWS} calibration rows are stored, "
            f"got {len(canonical)}")
    blob = json.dumps({"topics": canonical}, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")
    if len(blob) > MAX_CALIBRATION_BYTES:
        raise ProfileCalibrationError(
            f"calibration exceeds {MAX_CALIBRATION_BYTES} bytes serialized "
            f"({len(blob)})")
    return canonical


def calibration_get(profile_uuid: UUID) -> dict[str, Any] | None:
    """The canonical topic rows for one profile (built-ins served from the
    shipped file), or None if the uuid is unknown. Shape mirrors the API
    payload: {"builtin": bool, "topics": [...]}."""
    from db.profile import profile_get

    profile = profile_get(profile_uuid)
    if profile is None:
        return None
    return {"builtin": bool(profile.get("builtin")),
            "topics": calibration_rows(profile.get("data"))}


def calibration_put(profile_uuid: UUID, topics: Any) -> list[dict[str, Any]] | None:
    """Replace one profile's calibration snapshot (last acknowledged write
    wins within the subtree) under the shared row lock, validating against
    the locked row's current rows. Returns the canonical rows (the client
    needs server-assigned ids and stamps before its next edit), or None if
    the uuid is unknown. Raises ProfileCalibrationError on validation
    failure. Built-in uuids are the API layer's 400 (no row exists here)."""
    from db.profile import profile_mutate_data

    result: dict[str, list[dict[str, Any]]] = {}

    def _mutate(current: dict[str, Any]) -> dict[str, Any]:
        canonical = validate_calibration_topics(
            topics, calibration_rows(current))
        result["topics"] = canonical
        if canonical:
            current["calibration"] = {"topics": canonical}
        else:
            current.pop("calibration", None)  # empty reads as absent; stay sparse
        return current

    row = profile_mutate_data(profile_uuid, _mutate)
    if row is None:
        return None
    return result["topics"]


def refresh_calibration_identity(data: dict[str, Any]) -> dict[str, Any]:
    """Rewrite a copied data blob's calibration rows with fresh ids and the
    duplication timestamp: duplication copies semantic fields and order,
    never concurrency identity (built-in example rows may carry fixed
    ids/stamps in the shipped file; those must not survive into an editable
    copy)."""
    rows = calibration_rows(data)
    if not rows:
        return data
    stamp = _now_stamp()
    data["calibration"] = {"topics": [
        {**row, "id": str(uuid4()), "updated_at": stamp} for row in rows
    ]}
    return data
