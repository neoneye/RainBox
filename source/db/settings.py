"""Operator configuration stored in Postgres (the `app_setting` table).

A small **code-side registry** (`SETTINGS`) is the source of truth for which keys
exist and their type/default/validation/secret-ness and the legacy env var each
shadows. The `AppSetting` table only persists the *value*; its `value_type` /
`secret` / `description` columns are a seeded cache reconciled from the registry
on startup (`reconcile_app_settings`, called from init_db).

Read precedence: **DB value (if set) → env var → registry default.**
- string/json: NULL or "" counts as unset (falls through).
- bool/int: parsed from text, so `false`/`0` are explicit values; only NULL/""
  (or absent) is unset.

`get_setting()` touches db.session, so it must run inside a Flask app context
(the cron scheduler and web app both have one). Standalone CLI tools should NOT
call it — they pass explicit args instead. See the proposal:
docs/proposals/2026-06-07-user-configuration-in-postgres.md.
"""
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from db.models import AppSetting, db

logger = logging.getLogger(__name__)

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}

REDACTED = "••••••"


class UnknownSetting(KeyError):
    """A key not present in the registry was requested."""


@dataclass(frozen=True)
class Setting:
    key: str
    env: str | None              # legacy env var this shadows (fallback), or None
    type: str = "string"         # string|bool|int|json
    default: object = None
    secret: bool = False
    validate: Callable[[object], None] | None = None
    description: str = ""
    # Computed fallback used instead of `default` when DB and env are unset
    # (e.g. chat.default_model derives from the model_config table). Must be
    # cheap and app-context safe: it runs on every unset get_setting().
    dynamic_default: Callable[[], object] | None = None
    # Machine-owned bookkeeping keys (event stamps) that the /settings page
    # must not list. get_setting/set_setting treat them normally. Distinct
    # from `secret`: secrets are redacted but still listed and env-managed;
    # internal keys are ordinary DB values that are simply not operator-facing.
    internal: bool = False


def _validate_age_recipient(value: object) -> None:
    """Each whitespace/comma-separated token must be an age recipient (`age1…`).
    Empty means unset (allowed). SSH recipients (`ssh-ed25519 AAAA…`) contain a
    space and can't be expressed in this inline format — use the env-only
    recipients-file (RAINBOX_BACKUP_AGE_RECIPIENTS_FILE) for those."""
    import re

    for tok in re.split(r"[\s,]+", str(value).strip()):
        if tok and not tok.startswith("age1"):
            raise ValueError(
                f"not an age recipient (expected 'age1…'): {tok!r}"
            )


def _validate_chat_default_model(value: object) -> None:
    """Must be the uuid of an existing ModelConfig or ModelConfigOverride."""
    from uuid import UUID

    import db.model_config as model_config

    try:
        target = UUID(str(value).strip())
    except ValueError:
        raise ValueError(
            f"chat.default_model: not a uuid: {value!r}"
        ) from None
    try:
        model_config.resolved_model_kwargs(target)
    except LookupError as exc:
        raise ValueError(f"chat.default_model: {exc}") from None


def _validate_profile_current(value: object) -> None:
    """Must be the uuid of an existing profile (user-owned or built-in
    template) on the /profile page."""
    from uuid import UUID

    import db.profile as profile

    try:
        target = UUID(str(value).strip())
    except ValueError:
        raise ValueError(
            f"profile.current: not a uuid: {value!r}"
        ) from None
    if profile.profile_get(target) is None:
        raise ValueError(f"profile.current: no profile with uuid {target}")


def _default_chat_model() -> object:
    """Unset fallback for chat.default_model: the alphabetically earliest
    model config override (or None when no overrides exist)."""
    import db.model_config as model_config

    default = model_config.default_chat_model_uuid()
    return str(default) if default is not None else None


