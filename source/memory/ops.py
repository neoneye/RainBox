"""Memory operations: parse explicit user commands like "remember that …",
"forget …", "confirm that …", "correct that OLD -> NEW", "what do you
remember about …", "why do you remember …", and dispatch them against
db.MemoryClaim / db.MemoryEvidence.

Wired into QueryAgent.handle as a wedge BEFORE the Q&A path so memory
commands aren't accidentally answered by the curated registry.
"""

import re
from dataclasses import dataclass
from typing import Any

import db
from db import MemoryClaim, MemoryEvidence
from agents.query_handlers import QueryContext
from memory.embeddings import refresh_claim_embedding


@dataclass(frozen=True)
class MemoryCommand:
    """A parsed memory operation. `text` carries the operand; `new_text`
    is the right-hand side of a `correct OLD -> NEW`."""

    kind: str  # "remember" | "forget" | "confirm" | "correct" | "recall" | "explain"
    text: str = ""
    new_text: str = ""


# Each pattern returns the named groups its kind needs. The trailing `?` in
# question commands is consumed by the regex itself so it doesn't leak into
# the captured text.
_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^\s*correct\s+that\s+(?P<old>.+?)\s*->\s*(?P<new>.+?)\s*$", re.I), "correct"),
    (re.compile(r"^\s*confirm\s+that\s+(?P<text>.+?)\s*$", re.I), "confirm"),
    (re.compile(r"^\s*remember\s+that\s+(?P<text>.+?)\s*$", re.I), "remember"),
    # Negative lookahead: don't treat a trailing bare `that` as the operand —
    # otherwise "remember that" alone gets parsed as remembering the word "that".
    (re.compile(r"^\s*remember\s+(?!that\s*$)(?P<text>.+?)\s*$", re.I), "remember"),
    (re.compile(r"^\s*forget\s+that\s+(?P<text>.+?)\s*$", re.I), "forget"),
    (re.compile(r"^\s*forget\s+(?!that\s*$)(?P<text>.+?)\s*$", re.I), "forget"),
    (re.compile(r"^\s*what\s+do\s+you\s+remember\s+about\s+(?P<text>.+?)\s*\??\s*$", re.I), "recall_about"),
    (re.compile(r"^\s*what\s+do\s+you\s+remember\s*\??\s*$", re.I), "recall_all"),
    (re.compile(r"^\s*why\s+did\s+you\s+remember\s+that\s*\??\s*$", re.I), "used"),
    (re.compile(r"^\s*why\s+did\s+you\s+say\s+that\s*\??\s*$", re.I), "used"),
    (re.compile(r"^\s*which\s+memor(?:y|ies)\s+did\s+you\s+use\s*\??\s*$", re.I), "used"),
    (re.compile(r"^\s*why\s+do\s+you\s+remember\s+(?P<text>.+?)\s*\??\s*$", re.I), "explain"),
]


def parse_memory_command(text: str) -> "MemoryCommand | None":
    """Return a MemoryCommand if `text` matches one of the supported
    patterns; otherwise None. Matching is case-insensitive and tolerant
    of leading/trailing whitespace, but otherwise exact (no fuzzy verbs)
    so accidental text never gets written into memory."""
    for pat, kind in _PATTERNS:
        m = pat.match(text)
        if not m:
            continue
        if kind == "correct":
            return MemoryCommand(
                kind="correct",
                text=m.group("old").strip(),
                new_text=m.group("new").strip(),
            )
        if kind == "recall_all":
            return MemoryCommand(kind="recall", text="")
        if kind == "recall_about":
            return MemoryCommand(kind="recall", text=m.group("text").strip())
        if kind == "used":
            return MemoryCommand(kind="used", text="")
        # remember / forget / confirm / explain — single text operand.
        return MemoryCommand(kind=kind if kind != "explain" else "explain",
                             text=m.group("text").strip())
    return None


def normalize_memory_text(text: str) -> str:
    """Lower-case, strip surrounding whitespace, collapse internal whitespace.
    Used for both exact-match lookup and substring-topic recall."""
    return " ".join(text.lower().strip().split())


def find_memory_matches(
    text: str,
    status: str | None = "active",
) -> list[MemoryClaim]:
    """Return claims whose `text` (after normalization) equals the
    normalized input. `status` filters by lifecycle (default "active");
    pass None to ignore status."""
    norm = normalize_memory_text(text)
    q = db.db.session.query(MemoryClaim)
    if status is not None:
        q = q.filter(MemoryClaim.status == status)
    return [c for c in q.all() if normalize_memory_text(c.text) == norm]


