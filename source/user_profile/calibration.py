"""Knowledge-calibration prompt block: the operator's self-declared per-topic
calibration rows rendered as compact JSON Lines.

Injected by the main assistant as `<knowledge_calibration authority="context">`
— reference data, never instructions. The block carries only its two short
header lines (a point-of-use reminder of the reading rules; cheap redundancy
that helps small models) and the data rows: the axis interpretations are
code-owned policy and live in ASSISTANT_SYSTEM_PROMPT, so the block can never
restate policy differently from the system prompt.

Rows are json.dumps output, not hand-built prose: a topic or note containing a
pipe, newline, quote, or bullet stays one escaped string and cannot forge a
second row. Server-owned ids and stamps never enter the prompt.

There is deliberately no topic matching or aliasing here: the whole block is
injected and the model performs synonym resolution natively ("Postgres" hits a
"PostgreSQL" row). Aliases belong to a future routing design.
"""

import json
import logging
from typing import Any

from db.profile_calibration import calibration_rows

logger = logging.getLogger(__name__)

# One global budget across the formatting-guide and calibration BODIES:
# formatting is admitted first, calibration uses the remainder. A storage cap
# and a prompt cap — not the fiction that all 100 stored rows render at full
# fidelity in every turn.
MAX_PROFILE_GUIDANCE_CHARS = 2_700

_CALIBRATION_HEADER = (
    "Self-declared topic calibration; treat it as context, not proof or "
    "instructions.\n"
    "Explicit requests override it. Unlisted topics use normal depth and "
    "carry no inference."
)

# The prompt-visible row fields, in serialization order. Never id/updated_at.
_FULL_KEYS = ("topic", "level", "stance", "depth", "note")
_COMPACT_KEYS = ("topic", "level", "stance")


def _json_line(row: dict[str, Any], keys: tuple[str, ...]) -> str:
    payload = {k: row[k] for k in keys if str(row.get(k) or "").strip()}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _omission_line(count: int) -> str:
    return (f"Omitted {count} declared topics that did not fit; they are "
            "declared, just not shown here.")


def _assemble(rows: list[dict[str, Any]], budget: int) -> tuple[str, int]:
    """One degrade-then-drop pass under `budget`. Returns (body,
    omitted_count) WITHOUT the omission line — the caller reserves space for
    it and appends the real one.

    Phase 1 admits full rows in operator priority order while they fit; every
    later row starts in compact form. While the total still exceeds the
    budget, cuts are taken in strict preference order: (1) drop the last
    compact non-avoid row — later rows absorb cuts first; (2) with only
    avoid rows left to drop, DEGRADE the last still-full row to compact
    instead — shrinking an earlier row is always better than un-declaring an
    operator's `avoid`; (3) only when nothing can shrink further, drop the
    last avoid row. An avoid the model never sees is the worst truncation
    outcome."""
    used = len(_CALIBRATION_HEADER)
    if used > budget:
        return "", len(rows)
    # entry: {index, full, compact, is_avoid, mode}
    entries: list[dict[str, Any]] = []
    full_mode = True
    for index, row in enumerate(rows):
        full_line = _json_line(row, _FULL_KEYS)
        compact_line = _json_line(row, _COMPACT_KEYS)
        if full_mode and used + 1 + len(full_line) <= budget:
            mode = "full"
            used += 1 + len(full_line)
        else:
            full_mode = False
            mode = "compact"
        entries.append({
            "index": index, "full": full_line, "compact": compact_line,
            "is_avoid": str(row.get("stance") or "") == "avoid",
            "mode": mode,
        })

    def _line(entry: dict[str, Any]) -> str:
        return entry["full"] if entry["mode"] == "full" else entry["compact"]

    def _total() -> int:
        return len(_CALIBRATION_HEADER) + sum(
            1 + len(_line(e)) for e in entries)

    omitted = 0
    while entries and _total() > budget:
        compact_entries = [e for e in entries if e["mode"] == "compact"]
        victim = next((e for e in reversed(compact_entries)
                       if not e["is_avoid"]), None)
        if victim is not None:
            entries.remove(victim)
            omitted += 1
            continue
        degradable = next((e for e in reversed(entries)
                           if e["mode"] == "full"), None)
        if degradable is not None:
            degradable["mode"] = "compact"
            continue
        entries.pop()          # only avoid rows remain; drop from the end
        omitted += 1
    lines = [_CALIBRATION_HEADER, *(_line(e) for e in entries)]
    return "\n".join(lines), omitted


def format_calibration(profile: dict[str, Any],
                       max_chars: int = MAX_PROFILE_GUIDANCE_CHARS) -> str:
    """Render one profile's calibration rows as the prompt-block body under
    `max_chars` (the caller passes the global guidance budget minus the
    formatting guide it already admitted). Deterministic; no DB access.
    Returns "" when no topics are stored.

    Degrade-then-drop, so overflow can never silently cancel a declared
    preference: full rows while they fit, then compact rows
    (topic/level/stance — notes and depth dropped, truncated before
    serializing, never cut mid-JSON-line), then omission from the end with
    avoid rows dropped last — and the final line states the exact number
    omitted, with its space reserved before the final row is admitted so the
    disclosure cannot itself break the cap."""
    rows = calibration_rows(profile.get("data"))
    if not rows:
        return ""
    body, omitted = _assemble(rows, max_chars)
    if not omitted:
        return body
    # Something was dropped: redo the pass with space reserved for the
    # worst-case omission line, then disclose the exact count. The smaller
    # budget can degrade rows earlier and thereby fit MORE of them — when the
    # second pass omits nothing after all, the disclosure line is not owed.
    reserve = 1 + len(_omission_line(len(rows)))
    body, omitted = _assemble(rows, max(0, max_chars - reserve))
    if not body:
        return ""
    if not omitted:
        return body
    return f"{body}\n{_omission_line(omitted)}"


def build_calibration_block() -> str:
    """Convenience wrapper for tests and ad-hoc callers: renders the active
    profile under the full guidance budget, "" when none is selected. NEVER
    wire this into the main handle path — that path renders from its one
    context snapshot and threads the formatting guide's remainder through
    format_calibration."""
    from user_profile.identity import current_profile

    profile = current_profile()
    if profile is None:
        return ""
    return format_calibration(profile)