# The registry. Adding a key here is all it takes; init_db reconciles the row.
SETTINGS: dict[str, Setting] = {
    "backup.repo": Setting(
        "backup.repo", "RAINBOX_BACKUP_REPO", "string", None,
        description="Directory backups are written under.",
    ),
    "backup.age_recipient": Setting(
        "backup.age_recipient", "RAINBOX_BACKUP_AGE_RECIPIENT", "string", None,
        validate=_validate_age_recipient,
        description="age public key(s) backups are encrypted to "
                    "(whitespace/comma separated).",
    ),
    "backup.git_push": Setting(
        "backup.git_push", "RAINBOX_BACKUP_GIT_PUSH", "bool", False,
        description="Commit+push each backup into the backup-repo git repo.",
    ),
    "memory.recall_fifo_capacity": Setting(
        "memory.recall_fifo_capacity", None, "int", 10,
        description="Per-memory recall KPI FIFO: how many of the newest "
                    "used (true positive) and rejected (false positive) "
                    "filter verdicts are retained per memory; older ones are "
                    "pruned when new verdicts are recorded.",
    ),
    "cron.paused": Setting(
        "cron.paused", None, "bool", False,
        description="Global cron pause: while on, the scheduler fires nothing "
                    "(per-job/folder enabled flags are untouched, so resuming "
                    "restores the exact prior state).",
    ),
    "assistant.disabled_capabilities": Setting(
        "assistant.disabled_capabilities", None, "json", [],
        description="Assistant capability names the operator has turned off "
                    '(JSON list, e.g. ["memory_query","workspace_read_command"]). A '
                    "disabled capability is removed from both the assistant's "
                    "prompt catalog and its dispatch path.",
    ),
    "chat.default_model": Setting(
        "chat.default_model", None, "string", None,
        validate=_validate_chat_default_model,
        dynamic_default=_default_chat_model,
        description="Model a direct chat room talks to while the room itself "
                    "has no model selected (a ModelConfig or "
                    "ModelConfigOverride uuid). Picking a model inside a room "
                    "overrides this for that room only. Unset = the "
                    "alphabetically earliest model config override.",
    ),
    "profile.current": Setting(
        "profile.current", None, "string", None,
        validate=_validate_profile_current,
        description="The profile (from /profile) that IS the operator — the "
                    "current 'account'. The assistant injects this profile's "
                    "filled-in fields into every turn as <operator_identity>, "
                    "so it knows who it is talking to. Unset = no identity "
                    "block in the prompt.",
    ),
    "customize.dir": Setting(
        "customize.dir", "RAINBOX_CUSTOMIZE_DIR", "string", None,
        description="Directory with the operator's private customizations "
                    "(PII / persona — e.g. a checkout of a private repo). "
                    "Mirrors data/'s file naming: question_answer.jsonl "
                    "here overlays the base Q&A registry by id. Empty = no "
                    "overlay. After changing it (or editing the files), "
                    "press 'Repopulate Q&A memory'.",
    ),
    "qa.unlocked_shields": Setting(
        "qa.unlocked_shields", None, "json", [],
        description="Names of Q&A shields the operator has unlocked. A Q&A entry "
                    "carrying a shield reaches the LLM only when that shield is "
                    "in this list; an entry with no shield is always visible. "
                    "Empty (the default) keeps every shielded entry hidden.",
    ),
    "qa.facts_invalidated_at": Setting(
        "qa.facts_invalidated_at", None, "string", None,
        description="ISO timestamp of the last change that can stale prior "
                    "facts (a shield toggle or a Q&A repopulate). The assistant "
                    "posts a one-time 're-check facts' notice into a room the "
                    "next time it runs there after this changes.",
    ),
    "assistant.formatting_guide": Setting(
        "assistant.formatting_guide", None, "bool", False,
        description="Inject the active profile's deterministic formatting "
                    "guide into assistant turns. Default off: enable after "
                    "the formatting block passes its live release gate "
                    "(evals/profile_gate.py). Independent of the "
                    "calibration switch — the blocks gate separately.",
    ),
    "assistant.knowledge_calibration": Setting(
        "assistant.knowledge_calibration", None, "bool", False,
        description="Inject the active profile's knowledge-calibration rows "
                    "into assistant turns. Default off: enable after the "
                    "calibration block passes its live release gate "
                    "(evals/profile_gate.py). Independent of the formatting "
                    "switch — the blocks gate separately.",
    ),
    "profile.current_changed_at": Setting(
        "profile.current_changed_at", None, "string", None, internal=True,
        description="Event stamp of the last actual profile.current change, "
                    "written by set_current_profile in the same transaction "
                    "as the pointer and independent of "
                    "qa.facts_invalidated_at. The assistant's per-room "
                    "context marker acknowledges it; not operator-facing.",
    ),
}


