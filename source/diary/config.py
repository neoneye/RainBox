"""Source manifests and parser configuration.

A manifest is the operator's private JSON description of one diary source
(root directory, room, timezone, dialect rules, overrides). `load_manifest`
validates it completely before anything is written; `build_parser_config`
turns it into the immutable configuration a generation is parsed with, and
`config_fingerprint` names that configuration.

Pure: no database, no model. The only filesystem access is the root check in
`load_manifest(..., check_root=True)`.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Bump when parse_file's output for the same bytes and config changes. Part
# of the fingerprint, so every generation parsed by older code goes stale.
PARSER_VERSION = 1
# Bump when the identifier annotation rules change (same effect).
IDENTIFIER_VERSION = 1
# Passage cap in characters (code points). §5/§8 of the proposal: four whole
# passages plus labels fit one observation.
PASSAGE_CAP = 700

DIALECTS = ("timed", "daily", "changelog", "plain")
SENSITIVITIES = ("private", "secret")
MANIFEST_SCHEMA_VERSION = 1

_MANIFEST_KEYS = {
    "schema_version", "name", "root", "room_uuid", "agent_uuid", "timezone",
    "sensitivity", "allow_remote_models", "include_suffixes", "dialect_rules",
    "month_languages", "command_tokens", "author_tokens", "file_overrides",
}
_OVERRIDE_KEYS = {
    "content_sha256", "force_boundary_offsets", "suppress_boundary_offsets",
    "pasted_ranges",
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ManifestError(ValueError):
    """A manifest that cannot be registered. The message names the field."""


@dataclass(frozen=True)
class DialectRule:
    prefix: str
    dialect: str


@dataclass(frozen=True)
class FileOverride:
    content_sha256: str
    force_boundary_offsets: tuple[int, ...] = ()
    suppress_boundary_offsets: tuple[int, ...] = ()
    pasted_ranges: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True)
class Manifest:
    name: str
    root: str
    room_uuid: UUID
    agent_uuid: UUID | None
    timezone: str
    sensitivity: str
    allow_remote_models: bool
    include_suffixes: tuple[str, ...]
    dialect_rules: tuple[DialectRule, ...]
    month_languages: tuple[str, ...]
    command_tokens: tuple[str, ...]
    author_tokens: dict[str, str] = field(default_factory=dict)
    file_overrides: dict[str, FileOverride] = field(default_factory=dict)


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise ManifestError(message)


def _normalize_prefix(prefix: Any) -> str:
    _require(isinstance(prefix, str), "dialect_rules[].prefix must be a string")
    if prefix == "":
        return ""
    _require(not prefix.startswith("/"), f"prefix {prefix!r} must be relative")
    parts = [p for p in prefix.split("/") if p not in ("", ".")]
    _require(".." not in parts, f"prefix {prefix!r} must not contain '..'")
    _require(bool(parts), f"prefix {prefix!r} is empty after normalization; use \"\"")
    return "/".join(parts) + "/"


def _normalize_relative_path(path: Any) -> str:
    _require(isinstance(path, str) and path != "", "file path must be a non-empty string")
    _require(not path.startswith("/"), f"file path {path!r} must be relative")
    norm = posixpath.normpath(path)
    _require(not norm.startswith("..") and norm != ".", f"file path {path!r} escapes the root")
    return norm


def _int_list(value: Any, what: str) -> tuple[int, ...]:
    _require(isinstance(value, list), f"{what} must be a list")
    out = []
    for v in value:
        _require(isinstance(v, int) and not isinstance(v, bool) and v >= 0,
                 f"{what} entries must be non-negative integers")
        out.append(v)
    _require(len(set(out)) == len(out), f"{what} has duplicates")
    return tuple(sorted(out))


def _parse_override(path: str, raw: Any) -> FileOverride:
    _require(isinstance(raw, dict), f"file_overrides[{path!r}] must be an object")
    unknown = set(raw) - _OVERRIDE_KEYS
    _require(not unknown, f"file_overrides[{path!r}] has unknown keys {sorted(unknown)}")
    sha = raw.get("content_sha256")
    _require(isinstance(sha, str) and bool(_SHA256.match(sha)),
             f"file_overrides[{path!r}].content_sha256 must be 64 lowercase hex")
    force = _int_list(raw.get("force_boundary_offsets", []),
                      f"file_overrides[{path!r}].force_boundary_offsets")
    suppress = _int_list(raw.get("suppress_boundary_offsets", []),
                         f"file_overrides[{path!r}].suppress_boundary_offsets")
    _require(not (set(force) & set(suppress)),
             f"file_overrides[{path!r}] forces and suppresses the same offset")
    ranges_raw = raw.get("pasted_ranges", [])
    _require(isinstance(ranges_raw, list), f"file_overrides[{path!r}].pasted_ranges must be a list")
    ranges: list[tuple[int, int]] = []
    for r in ranges_raw:
        _require(isinstance(r, list) and len(r) == 2
                 and all(isinstance(x, int) and not isinstance(x, bool) for x in r)
                 and 0 <= r[0] < r[1],
                 f"file_overrides[{path!r}].pasted_ranges entries must be [start,end) with start < end")
        ranges.append((r[0], r[1]))
    ranges.sort()
    for a, b in zip(ranges, ranges[1:]):
        _require(a[1] <= b[0], f"file_overrides[{path!r}].pasted_ranges overlap")
    for off in force:
        _require(not any(s <= off < e for s, e in ranges),
                 f"file_overrides[{path!r}] forces a boundary inside a pasted range")
    return FileOverride(sha, force, suppress, tuple(ranges))


def load_manifest(raw: Any, *, check_root: bool = True) -> Manifest:
    """Validate a manifest object completely. Raises ManifestError naming the
    first problem. Room/agent existence is the caller's (DB) check."""
    _require(isinstance(raw, dict), "manifest must be a JSON object")
    unknown = set(raw) - _MANIFEST_KEYS
    _require(not unknown, f"unknown manifest keys {sorted(unknown)}")
    _require(raw.get("schema_version") == MANIFEST_SCHEMA_VERSION,
             f"schema_version must be {MANIFEST_SCHEMA_VERSION}")

    name = raw.get("name")
    _require(isinstance(name, str) and bool(_NAME.match(name)),
             "name must be 1-64 characters of letters, digits, '.', '_' or '-'")

    root = raw.get("root")
    _require(isinstance(root, str) and os.path.isabs(root), "root must be an absolute path")
    if check_root:
        _require(os.path.isdir(root), f"root {root!r} is not an existing directory")
        root = os.path.realpath(root)
    else:
        root = os.path.normpath(root)

    try:
        room_uuid = UUID(str(raw.get("room_uuid")))
    except ValueError as exc:
        raise ManifestError("room_uuid must be a UUID") from exc
    agent_raw = raw.get("agent_uuid")
    try:
        agent_uuid = None if agent_raw is None else UUID(str(agent_raw))
    except ValueError as exc:
        raise ManifestError("agent_uuid must be a UUID or null") from exc

    tz = raw.get("timezone")
    _require(isinstance(tz, str) and tz != "", "timezone is required (an IANA name)")
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ManifestError(f"timezone {tz!r} is not a known IANA zone") from exc

    sensitivity = raw.get("sensitivity", "private")
    _require(sensitivity in SENSITIVITIES, f"sensitivity must be one of {SENSITIVITIES}")
    allow_remote = raw.get("allow_remote_models", False)
    _require(isinstance(allow_remote, bool), "allow_remote_models must be a boolean")
    _require(not (allow_remote and sensitivity == "secret"),
             "a secret source cannot allow remote models")

    suffixes = raw.get("include_suffixes", [".txt"])
    _require(isinstance(suffixes, list) and all(isinstance(s, str) for s in suffixes),
             "include_suffixes must be a list of strings")
    _require(len(set(suffixes)) == len(suffixes), "include_suffixes has duplicates")

    rules_raw = raw.get("dialect_rules", [])
    _require(isinstance(rules_raw, list), "dialect_rules must be a list")
    rules: list[DialectRule] = []
    seen_prefixes: set[str] = set()
    for r in rules_raw:
        _require(isinstance(r, dict) and set(r) == {"prefix", "dialect"},
                 "dialect_rules entries must be {prefix, dialect}")
        prefix = _normalize_prefix(r["prefix"])
        _require(r["dialect"] in DIALECTS, f"unknown dialect {r['dialect']!r}")
        _require(prefix not in seen_prefixes, f"duplicate prefix {prefix!r}")
        seen_prefixes.add(prefix)
        rules.append(DialectRule(prefix, r["dialect"]))

    languages = raw.get("month_languages", [])
    _require(isinstance(languages, list) and all(isinstance(x, str) for x in languages),
             "month_languages must be a list of language tags")
    _require(len(set(languages)) == len(languages), "month_languages has duplicates")
    month_tables(tuple(languages))  # validates tags and conflicts

    commands = raw.get("command_tokens", [])
    _require(isinstance(commands, list) and all(isinstance(c, str) and c and not any(ch.isspace() for ch in c) for c in commands),
             "command_tokens must be non-empty tokens without whitespace")

    authors = raw.get("author_tokens", {})
    _require(isinstance(authors, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in authors.items()),
             "author_tokens must map token to label")

    overrides_raw = raw.get("file_overrides", {})
    _require(isinstance(overrides_raw, dict), "file_overrides must be an object")
    overrides: dict[str, FileOverride] = {}
    for path, ov in overrides_raw.items():
        norm = _normalize_relative_path(path)
        _require(norm not in overrides, f"file_overrides has two entries for {norm!r}")
        overrides[norm] = _parse_override(norm, ov)

    return Manifest(
        name=name, root=root, room_uuid=room_uuid, agent_uuid=agent_uuid,
        timezone=tz, sensitivity=sensitivity, allow_remote_models=allow_remote,
        include_suffixes=tuple(suffixes), dialect_rules=tuple(rules),
        month_languages=tuple(languages), command_tokens=tuple(commands),
        author_tokens=dict(authors), file_overrides=overrides,
    )


