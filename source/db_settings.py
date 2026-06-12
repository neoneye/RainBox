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

from db_models import AppSetting, db

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
    "cron.paused": Setting(
        "cron.paused", None, "bool", False,
        description="Global cron pause: while on, the scheduler fires nothing "
                    "(per-job/folder enabled flags are untouched, so resuming "
                    "restores the exact prior state).",
    ),
    "customize.dir": Setting(
        "customize.dir", "RAINBOX_CUSTOMIZE_DIR", "string", None,
        description="Directory with the operator's private customizations "
                    "(PII / persona — e.g. a checkout of a private repo). "
                    "Mirrors memory/'s file naming: question_answer.jsonl "
                    "here overlays the base Q&A registry by id. Empty = no "
                    "overlay. After changing it (or editing the files), "
                    "press 'Repopulate Q&A memory'.",
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

    return spec.default


def _to_text(spec: Setting, value: object) -> str:
    if spec.type == "bool":
        return "true" if value else "false"
    if spec.type == "int":
        return str(int(value))  # type: ignore[arg-type]
    if spec.type == "json":
        return json.dumps(value)
    return str(value)


def set_setting(key: str, value: object) -> None:
    """Persist `value` for `key` (None clears it → NULL). Validates against the
    registry; (re)stamps the row's metadata from the registry. App context
    required.

    Secrets are env-only: a `secret=True` setting must never hold a value in
    `app_setting` (it would land in cleartext in Postgres and in every backup —
    see the threat model in docs/backup.md). Clearing one (value=None) is fine."""
    spec = _registry(key)
    if spec.secret and value is not None:
        raise ValueError(
            f"{key} is env-only and cannot be stored in app_setting"
        )
    row = db.session.query(AppSetting).filter_by(key=key).one_or_none()
    if row is None:
        row = AppSetting(key=key)
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
    db.session.commit()


def _source(spec: Setting) -> str:
    row = db.session.query(AppSetting).filter_by(key=spec.key).one_or_none()
    if row is not None and not _is_unset(spec, row.value):
        return "db"
    if spec.env and not _is_unset(spec, os.environ.get(spec.env)):
        return "env"
    return "default"


def all_settings() -> list[dict]:
    """Every registry setting with its effective value, type, secret flag,
    description, and provenance (db/env/default). Secrets are redacted. Metadata
    comes from the registry, never the row, so it can't drift. App context
    required."""
    out = []
    for spec in SETTINGS.values():
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