def _registry(key: str) -> Setting:
    try:
        return SETTINGS[key]
    except KeyError:
        raise UnknownSetting(key) from None


def _coerce(spec: Setting, text: str) -> object:
    if spec.type == "bool":
        low = text.strip().lower()
        if low in _TRUTHY:
            return True
        if low in _FALSY:
            return False
        raise ValueError(f"{spec.key}: not a boolean: {text!r}")
    if spec.type == "int":
        return int(text)
    if spec.type == "json":
        return json.loads(text)
    return text  # string


def _is_unset(spec: Setting, text: str | None) -> bool:
    """For strings/json an empty value is 'unset' (use fallback); for bool/int
    only None is unset (so `false`/`0` stay explicit)."""
    if text is None:
        return True
    if spec.type in ("string", "json"):
        return text.strip() == ""
    return False


def get_setting(key: str) -> object:
    """Resolved value for `key`: DB → env → default, coerced to the declared
    type. Requires a Flask app context (reads db.session)."""
    spec = _registry(key)

    row = db.session.query(AppSetting).filter_by(key=key).one_or_none()
    if row is not None and not _is_unset(spec, row.value):
        return _coerce(spec, row.value)

    if spec.env:
        env_val = os.environ.get(spec.env)
        if env_val is not None and not _is_unset(spec, env_val):
            return _coerce(spec, env_val)

    if spec.dynamic_default is not None:
        return spec.dynamic_default()
    return spec.default


def _to_text(spec: Setting, value: object) -> str:
    if spec.type == "bool":
        return "true" if value else "false"
    if spec.type == "int":
        return str(int(value))  # type: ignore[arg-type]
    if spec.type == "json":
        return json.dumps(value)
    return str(value)


def _upsert_setting_row(spec: Setting, value: object) -> None:
    """Stage one setting row on the session WITHOUT committing, so a caller
    can compose several row updates into a single transaction
    (set_current_profile). Validates and stamps registry metadata exactly like
    set_setting; the caller owns commit/rollback."""
    if spec.secret and value is not None:
        raise ValueError(
            f"{spec.key} is env-only and cannot be stored in app_setting"
        )
    row = db.session.query(AppSetting).filter_by(key=spec.key).one_or_none()
    if row is None:
        row = AppSetting(key=spec.key)
        db.session.add(row)

    if value is None:
        row.value = None
    else:
        text = _to_text(spec, value)
        if spec.validate is not None and text.strip() != "":
            spec.validate(_coerce(spec, text))
        row.value = text

    # Metadata is registry-owned; keep the cached columns in sync.
    row.value_type = spec.type
    row.secret = spec.secret
    row.description = spec.description


def set_setting(key: str, value: object) -> None:
    """Persist `value` for `key` (None clears it → NULL). Validates against the
    registry; (re)stamps the row's metadata from the registry. App context
    required.

    Secrets are env-only: a `secret=True` setting must never hold a value in
    `app_setting` (it would land in cleartext in Postgres and in every backup —
    see the threat model in docs/backup.md). Clearing one (value=None) is fine."""
    _upsert_setting_row(_registry(key), value)
    db.session.commit()


def lock_setting_row(key: str) -> None:
    """Take the row lock on one `app_setting` row for the rest of the current
    transaction (SELECT ... FOR UPDATE; the reconciled registry guarantees
    the row exists). The coordination point between `set_current_profile`
    and profile deletion (db.profile.profile_save_tree): both take THIS lock
    first, before validating or mutating, so a switch cannot validate a
    profile that a concurrent tree save is about to delete and then write
    the pointer after the delete commits — the classic re-dangling race.
    App context required; the caller owns commit/rollback."""
    import sqlalchemy as sa

    db.session.execute(
        sa.select(AppSetting).where(AppSetting.key == key).with_for_update()
    ).one_or_none()


