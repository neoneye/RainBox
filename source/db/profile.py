"""Person-profile tree: folder/profile persistence + data validation.

Backs the /profile page. Holds the profile folder tree (load/validate/save —
the whole-tree bulk pattern shared with /prompt, /git and /cron) plus the
per-profile data operations: the registry-driven validator, data read/write
that preserves the connector-owned `dynamic` subtree, and duplication. The
built-in locale templates are not DB rows — they ship in
data/profile_templates.json and merge virtually into the tree load.
Re-exported from db for import compatibility.
"""
import re
from datetime import date
from typing import Any

from profile_fields import FIELDS_BY_KEY


class ProfileTreeError(ValueError):
    """A profile tree payload failed structural validation (bad uuid, dangling
    parent folder, cycle, built-in uuid, …). The PUT endpoint maps this to
    400, not 500."""


class ProfileTreeConflict(Exception):
    """The tree changed since the caller hydrated (stale base_version on save);
    mapped to HTTP 409 so the client re-hydrates instead of clobbering."""


class ProfileDataError(ValueError):
    """A profile `data` snapshot failed registry validation (unknown key,
    out-of-enum value, bad date, submitted `dynamic`). Mapped to HTTP 400
    with the offending field named."""


def validate_profile_data(data: Any) -> dict[str, Any]:
    """Validate a complete editable snapshot against the registry and return
    the canonical sparse object: known editable keys only, "" values removed
    before validation, string kinds checked strictly (enum membership, ISO
    calendar date). Deliberately soft on IANA/BCP-47/ISO-4217 membership —
    an uncommon-yet-valid value is never blocked. `dynamic` is
    connector-owned and rejected as read-only. Raises ProfileDataError
    naming the offending field."""
    if not isinstance(data, dict):
        raise ProfileDataError(f"'data' must be an object, got {type(data).__name__}")
    canonical: dict[str, Any] = {}
    for key, value in data.items():
        if key == "dynamic":
            raise ProfileDataError("field 'dynamic' is read-only (connector-owned)")
        field = FIELDS_BY_KEY.get(key)
        if field is None:
            raise ProfileDataError(f"unknown field: '{key}'")
        if value == "":
            continue  # canonicalize: blank means absent, the JSONB stays sparse
        if not isinstance(value, str):
            raise ProfileDataError(
                f"field '{key}' must be a string, got {type(value).__name__}")
        if field.kind == "enum" and value not in field.choices:
            raise ProfileDataError(
                f"field '{key}' must be one of {list(field.choices)}, got {value!r}")
        if field.kind == "date":
            # The regex pins the extended YYYY-MM-DD shape (fromisoformat alone
            # would also accept the basic 20260230 form); fromisoformat then
            # rejects impossible calendar dates like 2026-02-30.
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ProfileDataError(
                    f"field '{key}' must be an ISO date (YYYY-MM-DD), got {value!r}")
            try:
                date.fromisoformat(value)
            except ValueError:
                raise ProfileDataError(
                    f"field '{key}' is not a valid calendar date: {value!r}") from None
        canonical[key] = value
    return canonical