def _find_memory_by_substring(
    topic: str,
    status: str = "active",
) -> list[MemoryClaim]:
    """Return claims whose normalized `text` contains the normalized topic.
    Used for `what do you remember about <topic>`."""
    norm = normalize_memory_text(topic)
    q = (
        db.db.session.query(MemoryClaim)
        .filter(MemoryClaim.status == status)
        .order_by(MemoryClaim.id.asc())
    )
    return [c for c in q.all() if norm in normalize_memory_text(c.text)]


def _human_message_uuid(ctx: QueryContext) -> str | None:
    """Pull the triggering message UUID out of the QueryContext payload,
    or None if absent. Used for evidence's source_id."""
    raw = ctx.payload.get("message_uuid")
    return str(raw) if raw else None


def _handle_remember(ctx: QueryContext, text: str) -> str:
    result = db.record_belief(
        actor="explicit_human_command", scope="global", kind="fact",
        text=text, confidence=1.0, sensitivity="private",
        evidence={
            "provenance": "confirmed_by_user",
            "source_type": "manual",
            "source_id": _human_message_uuid(ctx),
            "excerpt": ctx.query,
        },
    )
    if result.outcome == "refused_tombstone":
        return f"I previously rejected that; not re-adding it. ({result.reason})"
    if result.claim is not None:
        refresh_claim_embedding(result.claim)
    return f"Remembered: {text}"


def _handle_forget(ctx: QueryContext, text: str) -> str:
    matches = find_memory_matches(text, status="active")
    if not matches:
        return f"I don't have anything matching {text!r} in active memory."
    if len(matches) > 1:
        return (
            f"Multiple active memories match {text!r} — please be more specific "
            f"(found {len(matches)})."
        )
    claim = matches[0]
    db.reject_memory(
        claim.uuid,
        evidence_args=dict(
            provenance="confirmed_by_user",
            source_type="chat_message",
            source_id=_human_message_uuid(ctx),
            excerpt=ctx.query,
        ),
    )
    refresh_claim_embedding(claim)  # now rejected → prunes its embedding
    return f"Forgot: {claim.text}"


def _handle_confirm(ctx: QueryContext, text: str) -> str:
    # Look at both candidate and active rows; either path adds confirmation
    # evidence and asserts status=active + confidence=1.0.
    matches = (
        find_memory_matches(text, status="candidate")
        + find_memory_matches(text, status="active")
    )
    if not matches:
        # No prior memory — confirm degenerates to remember.
        return _handle_remember(ctx, text)
    claim = matches[0]
    if claim.conflicts_with_uuid is not None:
        # A conflict candidate must be resolved (supersede/reject/not_conflict/
        # scoped_exception), not blindly activated — otherwise two conflicting
        # beliefs go active with a dangling conflict pointer.
        return (
            f"{text!r} is a conflict candidate; resolve the conflict on the "
            f"/memory page (supersede / reject / not a conflict / scoped "
            f"exception) instead of confirming it here."
        )
    claim.status = "active"
    claim.confidence = 1.0
    db.db.session.commit()
    db.add_memory_evidence(
        memory_uuid=claim.uuid,
        provenance="confirmed_by_user",
        source_type="chat_message",
        source_id=_human_message_uuid(ctx),
        excerpt=ctx.query,
    )
    refresh_claim_embedding(claim)
    return f"Confirmed: {claim.text}"


def _handle_correct(ctx: QueryContext, old_text: str, new_text: str) -> str:
    matches = find_memory_matches(old_text, status="active") + find_memory_matches(
        old_text, status="candidate"
    )
    if not matches:
        return f"I don't have anything matching {old_text!r} to correct."
    if len(matches) > 1:
        return (
            f"Multiple memories match {old_text!r} — please be more specific "
            f"before I correct one."
        )
    old = matches[0]
    old_text_snapshot = old.text  # capture before session state may change
    try:
        new = db.correct_belief(
            old.uuid, new_text,
            actor="explicit_human_command",
            evidence={
                "provenance": "confirmed_by_user",
                "source_type": "manual",
                "source_id": _human_message_uuid(ctx),
                "excerpt": ctx.query,
            },
        )
    except ValueError as exc:
        db.db.session.rollback()
        return f"Could not correct {old_text_snapshot!r} → {new_text!r}: {exc}"
    refresh_claim_embedding(new)
    refresh_claim_embedding(db.get_memory_claim(old.uuid))  # now superseded -> prune
    return f"Corrected: {old_text_snapshot} → {new_text}"