def set_current_profile(value: object) -> str | None:
    """The runtime write path for `profile.current`: validate the target,
    compare against the currently effective value, and on an actual change
    write `profile.current` and `profile.current_changed_at` as ONE
    transaction — a concurrent assistant turn sees either the complete old
    state or the complete new state, never a new profile with an old marker
    stamp. `qa.facts_invalidated_at` is deliberately untouched: a profile
    switch changes the declared-profile prompt blocks, not the Q&A knowledge
    base, and keeping the two event stamps independent is what lets the
    assistant's context marker report a still-unacknowledged Q&A event as a
    combined notice instead of silently absorbing it into the switch.

    Returns the stamp when a change was committed, or None on a no-op (same
    effective value). A plain set_setting("profile.current", ...) still works
    but stamps nothing — it is the low-level seam for tests and scripts; UI
    writes must route here (webapp/settings_views.py special-cases the key).
    App context required."""
    from uuid import UUID

    spec = _registry("profile.current")

    def _norm(raw: object) -> str | None:
        if raw is None or str(raw).strip() == "":
            return None
        try:
            return str(UUID(str(raw).strip()))
        except ValueError:
            return None  # a corrupt stored value reads as unset

    try:
        # Lock BEFORE validating: a concurrent tree save deleting the target
        # profile holds this same lock across its deletion, so validation
        # here cannot observe a profile that is mid-deletion and then write
        # a dangling pointer after that delete commits.
        lock_setting_row("profile.current")
        new_norm: str | None = None
        if value is not None and str(value).strip() != "":
            assert spec.validate is not None
            spec.validate(value)  # bad uuid / unknown profile → ValueError
            new_norm = str(UUID(str(value).strip()))
        if _norm(get_setting("profile.current")) == new_norm:
            db.session.rollback()  # release the lock; nothing to write
            return None
        stamp = datetime.now(UTC).isoformat()
        _upsert_setting_row(spec, new_norm)
        _upsert_setting_row(_registry("profile.current_changed_at"), stamp)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return stamp


def mark_facts_invalidated() -> str:
    """Stamp `qa.facts_invalidated_at` with the current time and return it.

    Called when a change can stale prior facts (a shield toggle or a Q&A
    repopulate). The assistant compares this against the markers already in a
    room to post a one-time re-check-facts notice. App context required; the
    value is persisted immediately via set_setting."""
    stamp = datetime.now(UTC).isoformat()
    set_setting("qa.facts_invalidated_at", stamp)
    return stamp


def _source(spec: Setting) -> str:
    row = db.session.query(AppSetting).filter_by(key=spec.key).one_or_none()
    if row is not None and not _is_unset(spec, row.value):
        return "db"
    if spec.env and not _is_unset(spec, os.environ.get(spec.env)):
        return "env"
    return "default"


def all_settings(include_internal: bool = False) -> list[dict]:
    """Every registry setting with its effective value, type, secret flag,
    description, and provenance (db/env/default). Secrets are redacted; internal
    bookkeeping keys are omitted unless include_internal is set (the /settings
    page never lists them). Metadata comes from the registry, never the row, so
    it can't drift. App context required."""
    out = []
    for spec in SETTINGS.values():
        if spec.internal and not include_internal:
            continue
        value = get_setting(spec.key)
        out.append({
            "key": spec.key,
            "value": REDACTED if (spec.secret and value not in (None, "")) else value,
            "value_type": spec.type,
            "secret": spec.secret,
            "description": spec.description,
            "source": _source(spec),
        })
    return out


def reconcile_app_settings() -> None:
    """Idempotent: ensure an `app_setting` row exists for every registry key
    (value left NULL so env/default still apply) and (re)stamp its
    value_type/secret/description from the registry, so the cached metadata
    columns never drift from code. Called from init_db. App context required."""
    existing = {r.key: r for r in db.session.query(AppSetting).all()}
    for spec in SETTINGS.values():
        row = existing.get(spec.key)
        if row is None:
            row = AppSetting(key=spec.key, value=None)
            db.session.add(row)
        row.value_type = spec.type
        row.secret = spec.secret
        row.description = spec.description
    db.session.commit()
