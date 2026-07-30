"""Serialize a profile's prompt blocks for side-by-side inspection.

The /profile page's Export dialog exists to answer one question: what does
this profile actually put in front of the assistant? So every format here is
derived from the strings the production builders emit — `format_identity_block`,
`declared_language_candidates`, `format_calibration` — parsed back into data
and re-rendered. Nothing re-implements a block. A second implementation would
drift, and an export that quietly disagrees with the prompt is worse than no
export at all.

The document does NOT key on the prompt tags. A tag like `user_settings_json`
carries its suffix because the model needs to know what the payload inside it
is; repeating that suffix as a key inside a YAML or XML document states the
wrong format, and inside a JSON document states the obvious. The profile's
own fields are the document's top level, with `language` and `knowledge`
beside them.

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

# The document key each section occupies. "profile" is absent on purpose: its
# fields ARE the top level, so a field reads as `full_name`, not
# `user_settings_json.full_name`. Nothing in the field registry may take one of
# these names — see test_no_profile_field_collides_with_a_section_key, which
# exists because `language` is an entirely plausible future field and hoisting
# would silently overwrite it.
SECTION_KEYS: dict[str, str] = {
    "languages": "language",
    "calibration": "knowledge",
}

_XML_ROOT = "user_settings"


def _parse_calibration(body: str) -> dict[str, Any]:
    """The calibration rows, plus the disclosure when some were dropped.

    The block is a prose preamble, then one JSON object per row, then — only
    when rows were dropped to fit the budget — a prose line saying how many.
    Neither prose line parses as JSON, so position tells them apart: prose
    before the first row is the preamble, prose after the last is the
    disclosure.

    The preamble is discarded. It is a fixed sentence present in every block,
    identical for every profile, and it says nothing about the profile being
    exported. The disclosure is kept precisely because it is not boilerplate:
    it means the prompt is not carrying everything the profile declares.
    """
    rows: list[Any] = []
    trailing: list[str] = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            if rows:
                trailing.append(line)
    out: dict[str, Any] = {"rows": rows}
    if trailing:
        out["omitted"] = " ".join(trailing)
    return out


def collect_sections(
    profile: dict[str, Any],
    sections: list[str] | tuple[str, ...] = SECTIONS,
    *,
    calibration_max_chars: int = MAX_PROFILE_GUIDANCE_CHARS,
) -> dict[str, Any]:
    """The requested blocks as one document, in prompt order.

    A section that renders empty is omitted rather than shown as `{}` or `[]`:
    the assistant omits the whole element in that case, and an empty container
    would suggest the prompt carries a block it does not.
    """
    doc: dict[str, Any] = {}
    for name in SECTIONS:              # SECTIONS, not the argument: prompt order
        if name not in sections:
            continue
        if name == "profile":
            doc.update(json.loads(format_identity_block(profile) or "{}"))
            continue
        if name == "languages":
            payload: Any = declared_language_candidates(profile)
        else:
            payload = _parse_calibration(
                format_calibration(profile, max_chars=calibration_max_chars))
            if not payload.get("rows"):
                payload = {}
        if payload:
            doc[SECTION_KEYS[name]] = payload
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


def _xml_item_name(key: str) -> str:
    """The element name for one entry of a list. XML says a list by repeating
    an element, so `language: [...]` becomes repeated `<language>` and
    `rows: [...]` becomes repeated `<row>` — not a wrapper full of `<item>`,
    which is JSON's shape wearing angle brackets. Naive de-pluralization is
    enough here: every key is ours and none is irregular."""
    return key[:-1] if len(key) > 1 and key.endswith("s") else key


def _append_xml(parent: ET.Element, key: str, value: Any) -> None:
    if isinstance(value, list):
        item = _xml_item_name(key)
        for entry in value:
            _append_xml(parent, item, entry)
        return
    node = ET.SubElement(parent, _xml_name(key))
    if isinstance(value, dict):
        for k, v in value.items():
            _append_xml(node, k, v)
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
