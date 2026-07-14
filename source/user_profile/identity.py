"""Operator identity block: who the operator *is*, from the current profile.

The `profile.current` setting points at one profile on the /profile page (a
person profile — the operator's own "account"). This module renders that
profile's filled-in fields into a compact prompt block the assistant injects
as `<operator_identity>`, next to the memory-derived `<operator_profile>`
digest: identity is declared once by the operator, the digest accrues from
remembered claims.

Rendering is registry-driven (`profile_fields.PROFILE_FIELDS`): fields appear
in registry order under their human labels, absent/blank fields are skipped,
and the connector-owned `dynamic` subtree is never rendered.
"""

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
    """Render one profile as prompt text: a header naming the profile, then
    one `- Label: value` line per filled-in field, in registry order. This is
    the single place to experiment with identity prompt formatting."""
    data = profile.get("data") or {}
    name = str(profile.get("name") or "").strip()
    header = "Who the operator is"
    if name:
        header += f" (profile: {name})"
    lines = [header + ":"]
    for field in PROFILE_FIELDS:
        value = str(data.get(field.key) or "").strip()
        if value:
            lines.append(f"- {field.label}: {value}")
    return "\n".join(lines)


def build_identity_block() -> str:
    """The operator identity prompt block, or "" when no current profile is
    set — so callers can inject unconditionally without a stray header."""
    profile = current_profile()
    if profile is None:
        return ""
    return format_identity_block(profile)