def _handle_recall(ctx: QueryContext, topic: str) -> str:
    if topic:
        matches = _find_memory_by_substring(topic, status="active")
        if not matches:
            return f"I don't have any active memory about {topic!r}."
        bullets = "\n".join(
            f"- ({c.sensitivity}) {c.text}" for c in matches
        )
        return f"Memories about {topic!r}:\n{bullets}"
    # No topic: list every active memory.
    all_active = (
        db.db.session.query(MemoryClaim)
        .filter(MemoryClaim.status == "active")
        .order_by(MemoryClaim.id.asc())
        .all()
    )
    if not all_active:
        return "I don't have any active memories yet."
    bullets = "\n".join(f"- ({c.sensitivity}) {c.text}" for c in all_active)
    return f"Active memories:\n{bullets}"


def _handle_explain(ctx: QueryContext, topic: str) -> str:
    matches = find_memory_matches(topic, status="active")
    if not matches:
        matches = _find_memory_by_substring(topic, status="active")
    if not matches:
        return f"I don't have any active memory matching {topic!r}."
    claim = matches[0]
    ev = (
        db.db.session.query(MemoryEvidence)
        .filter(MemoryEvidence.memory_uuid == claim.uuid)
        .order_by(MemoryEvidence.id.asc())
        .all()
    )
    if not ev:
        return f"I remember: {claim.text} (no evidence rows recorded)"
    lines = [f"I remember: {claim.text}", "Evidence:"]
    for e in ev:
        line = f"- {e.provenance} via {e.source_type}"
        if e.source_id:
            line += f" (source_id={e.source_id})"
        if e.excerpt:
            line += f" — {e.excerpt}"
        lines.append(line)
    return "\n".join(lines)


def _handle_used(ctx: QueryContext) -> str:
    """Look up the most recent `debug-memory` chat row in this room and
    report the memories it referenced. Each entry lists the memory text,
    its provenance summary, and a short evidence excerpt where one
    exists. If nothing has been logged in this room, says so."""
    from db import ChatMessage as _ChatMessage  # local to avoid a top-level cycle
    import json as _json
    from uuid import UUID

    room_uuid = ctx.room_uuid
    if room_uuid is None:
        return "I have no record of memories used in this room."
    row = (
        db.db.session.query(_ChatMessage)
        .filter(
            _ChatMessage.room_uuid == room_uuid,
            _ChatMessage.kind == "debug-memory",
        )
        .order_by(_ChatMessage.id.desc())
        .limit(1)
        .first()
    )
    if row is None:
        return "I haven't logged any memory use in this room yet."
    try:
        payload = _json.loads(row.text)
    except (ValueError, TypeError):
        return "The most recent memory-use audit row could not be parsed."
    entries = payload.get("memories") or []
    if not entries:
        return "The most recent memory-use audit row listed no memories."
    lines = ["For my previous reply I used these memories:"]
    for e in entries:
        muuid_raw = e.get("memory_uuid")
        if not muuid_raw:
            continue
        try:
            muuid = UUID(str(muuid_raw))
        except (ValueError, TypeError):
            continue
        claim = db.get_memory_claim(muuid)
        prov = ", ".join(e.get("provenance") or []) or "no provenance recorded"
        if claim is None:
            lines.append(f"- {muuid_raw} (claim no longer present) — {prov}")
            continue
        lines.append(f"- {claim.text}")
        lines.append(f"    uuid: {muuid_raw}")
        lines.append(f"    provenance: {prov}")
    return "\n".join(lines)


def handle_memory_command(ctx: QueryContext, cmd: MemoryCommand) -> str:
    """Apply a parsed memory command and return the reply text the agent
    should post. DB mutations happen as a side effect."""
    if cmd.kind == "remember":
        return _handle_remember(ctx, cmd.text)
    if cmd.kind == "forget":
        return _handle_forget(ctx, cmd.text)
    if cmd.kind == "confirm":
        return _handle_confirm(ctx, cmd.text)
    if cmd.kind == "correct":
        return _handle_correct(ctx, cmd.text, cmd.new_text)
    if cmd.kind == "recall":
        return _handle_recall(ctx, cmd.text)
    if cmd.kind == "explain":
        return _handle_explain(ctx, cmd.text)
    if cmd.kind == "used":
        return _handle_used(ctx)
    raise ValueError(f"unknown command kind: {cmd.kind}")
