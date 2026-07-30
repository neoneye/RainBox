"""Serialize a profile's prompt blocks for side-by-side inspection.

The /profile page's Export dialog exists to answer one question: what does
this profile actually put in front of the assistant? So every format here is
derived from the strings the production builders emit — `format_identity_block`,
`declared_language_candidates`, `format_calibration` — parsed back into data
and re-rendered. Nothing re-implements a block. A second implementation would
drift, and an export that quietly disagrees with the prompt is worse than no
export at all.

The document keys on the prompt tag each section is injected under, so an
entry can be lined up against the block it becomes.

Deliberately absent: the profile's display name and uuid. The identity block
carries neither (see `identity.py`), and adding them here would make the
export show something the prompt never sends.

Caveat worth knowing when reading an overflowing calibration section: in a
live turn `format_calibration` receives the guidance budget *minus* whatever
the formatting guide already took, so a profile close to the cap can show one
more row here than the assistant would receive. Content is identical either
way; only the drop point moves.
"""

import json
import re
import xml.etree.ElementTree as ET
from typing import Any

import yaml

from user_profile.calibration import MAX_PROFILE_GUIDANCE_CHARS, format_calibration
from user_profile.identity import format_identity_block
from user_profile.languages import declared_language_candidates

FORMATS: tuple[str, ...] = ("json", "yaml", "xml")
SECTIONS: tuple[str, ...] = ("profile", "languages", "calibration")

# The prompt tag each section arrives under.
SECTION_TAGS: dict[str, str] = {
    "profile": "user_settings_json",
    "languages": "user_settings_languages_json",
    "calibration": "knowledge_calibration",
}

_XML_ROOT = "user_settings"
_XML_LIST_ITEM = "item"


def _parse_calibration(body: str) -> dict[str, Any]:
    """Split the calibration block into its three parts.

    The block is a prose preamble, then one JSON object per row, then — only
    when rows were dropped to fit the budget — a prose line disclosing how
    many. Both prose lines would fail to parse as JSON, so position tells them
    apart: prose before the first row is the preamble, prose after the last is
    the omission disclosure. Keeping them separate matters, because one is
    boilerplate present in every block and the other is a warning that the
    prompt is not carrying everything the profile declares.
    """
    preamble: list[str] = []
    rows: list[Any] = []
    trailing: list[str] = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            (trailing if rows else preamble).append(line)
    out: dict[str, Any] = {}
    if preamble:
        out["preamble"] = " ".join(preamble)
    out["rows"] = rows
    if trailing:
        out["omitted"] = " ".join(trailing)
    return out


def collect_sections(
    profile: dict[str, Any],
    sections: list[str] | tuple[str, ...] = SECTIONS,
    *,
    calibration_max_chars: int = MAX_PROFILE_GUIDANCE_CHARS,
) -> dict[str, Any]:
    """The requested blocks as data, keyed by prompt tag, in prompt order.

    A section that renders empty is omitted rather than shown as `{}` or `[]`:
    the assistant omits the whole element in that case, and an empty container
    would suggest the prompt carries a block it does not.
    """
    doc: dict[str, Any] = {}
    for name in SECTIONS:              # SECTIONS, not the argument: prompt order
        if name not in sections:
            continue
        tag = SECTION_TAGS[name]
        if name == "profile":
            payload = json.loads(format_identity_block(profile) or "{}")
        elif name == "languages":
            payload = declared_language_candidates(profile)
        else:
            payload = _parse_calibration(
                format_calibration(profile, max_chars=calibration_max_chars))
            if not payload.get("rows"):
                payload = {}
        if payload:
            doc[tag] = payload
    return doc


def _xml_name(key: str) -> str:
    """A key rendered as an XML element name. Profile keys are registry
    identifiers so this is normally a no-op, but `number_format.comment` shows
    that keys are not guaranteed to be bare identifiers, and an invalid name
    would produce a document no parser accepts."""
    name = re.sub(r"[^\w.\-]", "_", str(key))
    if not name or not (name[0].isalpha() or name[0] == "_"):
        name = "_" + name
    return name


def _append_xml(parent: ET.Element, key: str, value: Any) -> None:
    node = ET.SubElement(parent, _xml_name(key))
    if isinstance(value, dict):
        for k, v in value.items():
            _append_xml(node, k, v)
    elif isinstance(value, list):
        for item in value:
            _append_xml(node, _XML_LIST_ITEM, item)
    elif value is not None:
        # ElementTree escapes the text, so a profile value cannot forge a tag.
        node.text = str(value)


def render(doc: dict[str, Any], fmt: str) -> str:
    """Render a collected document in one of FORMATS."""
    if fmt == "json":
        return json.dumps(doc, ensure_ascii=False, indent=2)
    if fmt == "yaml":
        return yaml.safe_dump(
            doc, sort_keys=False, allow_unicode=True, default_flow_style=False
        ).rstrip("\n")
    if fmt == "xml":
        root = ET.Element(_XML_ROOT)
        for key, value in doc.items():
            _append_xml(root, key, value)
        ET.indent(root, space="  ")
        return ET.tostring(root, encoding="unicode")
    raise ValueError(f"unknown export format: {fmt!r}")


def export_settings(
    profile: dict[str, Any],
    *,
    sections: list[str] | tuple[str, ...] = SECTIONS,
    fmt: str = "json",
    calibration_max_chars: int = MAX_PROFILE_GUIDANCE_CHARS,
) -> str:
    """One profile's selected prompt blocks, serialized in `fmt`."""
    return render(
        collect_sections(profile, sections,
                         calibration_max_chars=calibration_max_chars),
        fmt,
    )
