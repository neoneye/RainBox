"""Manifest validation, month tables, dialect selection and fingerprints."""

import copy

import pytest

from diary.config import (
    ManifestError,
    build_parser_config,
    config_fingerprint,
    dialect_for,
    file_included,
    load_manifest,
    month_tables,
)

BASE = {
    "schema_version": 1,
    "name": "diary-fixture",
    "root": "/private/tmp/rainbox-diary-fixture",
    "room_uuid": "10000000-0000-4000-8000-000000000001",
    "agent_uuid": None,
    "timezone": "Europe/Copenhagen",
    "sensitivity": "private",
    "allow_remote_models": False,
    "include_suffixes": [".txt"],
    "dialect_rules": [
        {"prefix": "current/", "dialect": "timed"},
        {"prefix": "daily/", "dialect": "daily"},
        {"prefix": "archive", "dialect": "changelog"},
    ],
    "month_languages": ["en", "da", "de"],
    "command_tokens": ["/goal"],
    "author_tokens": {"kreese": "operator"},
    "file_overrides": {},
}


def manifest(**changes):
    raw = copy.deepcopy(BASE)
    raw.update(changes)
    return load_manifest(raw, check_root=False)


def test_valid_manifest_normalizes_prefixes():
    m = manifest()
    assert [r.prefix for r in m.dialect_rules] == ["current/", "daily/", "archive/"]
    assert m.sensitivity == "private" and m.allow_remote_models is False


@pytest.mark.parametrize("changes, fragment", [
    ({"schema_version": 2}, "schema_version"),
    ({"surprise": 1}, "unknown manifest keys"),
    ({"root": "relative/path"}, "absolute"),
    ({"room_uuid": "not-a-uuid"}, "room_uuid"),
    ({"timezone": "Mars/Olympus"}, "IANA"),
    ({"sensitivity": "public"}, "sensitivity"),
    ({"sensitivity": "secret", "allow_remote_models": True}, "secret source"),
    ({"dialect_rules": [{"prefix": "../up/", "dialect": "timed"}]}, ".."),
    ({"dialect_rules": [{"prefix": "/abs/", "dialect": "timed"}]}, "relative"),
    ({"dialect_rules": [{"prefix": "a/", "dialect": "poem"}]}, "unknown dialect"),
    ({"dialect_rules": [{"prefix": "a", "dialect": "timed"},
                        {"prefix": "a/", "dialect": "daily"}]}, "duplicate prefix"),
    ({"month_languages": ["xx-notalanguage"]}, "month language"),
    ({"command_tokens": ["two words"]}, "command_tokens"),
])
def test_invalid_manifests_name_the_field(changes, fragment):
    with pytest.raises(ManifestError, match=fragment):
        manifest(**changes)


def test_override_validation():
    sha = "a" * 64
    ok = manifest(file_overrides={"current/2027.txt": {
        "content_sha256": sha, "force_boundary_offsets": [10],
        "suppress_boundary_offsets": [20], "pasted_ranges": [[30, 40], [50, 60]]}})
    assert ok.file_overrides["current/2027.txt"].pasted_ranges == ((30, 40), (50, 60))
    with pytest.raises(ManifestError, match="overlap"):
        manifest(file_overrides={"x.txt": {"content_sha256": sha,
                                           "pasted_ranges": [[0, 10], [5, 20]]}})
    with pytest.raises(ManifestError, match="inside a pasted range"):
        manifest(file_overrides={"x.txt": {"content_sha256": sha,
                                           "force_boundary_offsets": [5],
                                           "pasted_ranges": [[0, 10]]}})
    with pytest.raises(ManifestError, match="same offset"):
        manifest(file_overrides={"x.txt": {"content_sha256": sha,
                                           "force_boundary_offsets": [5],
                                           "suppress_boundary_offsets": [5]}})
    with pytest.raises(ManifestError, match="escapes"):
        manifest(file_overrides={"../x.txt": {"content_sha256": sha}})


def test_month_tables_cover_the_fixture_languages():
    table = month_tables(("en", "da", "de"))
    assert table["juli"] == 7 and table["jul"] == 7 and table["märz"] == 3
    assert table["maj"] == 5 and table["mai"] == 5 and table["sept"] == 9
    assert all(k == k.casefold() and not k.endswith(".") for k in table)


def test_dialect_for_uses_longest_component_prefix():
    rules = manifest(dialect_rules=[
        {"prefix": "", "dialect": "timed"},
        {"prefix": "daily/", "dialect": "daily"},
        {"prefix": "daily/old/", "dialect": "changelog"},
    ]).dialect_rules
    assert dialect_for("x.txt", rules) == "timed"
    assert dialect_for("daily/2027_07_26.txt", rules) == "daily"
    assert dialect_for("daily/old/ChangeLog.txt", rules) == "changelog"
    assert dialect_for("dailyish/x.txt", rules) == "timed"   # component boundary
    assert dialect_for("x.txt", ()) == "plain"


def test_file_included_suffixes():
    assert file_included("a/b.txt", [".txt"])
    assert not file_included("a/b.TXT", [".txt"])
    assert not file_included("a/README", [".txt"])
    assert file_included("a/README", [".txt", ""])


def test_fingerprint_covers_parse_inputs_not_policy():
    base = config_fingerprint(build_parser_config(manifest()))
    assert base == config_fingerprint(build_parser_config(manifest()))
    assert base != config_fingerprint(build_parser_config(manifest(timezone="UTC")))
    assert base != config_fingerprint(build_parser_config(manifest(month_languages=["en"])))
    assert base == config_fingerprint(build_parser_config(manifest(allow_remote_models=True)))
    assert base == config_fingerprint(build_parser_config(manifest(sensitivity="secret")))
