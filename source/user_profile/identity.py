"""Operator identity block: who the operator *is*, from the current profile.

The `profile.current` setting points at one profile on the /profile page (a
person profile — the operator's own "account"). This module renders that
profile's filled-in fields into a compact prompt block the assistant injects
as `<user_settings_json>`, next to the memory-derived `<operator_profile>`
digest: identity is declared once by the operator, the digest accrues from
remembered claims.

Rendering is registry-driven (`profile_fields.PROFILE_FIELDS`) and emits JSON:
fields appear under their registry keys in registry order, absent/blank fields
are skipped, and the connector-owned `dynamic` subtree is never rendered. JSON
because every value is escaped by json.dumps (a field containing newlines or
quotes cannot forge structure) and the registry keys are stable machine
identifiers — and local models see far more JSON than any other prompt shape.
"""

import json
import logging
from typing import Any
from uuid import UUID

import db
from profile_fields import PROFILE_FIELDS

logger = logging.getLogger(__name__)


def current_profile() -> dict[str, Any] | None:
    """The profile selected by the `profile.current` setting (full data blob,
    via profile_get), or None when the setting is unset or the uuid no longer
    resolves (e.g. the profile was deleted after being selected). App context
    required (reads the setting from the DB)."""
    raw = db.get_setting("profile.current")
    if not raw:
        return None
    try:
        target = UUID(str(raw).strip())
    except ValueError:
        logger.warning("profile.current is not a uuid: %r", raw)
        return None
    profile = db.profile_get(target)
    if profile is None:
        logger.warning("profile.current points at unknown profile %s", target)
    return profile


def format_identity_block(profile: dict[str, Any]) -> str:
    """Render one profile as a prompt block: a JSON object of the filled-in
    fields under their registry keys, in registry order. No preamble line
    and no profile display name: the enclosing <user_settings_json> tag
    names the content, and the tree label is operator
    bookkeeping (it rides the per-step debug log, not the prompt). This is
    the single place to experiment with identity prompt formatting.

    A field whose raw value is opaque (number_format's sample string) gets a
    code-owned "<key>.comment" entry spelling the convention out — looked up
    from the validated enum value, never operator text, so it cannot smuggle
    instructions into this context-authority block."""
    from user_profile.formatting import NUMBER_FORMAT_COMMENTS

    data = profile.get("data") or {}
    payload: dict[str, str] = {}
    for field in PROFILE_FIELDS:
        value = str(data.get(field.key) or "").strip()
        if not value:
            continue
        payload[field.key] = value
        if field.key == "number_format" and value in NUMBER_FORMAT_COMMENTS:
            payload["number_format.comment"] = NUMBER_FORMAT_COMMENTS[value]
    return json.dumps(payload, ensure_ascii=False, indent=2)


def build_identity_block() -> str:
    """The operator identity prompt block, or "" when no current profile is
    set — so callers can inject unconditionally without a stray header."""
    profile = current_profile()
    if profile is None:
        return ""
    return format_identity_block(profile)
