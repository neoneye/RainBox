"""The /diary/passage page — one diary citation, read-only, in its file.

A citation (`diary:<revision_uuid>:<byte_start>-<byte_end>`, diary/citations.py)
names bytes in one immutable revision of one diary file. This page is where
a reply source's diary link lands: it shows the source, the file, the entry
the citation starts in (its date, from the file's current parse), whether the
revision is still the file's current text, and the file's text around the
cited bytes with those bytes highlighted. `?whole=1` shows the whole file.

A bare `diary:<revision_uuid>` (a citation cut before its byte range) names
the revision as a whole: the page shows the file from its start and says so.

An excluded file (db.diary_exclusions) reads as not found, as it does for the
assistant's reads. A source being switched off does not hide it: this is the
operator's page over the operator's own diary.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime, time
from uuid import UUID

import sqlalchemy as sa
from flask import render_template_string, request

import db
from diary.citations import parse_citation

from .core import app

# Lines shown before and after the cited bytes.
CONTEXT_LINES = 40

_BARE_REVISION = re.compile(
    r"^diary:([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}):?$")


@dataclass
class DiaryPassageView:
    citation: str
    source_name: str
    path: str
    revision_uuid: UUID
    ingested_at: datetime | None
    current: bool              # the revision's bytes still open the file's current text
    whole_revision: bool       # a bare revision link: nothing is highlighted
    byte_start: int
    byte_end: int
    entry_date: date | None
    entry_clock: time | None
    before: str
    cited: str
    after: str
    lines_hidden_before: int
    lines_hidden_after: int


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def diary_passage(link: str, *, whole: bool = False,
                  context_lines: int = CONTEXT_LINES) -> DiaryPassageView | None:
    """The page's view of one diary link, or None: not a diary link, no such
    revision, or an excluded file."""
    link = str(link or "").strip()
    c = parse_citation(link)
    if c is not None:
        revision_uuid, start, end = c.revision_uuid, c.byte_start, c.byte_end
    else:
        m = _BARE_REVISION.match(link)
        if m is None:
            return None
        revision_uuid, start, end = UUID(m[1]), 0, 0
    rev = db.session.get(db.DiaryRevision, revision_uuid)
    f = db.session.get(db.DiaryFile, rev.file_uuid) if rev else None
    source = db.session.get(db.DiarySource, f.source_uuid) if f else None
    if (rev is None or f is None or source is None
            or f.relative_path in db.diary_exclusions(source.uuid)):
        return None
    if end > rev.byte_length:
        return None
    raw = db.diary_revision_bytes(rev)

    # The entry the citation starts in: entries are parsed from the file's
    # current generation, so they apply when this revision is (a prefix of) it.
    current = False
    entry_date = entry_clock = None
    gen = (db.session.get(db.DiaryGeneration, f.current_generation_uuid)
           if f.current_generation_uuid else None)
    cur = db.session.get(db.DiaryRevision, gen.revision_uuid) if gen else None
    if cur is not None:
        current = cur.uuid == rev.uuid or (
            rev.byte_length <= cur.byte_length
            and hashlib.sha256(db.diary_revision_bytes(cur)[: rev.byte_length]).hexdigest()
            == rev.sha256)
    if gen is not None and current and end > start:
        row = db.session.execute(sa.text(
            "SELECT date_local, clock_start FROM diary_entry WHERE generation_uuid = :g "
            "AND byte_start <= :b AND byte_end > :b ORDER BY ordinal LIMIT 1"),
            {"g": gen.uuid, "b": start}).first()
        if row is not None:
            entry_date, entry_clock = row[0], row[1]

    before, cited, after = _decode(raw[:start]), _decode(raw[start:end]), _decode(raw[end:])
    hidden_before = hidden_after = 0
    if not whole:
        lines = before.split("\n")
        # The first kept "line" is the cited line's own start; count whole lines.
        if len(lines) > context_lines + 1:
            hidden_before = len(lines) - (context_lines + 1)
            before = "\n".join(lines[hidden_before:])
        limit = context_lines * 2 if end == start else context_lines
        lines = after.split("\n")
        if len(lines) > limit + 1:
            hidden_after = len(lines) - (limit + 1)
            after = "\n".join(lines[: limit + 1])
    return DiaryPassageView(
        citation=link, source_name=source.name, path=f.relative_path,
        revision_uuid=rev.uuid, ingested_at=rev.first_ingested_at, current=current,
        whole_revision=end == start, byte_start=start, byte_end=end,
        entry_date=entry_date, entry_clock=entry_clock,
        before=before, cited=cited, after=after,
        lines_hidden_before=hidden_before, lines_hidden_after=hidden_after)


DIARY_PASSAGE_TEMPLATE = """
<!doctype html>
<title>Diary passage &mdash; rainbox</title>
{% include "_nav.html" %}
<style>
  body { margin: 0; font-family: system-ui, sans-serif; }
  .pp-diary { max-width: 1000px; margin: 1rem auto; padding: 0 1rem; }
  .pp-diary h1 { margin: 0.2rem 0 0.8rem; }
  .pp-diary .status { color: #6c757d; }
  .pp-diary table { border-collapse: collapse; width: 100%; margin-bottom: 1rem; }
  .pp-diary th { text-align: left; vertical-align: top; white-space: nowrap;
                 font-size: 0.8rem; color: #6c757d; text-transform: uppercase;
                 letter-spacing: 0.04em; padding: 0.55rem 1rem 0.55rem 0;
                 border-bottom: 1px solid #eee; width: 1%; }
  .pp-diary td { padding: 0.5rem 0; border-bottom: 1px solid #eee; vertical-align: top;
                 overflow-wrap: anywhere; }
  .pp-diary code { font-family: ui-monospace, monospace; font-size: 0.85rem;
                   background: #f6f8fa; padding: 1px 5px; border-radius: 4px; }
  .pp-diary .note { color: #6c757d; font-size: 0.85rem; margin: 0.4rem 0; }
  .pp-diary pre.text { white-space: pre-wrap; overflow-wrap: anywhere; font-size: 0.85rem;
                       line-height: 1.45; background: #fafafa; border: 1px solid #eee;
                       border-radius: 6px; padding: 0.8rem 1rem; margin: 0; }
  .pp-diary pre.text .ctx { color: #6c757d; }
  .pp-diary pre.text mark { background: #fff3bf; color: inherit;
                            box-shadow: 0 0 0 2px #fff3bf; border-radius: 2px; }
</style>
<main class="pp-diary">
  <h1>Diary passage</h1>
  {% if view %}
  <table>
    <tr><th>Citation</th><td><code>{{ view.citation }}</code></td></tr>
    <tr><th>Source</th><td>{{ view.source_name }}</td></tr>
    <tr><th>File</th><td><code>{{ view.path }}</code></td></tr>
    {% if view.entry_date %}
    <tr><th>Entry</th><td>{{ view.entry_date.isoformat() }}{% if view.entry_clock %}
      {{ view.entry_clock.strftime('%H:%M') }}{% endif %}</td></tr>
    {% endif %}
    {% if not view.whole_revision %}
    <tr><th>Bytes</th><td>{{ view.byte_start }}&ndash;{{ view.byte_end }}</td></tr>
    {% endif %}
    <tr><th>Revision</th><td><code>{{ view.revision_uuid }}</code>{% if view.ingested_at %}, read {{ view.ingested_at.strftime('%Y-%m-%d %H:%M') }}{% endif %}
      &mdash; {{ 'its text is still in the file' if view.current
                 else 'the file has changed since; this text is no longer in it' }}</td></tr>
  </table>
  {% if view.whole_revision %}
  <p class="note">This link names the whole file revision, not a passage: there is
    nothing to highlight.</p>
  {% endif %}
  {% if view.lines_hidden_before %}
  <p class="note">{{ view.lines_hidden_before }} earlier lines not shown &middot;
    <a href="?citation={{ view.citation|urlencode }}&whole=1">show the whole file</a></p>
  {% endif %}
  <pre class="text"><span class="ctx">{{ view.before }}</span>{% if view.cited %}<mark id="cited">{{ view.cited }}</mark>{% endif %}<span class="ctx">{{ view.after }}</span></pre>
  {% if view.lines_hidden_after %}
  <p class="note">{{ view.lines_hidden_after }} later lines not shown &middot;
    <a href="?citation={{ view.citation|urlencode }}&whole=1">show the whole file</a></p>
  {% endif %}
  <script>
    const cited = document.getElementById('cited');
    if (cited) cited.scrollIntoView({block: 'center'});
  </script>
  {% else %}
  <p class="status">No diary passage at <code>{{ wanted }}</code>.</p>
  {% endif %}
</main>
"""


@app.route("/diary/passage")
def diary_passage_page() -> str:
    wanted = request.args.get("citation", "")
    view = diary_passage(wanted, whole=request.args.get("whole") == "1")
    return render_template_string(DIARY_PASSAGE_TEMPLATE, view=view, wanted=wanted)