def month_tables(languages: tuple[str, ...]) -> dict[str, int]:
    """casefolded month token -> month number, over wide and abbreviated
    names in both format and stand-alone contexts, trailing period stripped.
    Materialized at register time so the fingerprint covers it and a Babel
    upgrade cannot silently change a parse. A token that two languages map to
    different months is rejected rather than guessed."""
    from babel import Locale, UnknownLocaleError
    from babel.dates import get_month_names

    table: dict[str, int] = {}
    for lang in languages:
        try:
            Locale.parse(lang.replace("-", "_"))
        except (UnknownLocaleError, ValueError) as exc:
            raise ManifestError(f"unknown month language {lang!r}") from exc
        for width in ("wide", "abbreviated"):
            for context in ("format", "stand-alone"):
                names = get_month_names(width, context, lang.replace("-", "_"))
                for month, name in names.items():
                    token = name.rstrip(".").casefold()
                    if not token:
                        continue
                    prior = table.get(token)
                    _require(prior is None or prior == month,
                             f"month token {token!r} means month {prior} and {month} "
                             "in the configured languages")
                    table[token] = month
    return dict(sorted(table.items()))


def dialect_for(relative_path: str, rules: tuple[DialectRule, ...] | list[dict]) -> str:
    """Longest matching directory prefix, on path-component boundaries;
    unmatched files are `plain`. `rules` may be DialectRule objects or the
    serialized dicts stored in a parser config."""
    best_len = -1
    best = "plain"
    for rule in rules:
        prefix = rule.prefix if isinstance(rule, DialectRule) else rule["prefix"]
        dialect = rule.dialect if isinstance(rule, DialectRule) else rule["dialect"]
        if (prefix == "" or relative_path.startswith(prefix)) and len(prefix) > best_len:
            best_len = len(prefix)
            best = dialect
    return best


