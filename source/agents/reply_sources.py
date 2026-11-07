"""The items a reply rests on: its `sources` argument, checked and resolved.

The model names them in `reply`'s `sources` argument — a link and a short
title per item — because only it knows which of the turn's reads the answer
used. Code decides which of them stand: a link is kept only when its text
appears verbatim in something this turn showed the model (an observation, the
request message), so the model cannot cite what it never read or what an
earlier answer said. Every kept link is resolved here into a kind and a page
the chat can open. Nothing is bounced: a link that fails a check is dropped
and the drop is reported, with its reason, on the reply's step.

One repair is made instead of a drop. A small model sometimes copies a whole
line of an observation into `link` — a read's summary with the items it
returned listed inside it — rather than one item. The line as a whole was
never shown verbatim, but the items in it were: each link inside it (a token
holding a uuid, such as a diary citation, or a web address) that this turn
showed is kept as a source of its own, under the entry's name, and the report
says how many were kept from it. A link this turn never showed is dropped
however it is wrapped.

Design: docs/superpowers/specs/2026-11-02-reply-sources-design.md.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable

# A reply cites the reads it rests on, not every line it saw.
MAX_REPLY_SOURCES = 20
# A title, not a description.
MAX_SOURCE_NAME_CHARS = 120
# Longer than any uuid, URL or path an observation shows in one piece.
MAX_SOURCE_LINK_CHARS = 500

_UUID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

# Why a source was dropped, as the reply's step reports it.
DROP_NOT_SEEN = "not in any observation of this turn or the request"
DROP_NO_LINK = "no link"
DROP_TOO_LONG = f"link longer than {MAX_SOURCE_LINK_CHARS} characters"
DROP_DUPLICATE = "same link as an earlier source"
DROP_OVER_LIMIT = f"more than {MAX_REPLY_SOURCES} sources"
DROP_NOT_A_LIST = "sources is not a list"

# What a link-shaped token inside a longer link may be wrapped in: quotes,
# brackets, and the punctuation of the sentence around it.
_WRAPPING = "\"'`()[]{}<>,;.:…"


def links_inside(text: str) -> list[str]:
    """The link-shaped tokens in `text`, in order: whitespace-separated words,
    stripped of wrapping punctuation, that hold a full uuid (a uuid, a diary
    citation) or are a web address."""
    out: list[str] = []
    for word in text.split():
        token = word.strip(_WRAPPING)
        if token and (_UUID.search(token) or token.startswith(("http://", "https://"))):
            out.append(token)
    return out


def normalize_sources(raw: Any) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """`(entries, dropped)` from the `sources` argument as the model wrote it.

    An entry is `{"name", "link"}`, both stripped strings; a bare string is a
    link with no name. A dropped item keeps what could be read of it plus its
    `reason`."""
    if raw is None:
        return [], []
    if not isinstance(raw, list):
        return [], [{"name": "", "link": str(raw)[:MAX_SOURCE_LINK_CHARS],
                     "reason": DROP_NOT_A_LIST}]
    entries: list[dict[str, str]] = []
    dropped: list[dict[str, str]] = []
    seen_links: set[str] = set()
    for item in raw:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            link = str(item.get("link") or "").strip()
        else:
            name, link = "", str(item or "").strip()
        name = name[:MAX_SOURCE_NAME_CHARS]
        reason = None
        if not link:
            reason = DROP_NO_LINK
        elif len(link) > MAX_SOURCE_LINK_CHARS:
            reason = DROP_TOO_LONG
        elif link in seen_links:
            reason = DROP_DUPLICATE
        elif len(entries) >= MAX_REPLY_SOURCES:
            reason = DROP_OVER_LIMIT
        if reason is not None:
            dropped.append({"name": name, "link": link[:MAX_SOURCE_LINK_CHARS],
                            "reason": reason})
            continue
        seen_links.add(link)
        entries.append({"name": name, "link": link})
    return entries, dropped


def check_seen(
    entries: list[dict[str, str]], seen_texts: Iterable[str]
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """`(kept, dropped)`: an entry is kept when its link occurs verbatim in one
    of `seen_texts` — this turn's observations and the request message. An
    entry that is not, but holds links that are (`links_inside`), is reported
    dropped and those links are kept in its place, under its name, up to
    MAX_REPLY_SOURCES in all."""
    texts = [t for t in seen_texts if t]

    def seen(link: str) -> bool:
        return any(link in text for text in texts)

    kept: list[dict[str, str]] = []
    dropped: list[dict[str, str]] = []
    for entry in entries:
        if seen(entry["link"]):
            if all(k["link"] != entry["link"] for k in kept):
                kept.append(entry)
            continue
        rescued = 0
        for link in links_inside(entry["link"]):
            if len(kept) >= MAX_REPLY_SOURCES:
                break
            if link != entry["link"] and seen(link) and all(k["link"] != link for k in kept):
                kept.append({"name": entry["name"], "link": link})
                rescued += 1
        reason = DROP_NOT_SEEN
        if rescued:
            reason += f"; kept the {rescued} link{'s' if rescued > 1 else ''} inside it"
        dropped.append({**entry, "reason": reason})
    return kept, dropped


def resolve_source(
    entry: dict[str, str],
    describe_uuid: Callable[[str], dict[str, Any] | None],
) -> dict[str, Any]:
    """`{"name", "link", "kind", "url"}` for one kept entry.

    A full uuid is looked up (`db.describe_uuid`) for its kind and page,
    falling back to the Find page, which resolves any uuid; a web address is
    its own page; anything else is a path with no page."""
    link = entry["link"]
    name = entry.get("name") or ""
    if _UUID.fullmatch(link):
        try:
            match = describe_uuid(link)
        except Exception:   # noqa: BLE001 — an unresolved kind still links to /find
            match = None
        kind = str((match or {}).get("kind") or "record")
        url = (match or {}).get("url") or f"/find?q={link}"
        if not name:
            name = str((match or {}).get("name") or "")
    elif link.startswith(("http://", "https://")):
        kind, url = "web", link
    else:
        kind, url = "path", None
    return {"name": name or link, "link": link, "kind": kind, "url": url}


def reply_sources(
    raw: Any,
    seen_texts: Iterable[str],
    describe_uuid: Callable[[str], dict[str, Any] | None],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """`(sources, dropped)` for a reply's `sources` argument: normalized,
    checked against what the turn showed, and resolved."""
    entries, dropped = normalize_sources(raw)
    kept, unseen = check_seen(entries, seen_texts)
    return [resolve_source(e, describe_uuid) for e in kept], dropped + unseen


def sources_report(sources: list[dict[str, Any]],
                   dropped: list[dict[str, str]]) -> str:
    """The reply step's observation text: what was kept, then what was dropped
    and why."""
    lines = [f"sources: {len(sources)}"]
    for i, s in enumerate(sources, start=1):
        lines.append(f"{i}. {s['name']} — {s['link']} ({s['kind']})")
    if dropped:
        lines.append(f"dropped: {len(dropped)}")
        for d in dropped:
            label = f"{d['name']} — " if d.get("name") else ""
            lines.append(f"- {label}{d['link']}: {d['reason']}")
    return "\n".join(lines)
