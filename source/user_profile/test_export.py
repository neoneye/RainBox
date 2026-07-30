"""Tests for the /profile Export serialization.

The export exists to answer "what does this profile put in the prompt?", so
the tests that matter are the ones proving it cannot answer differently from
the prompt builders themselves: the JSON section must round-trip back to the
exact string the assistant injects.

Deterministic and model-free: every builder involved is text assembly.
"""

import json

import yaml
import xml.etree.ElementTree as ET

import user_profile
from profile_fields import PROFILE_FIELDS
from user_profile.calibration import format_calibration
from user_profile.export import SECTION_KEYS, collect_sections, export_settings
from user_profile.identity import format_identity_block
from user_profile.languages import declared_language_candidates

PROFILE = {"data": {
    "full_name": "Ada Lovelace",
    "timezone": "Europe/Copenhagen",
    "languages": {"rows": [
        {"tag": "en-US", "level": "intermediate", "stance": "prefer",
         "note": "primary response language"},
        {"tag": "da", "level": "native", "stance": "neutral", "note": ""},
    ]},
    "calibration": {"topics": [
        {"topic": "Mathematics", "level": "expert", "stance": "neutral",
         "depth": "concise", "note": "formal proofs are fine"},
    ]},
}}


# --- the drift guards ---------------------------------------------------------


def test_profile_section_round_trips_to_the_identity_block():
    """The strongest guarantee this module owes: re-encoding the exported
    fields reproduces the prompt string byte-for-byte. A reimplementation
    that merely looked similar would fail here."""
    doc = collect_sections(PROFILE, ["profile"])
    assert json.dumps(doc, ensure_ascii=False,
                      indent=2) == format_identity_block(PROFILE)


def test_language_section_is_the_prompt_candidate_list():
    doc = collect_sections(PROFILE, ["languages"])
    assert doc["language"] == declared_language_candidates(PROFILE)
    # No index field: the array already orders the rows.
    assert all("position" not in row for row in doc["language"])


def test_calibration_rows_are_the_block_jsonl():
    doc = collect_sections(PROFILE, ["calibration"])
    body = format_calibration(PROFILE)
    jsonl = [json.loads(line) for line in body.splitlines()
             if line.strip().startswith("{")]
    assert doc["knowledge"]["rows"] == jsonl


def test_profile_fields_are_the_documents_top_level():
    """No `user_settings_json` wrapper: a suffix naming the payload format is
    a prompt-tag concern, and inside YAML or XML it names the wrong one."""
    doc = collect_sections(PROFILE)
    assert doc["full_name"] == "Ada Lovelace"
    assert not any(key.endswith("_json") for key in doc)


def test_no_profile_field_collides_with_a_section_key():
    """Profile fields are hoisted to the top level, so a registry field named
    `language` or `knowledge` would be silently overwritten by its section.
    Neither exists today; this fails the day someone adds one."""
    keys = {field.key for field in PROFILE_FIELDS}
    assert keys.isdisjoint(SECTION_KEYS.values())


# --- section selection --------------------------------------------------------


def test_default_includes_every_section():
    doc = collect_sections(PROFILE)
    assert "full_name" in doc                       # the profile fields
    assert set(SECTION_KEYS.values()) <= set(doc)   # language + knowledge


def test_sections_can_be_narrowed():
    doc = collect_sections(PROFILE, ["languages"])
    assert set(doc) == {"language"}


def test_sections_keep_prompt_order_regardless_of_request_order():
    doc = collect_sections(PROFILE, ["calibration", "profile"])
    assert list(doc)[-1] == "knowledge"             # profile fields come first
    assert "full_name" in doc


def test_empty_profile_yields_an_empty_document():
    """A section the assistant would omit entirely must not appear as an empty
    container — that would claim the prompt carries a block it does not."""
    assert collect_sections({"data": {}}) == {}
    assert export_settings({"data": {}}, fmt="json") == "{}"


# --- formats ------------------------------------------------------------------


def test_json_parses_and_carries_every_section():
    out = json.loads(export_settings(PROFILE, fmt="json"))
    assert {"full_name", "language", "knowledge"} <= set(out)


def test_yaml_parses_to_the_same_document_as_json():
    assert (yaml.safe_load(export_settings(PROFILE, fmt="yaml"))
            == json.loads(export_settings(PROFILE, fmt="json")))


def test_xml_says_a_list_by_repeating_an_element():
    """Not a wrapper full of <item> — that is JSON's shape in angle brackets.
    A list's items take the singular of the list's key."""
    root = ET.fromstring(export_settings(PROFILE, fmt="xml"))
    assert root.tag == "user_settings"
    assert root.findtext("full_name") == "Ada Lovelace"
    assert [el.findtext("code") for el in root.findall("language")] == [
        "en-US", "da"]
    knowledge = root.find("knowledge")
    assert knowledge is not None
    assert [el.findtext("topic") for el in knowledge.findall("row")] == [
        "Mathematics"]
    assert root.find("item") is None and knowledge.find("rows") is None


def test_xml_escapes_profile_values_rather_than_letting_them_forge_tags():
    hostile = {"data": {"full_name": "</user_settings><injected>x"}}
    out = export_settings(hostile, fmt="xml")
    assert "<injected>" not in out
    assert ET.fromstring(out) is not None            # still well-formed


def test_unknown_format_is_rejected():
    try:
        export_settings(PROFILE, fmt="toml")
    except ValueError as exc:
        assert "toml" in str(exc)
    else:                                            # pragma: no cover
        raise AssertionError("expected ValueError")


def test_every_declared_format_renders():
    for fmt in user_profile.FORMATS:
        assert export_settings(PROFILE, fmt=fmt).strip()