def build_parser_config(manifest: Manifest) -> dict[str, Any]:
    """The immutable configuration a generation is parsed with. Everything
    that changes parse output is in here; policy fields are not."""
    return {
        "parser_version": PARSER_VERSION,
        "identifier_version": IDENTIFIER_VERSION,
        "passage_cap": PASSAGE_CAP,
        "timezone": manifest.timezone,
        "dialect_rules": [{"prefix": r.prefix, "dialect": r.dialect}
                          for r in sorted(manifest.dialect_rules, key=lambda r: r.prefix)],
        "month_table": month_tables(manifest.month_languages),
        "command_tokens": sorted(manifest.command_tokens),
        "author_tokens": dict(sorted(manifest.author_tokens.items())),
        "file_overrides": {
            path: {
                "content_sha256": ov.content_sha256,
                "force_boundary_offsets": list(ov.force_boundary_offsets),
                "suppress_boundary_offsets": list(ov.suppress_boundary_offsets),
                "pasted_ranges": [list(r) for r in ov.pasted_ranges],
            }
            for path, ov in sorted(manifest.file_overrides.items())
        },
    }


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def config_fingerprint(parser_config: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(parser_config).encode("utf-8")).hexdigest()


def file_included(relative_path: str, suffixes: tuple[str, ...] | list[str]) -> bool:
    """Case-sensitive suffix match; "" admits extensionless files."""
    base = posixpath.basename(relative_path)
    for suffix in suffixes:
        if suffix == "":
            if "." not in base.lstrip("."):
                return True
        elif base.endswith(suffix):
            return True
    return False


def manifest_to_json(manifest: Manifest) -> dict[str, Any]:
    """The normalized manifest as stored in `diary_source.config`."""
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "name": manifest.name,
        "root": manifest.root,
        "room_uuid": str(manifest.room_uuid),
        "agent_uuid": str(manifest.agent_uuid) if manifest.agent_uuid else None,
        "timezone": manifest.timezone,
        "sensitivity": manifest.sensitivity,
        "allow_remote_models": manifest.allow_remote_models,
        "include_suffixes": list(manifest.include_suffixes),
        "dialect_rules": [{"prefix": r.prefix, "dialect": r.dialect} for r in manifest.dialect_rules],
        "month_languages": list(manifest.month_languages),
        "command_tokens": list(manifest.command_tokens),
        "author_tokens": dict(manifest.author_tokens),
        "file_overrides": {
            path: {
                "content_sha256": ov.content_sha256,
                "force_boundary_offsets": list(ov.force_boundary_offsets),
                "suppress_boundary_offsets": list(ov.suppress_boundary_offsets),
                "pasted_ranges": [list(r) for r in ov.pasted_ranges],
            }
            for path, ov in manifest.file_overrides.items()
        },
    }
