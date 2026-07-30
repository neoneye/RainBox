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
from user_profile.calibration import format_calibration
from user_profile.export import SECTION_TAGS, collect_sections, export_settings
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
    section reproduces the prompt string byte-for-byte. A reimplementation
    that merely looked similar would fail here."""
    doc = collect_sections(PROFILE, ["profile"])
    assert json.dumps(doc["user_settings_json"], ensure_ascii=False,
                      indent=2) == format_identity_block(PROFILE)


def test_language_section_is_the_prompt_candidate_list():
    doc = collect_sections(PROFILE, ["languages"])
    assert (doc["user_settings_languages_json"]
            == declared_language_candidates(PROFILE))
    # No index field: the array already orders the rows.
    assert all("position" not in row
               for row in doc["user_settings_languages_json"])


def test_calibration_rows_are_the_block_jsonl():
    doc = collect_sections(PROFILE, ["calibration"])
    body = format_calibration(PROFILE)
    jsonl = [json.loads(line) for line in body.splitlines()
             if line.strip().startswith("{")]
    assert doc["knowledge_calibration"]["rows"] == jsonl


def test_sections_are_keyed_by_their_prompt_tag():
    doc = collect_sections(PROFILE)
    assert set(doc) == set(SECTION_TAGS.values())


# --- section selection --------------------------------------------------------


def test_default_includes_every_section():
    assert set(collect_sections(PROFILE)) == {
        "user_settings_json", "user_settings_languages_json",
        "knowledge_calibration"}


def test_sections_can_be_narrowed():
    doc = collect_sections(PROFILE, ["languages"])
    assert set(doc) == {"user_settings_languages_json"}


def test_sections_keep_prompt_order_regardless_of_request_order():
    doc = collect_sections(PROFILE, ["calibration", "profile"])
    assert list(doc) == ["user_settings_json", "knowledge_calibration"]


def test_empty_profile_yields_an_empty_document():
    """A section the assistant would omit entirely must not appear as an empty
    container — that would claim the prompt carries a block it does not."""
    assert collect_sections({"data": {}}) == {}
    assert export_settings({"data": {}}, fmt="json") == "{}"


# --- formats ------------------------------------------------------------------


def test_json_parses_and_carries_every_section():
    out = json.loads(export_settings(PROFILE, fmt="json"))
    assert set(out) == set(SECTION_TAGS.values())


def test_yaml_parses_to_the_same_document_as_json():
    assert (yaml.safe_load(export_settings(PROFILE, fmt="yaml"))
            == json.loads(export_settings(PROFILE, fmt="json")))


def test_xml_parses_and_nests_by_section():
    root = ET.fromstring(export_settings(PROFILE, fmt="xml"))
    assert root.tag == "user_settings"
    assert [c.tag for c in root] == list(SECTION_TAGS.values())
    langs = root.find("user_settings_languages_json")
    assert langs is not None
    codes = [item.findtext("code") for item in langs]
    assert codes == ["en-US", "da"]


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
