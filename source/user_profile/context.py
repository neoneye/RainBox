"""The declared-profile context snapshot: one capture per assistant turn.

The three declared-profile prompt bodies (identity, formatting guide,
knowledge calibration) and the room's context marker must all come from ONE
snapshot: reading `profile.current` independently for each block can mix two
people if the setting changes between calls, and reading the marker stamps
separately can show the new profile without its switch notice. A switch
committed after capture applies on the next turn; one committed before capture
applies to both marker and blocks on this turn (db.set_current_profile writes
the pointer and `profile.current_changed_at` in a single transaction, so the
snapshot can never see a new profile with an old change stamp;
`qa.facts_invalidated_at` is a deliberately independent event stamp).
"""

import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import sqlalchemy as sa

import db
from db.models import AppSetting, db as sqla

logger = logging.getLogger(__name__)

_CONTEXT_KEYS = ("profile.current", "qa.facts_invalidated_at",
                 "profile.current_changed_at")


@dataclass(frozen=True)
class ProfileContext:
    """Immutable per-turn snapshot: the effective profile pointer, its
    resolved data, and both context-invalidation event stamps."""

    profile_uuid: UUID | None = None
    profile: dict[str, Any] | None = field(default=None)
    facts_invalidated_at: str | None = None
    profile_changed_at: str | None = None


def current_profile_context() -> ProfileContext:
    """Read `profile.current`, `qa.facts_invalidated_at`, and
    `profile.current_changed_at` in one database statement, then resolve the
    pointer to one profile dict. All three settings are DB-only (no env, no
    default), so the raw rows are the effective values; NULL/"" reads as
    unset. App context required."""
    rows = sqla.session.execute(
        sa.select(AppSetting.key, AppSetting.value)
        .where(AppSetting.key.in_(_CONTEXT_KEYS))
    ).all()
    values = {key: (value.strip() if isinstance(value, str) else None) or None
              for key, value in rows}

    profile_uuid: UUID | None = None
    profile: dict[str, Any] | None = None
    raw = values.get("profile.current")
    if raw:
        try:
            profile_uuid = UUID(raw)
        except ValueError:
            logger.warning("profile.current is not a uuid: %r", raw)
        if profile_uuid is not None:
            profile = db.profile_get(profile_uuid)
            if profile is None:
                logger.warning(
                    "profile.current points at unknown profile %s", profile_uuid)
                profile_uuid = None

    return ProfileContext(
        profile_uuid=profile_uuid,
        profile=profile,
        facts_invalidated_at=values.get("qa.facts_invalidated_at"),
        profile_changed_at=values.get("profile.current_changed_at"),
    )
