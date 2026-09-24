"""`parse_file`: bytes of one diary file -> entries, passages, annotations,
coverage and diagnostics. Pure: no database, no model, no filesystem.

One unit inside, bytes at the edge. The file is decoded once and every
decision (boundaries, chunk caps, override positions, match spans) is made in
character (code point) offsets. When a span is emitted it is converted to a
half-open UTF-8 byte range into the revision, BOM included, so a stored
offset slices `raw_bytes` directly. A character boundary is a UTF-8 boundary,
so no emitted range can split a multi-byte character.

Precedence (proposal §5):
1. strict decoding (the only failure that quarantines a file);
2. explicit overrides (stale or invalid ones are skipped with a diagnostic);
3. fenced regions, with a circuit breaker for an unmatched opener;
4. likely terminal regions, then dialect boundaries outside protected spans;
5. date/author context, order diagnostics, coverage, passage chunking.

Diagnostics carry a code, a byte offset and numeric/hash detail — never
copied body text.
"""

from __future__ import annotations

import bisect
import hashlib
import posixpath
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from diary.config import dialect_for

BOM = b"\xef\xbb\xbf"
# Every code parse_file can emit. test_parsing pins one fixture per code.
DIAGNOSTIC_CODES = frozenset({
    "nul_byte", "invalid_utf8", "override_stale", "override_invalid",
    "fence_unterminated", "possible_boundary_in_terminal", "invalid_date",
    "invalid_time", "unknown_month", "filename_header_mismatch",
    "invalid_daily_filename", "clock_regression", "date_regression",
})
# How far an unmatched fence opener (or a likely terminal region) protects
# before the circuit breaker closes it.
PROTECTED_REGION_CAP = 16_384

TERMINAL_START = re.compile(r"^(?:\$[ \t]|[A-Za-z0-9_.@-]+:[~/][^\r\n]*?[#$](?:[ \t]|$))")
FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})")
TIMED_DATE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
TIMED_TIME = re.compile(r"^(\d{2})h(\d{2})(?:[ \t]*-[ \t]*(\d{2})h(\d{2}))?$")
DAILY_TIME = re.compile(r"^(\d{2})(\d{2})$")
CHANGELOG_HEADER = re.compile(r"^(\d{1,2})-([^\s\d-]+)-(\d{4})[ \t]+(\S+)$")
CHANGELOG_BULLET = re.compile(r"^\*[ \t]")
FILENAME_DATE = (
    re.compile(r"^(\d{4})(\d{2})(\d{2})$"),
    re.compile(r"^(\d{4})_(\d{2})_(\d{2})$"),
)
FILENAME_DATE_DMY = re.compile(r"^(\d{2})-(\d{2})-(\d{4})$")
DAILY_FILENAME = re.compile(r"^(\d{4})_(\d{2})_(\d{2})$")

# Identifier rules (IDENTIFIER_VERSION 1). Conservative; unusual identifiers
# are reached through literal mode, not through these.
_URL = re.compile(r"https?://[^\s<>\"'`]+")
_HASH = re.compile(r"(?<![0-9A-Za-z])[0-9a-f]{7,40}(?![0-9A-Za-z])")
_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_QUALIFIED = re.compile(r"(?<![\w:#])[A-Za-z_]\w*(?:(?:::|#)[A-Za-z_]\w*)+")
_ISSUE = re.compile(r"(?<![\w/#&])#\d+\b")
_MODEL = re.compile(r"(?<![\w/:.-])[a-z][a-z0-9._-]*:[a-z0-9][a-z0-9._-]*\b(?!/)")
_TOKEN = re.compile(r"\S+")
_TRAILING_PUNCT = ".,;:!?)]}\"'#>"


@dataclass(frozen=True)
class Diagnostic:
    code: str
    byte_offset: int
    detail: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"code": self.code, "byte_offset": self.byte_offset, **self.detail}


@dataclass(frozen=True)
class ParsedPassage:
    part_index: int
    byte_start: int
    byte_end: int
    text: str
    text_hash: str


@dataclass(frozen=True)
class ParsedAnnotation:
    kind: str       # identifier | command_hint | idea_hint | pasted
    subtype: str    # url|hash|path|symbol|issue|model for identifiers, else none
    value: str
    byte_start: int
    byte_end: int
    basis: str      # rule | explicit_override


@dataclass
class ParsedEntry:
    ordinal: int
    byte_start: int
    byte_end: int
    text: str
    date_local: date | None
    clock_start: time | None
    clock_end: time | None
    date_basis: str      # header | filename | unknown
    time_status: str     # date_only|minute|range|unknown|ambiguous|invalid_range
    author_token: str | None
    context_ranges: list[tuple[int, int]]
    passages: list[ParsedPassage] = field(default_factory=list)
    annotations: list[ParsedAnnotation] = field(default_factory=list)


@dataclass
class ParsedFile:
    status: str          # ok | quarantined
    dialect: str
    byte_length: int
    entries: list[ParsedEntry]
    coverage: list[tuple[int, int, str]]
    diagnostics: list[Diagnostic]

    def coverage_json(self) -> list[list[Any]]:
        return [[s, e, t] for s, e, t in self.coverage]

    def diagnostics_json(self) -> list[dict[str, Any]]:
        return [d.to_json() for d in self.diagnostics]


@dataclass
class _Line:
    index: int
    char_start: int
    char_end: int
    byte_start: int
    byte_end: int
    text: str
    rec: str      # recognition form: ending and trailing spaces/tabs removed

    @property
    def empty(self) -> bool:
        return self.rec.strip(" \t") == ""


@dataclass
class _Marker:
    kind: str                     # date | start
    date: date | None = None
    clock_start: time | None = None
    clock_end: time | None = None
    author: str | None = None


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _valid_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _valid_clock(h: int, m: int) -> time | None:
    if 0 <= h <= 23 and 0 <= m <= 59:
        return time(h, m)
    return None


def filename_date(relative_path: str) -> date | None:
    """Only complete stems: YYYYMMDD, YYYY_MM_DD or DD-MM-YYYY."""
    stem = posixpath.basename(relative_path).split(".", 1)[0]
    for pattern in FILENAME_DATE:
        m = pattern.match(stem)
        if m:
            return _valid_date(int(m[1]), int(m[2]), int(m[3]))
    m = FILENAME_DATE_DMY.match(stem)
    if m:
        return _valid_date(int(m[3]), int(m[2]), int(m[1]))
    return None


class _Text:
    """Decoded text split on "\\n" only, with char<->byte conversion."""

    def __init__(self, text: str, bom: int):
        self.text = text
        self.bom = bom
        self.lines: list[_Line] = []
        char = 0
        byte = bom
        idx = 0
        while char < len(text):
            nl = text.find("\n", char)
            end = len(text) if nl < 0 else nl + 1
            line_text = text[char:end]
            nbytes = len(line_text.encode("utf-8"))
            rec = line_text[:-1] if line_text.endswith("\n") else line_text
            if rec.endswith("\r"):
                rec = rec[:-1]
            rec = rec.rstrip(" \t")
            self.lines.append(_Line(idx, char, end, byte, byte + nbytes, line_text, rec))
            idx += 1
            char = end
            byte += nbytes
        self.char_starts = [ln.char_start for ln in self.lines]
        self.byte_length = byte

    def byte_at(self, char_offset: int) -> int:
        if char_offset >= len(self.text):
            return self.byte_length
        i = bisect.bisect_right(self.char_starts, char_offset) - 1
        line = self.lines[i]
        return line.byte_start + len(line.text[: char_offset - line.char_start].encode("utf-8"))


def _quarantined(dialect: str, n: int, diag: Diagnostic) -> ParsedFile:
    return ParsedFile("quarantined", dialect, n, [], [], [diag])


def parse_file(raw_bytes: bytes, relative_path: str, parser_config: dict[str, Any]) -> ParsedFile:
    dialect = dialect_for(relative_path, parser_config.get("dialect_rules", []))
    n = len(raw_bytes)
    nul = raw_bytes.find(b"\x00")
    if nul >= 0:
        return _quarantined(dialect, n, Diagnostic("nul_byte", nul))
    bom = 3 if raw_bytes.startswith(BOM) else 0
    try:
        decoded = raw_bytes[bom:].decode("utf-8")
    except UnicodeDecodeError as exc:
        return _quarantined(dialect, n, Diagnostic("invalid_utf8", bom + exc.start))
    return _Parser(raw_bytes, relative_path, parser_config, dialect, _Text(decoded, bom)).run()


class _Parser:
    def __init__(self, raw: bytes, path: str, config: dict[str, Any], dialect: str, text: _Text):
        self.raw = raw
        self.path = path
        self.config = config
        self.dialect = dialect
        self.t = text
        self.lines = text.lines
        self.diags: list[Diagnostic] = []
        self.months: dict[str, int] = config.get("month_table", {})
        self.tz = ZoneInfo(config["timezone"]) if config.get("timezone") else None
        self.cap: int = config.get("passage_cap", 700)
        self.file_date = filename_date(path)
        n = len(self.lines)
        self.forced: set[int] = set()
        self.suppressed: set[int] = set()
        self.protect: list[str | None] = [None] * n   # explicit | fenced | terminal
        if self.dialect == "daily":
            stem = posixpath.basename(path).split(".", 1)[0]
            m = DAILY_FILENAME.match(stem)
            daily_date = m and _valid_date(int(m[1]), int(m[2]), int(m[3]))
            if not daily_date:
                self.diags.append(Diagnostic("invalid_daily_filename", 0))
                self.dialect = "plain"
                self.file_date = None

    # --- overrides -----------------------------------------------------------

    def apply_overrides(self) -> None:
        ov = self.config.get("file_overrides", {}).get(self.path)
        if not ov:
            return
        actual = sha256_hex(self.raw)
        if actual != ov["content_sha256"]:
            self.diags.append(Diagnostic("override_stale", 0, {
                "expected_sha256": ov["content_sha256"], "actual_sha256": actual}))
            return
        by_byte = {ln.byte_start: ln.index for ln in self.lines}
        eof = self.t.byte_length
        n = len(self.lines)

        def line_of(offset: int, *, allow_eof: bool) -> int | None:
            if allow_eof and offset == eof:
                return n
            return by_byte.get(offset)

        forced, suppressed, pasted = [], [], []
        for off in ov.get("force_boundary_offsets", []):
            li = line_of(off, allow_eof=False)
            if li is None:
                return self._override_invalid(off)
            forced.append(li)
        for off in ov.get("suppress_boundary_offsets", []):
            li = line_of(off, allow_eof=False)
            if li is None:
                return self._override_invalid(off)
            suppressed.append(li)
        for start, end in ov.get("pasted_ranges", []):
            a = line_of(start, allow_eof=False)
            b = line_of(end, allow_eof=True)
            if a is None:
                return self._override_invalid(start)
            if b is None:
                return self._override_invalid(end)
            pasted.append((a, b))
        self.forced = set(forced)
        self.suppressed = set(suppressed)
        for a, b in pasted:
            for i in range(a, b):
                self.protect[i] = "explicit"

    def _override_invalid(self, offset: int) -> None:
        self.diags.append(Diagnostic("override_invalid", offset))

    # --- line classification ---------------------------------------------------

    def date_header(self, line: _Line, *, report: bool) -> _Marker | None:
        """A date-carrying header of this file's dialect, or None."""
        rec = line.rec
        if self.dialect == "timed":
            m = TIMED_DATE.match(rec)
            if not m:
                return None
            d = _valid_date(int(m[1]), int(m[2]), int(m[3]))
            if d is None:
                if report and 1970 <= int(m[1]) <= 2099:
                    self.diags.append(Diagnostic("invalid_date", line.byte_start))
                return None
            return _Marker("date", date=d)
        if self.dialect == "changelog":
            m = CHANGELOG_HEADER.match(rec)
            if not m:
                return None
            month = self.months.get(m[2].rstrip(".").casefold())
            if month is None:
                if report:
                    self.diags.append(Diagnostic("unknown_month", line.byte_start))
                return None
            d = _valid_date(int(m[3]), month, int(m[1]))
            if d is None:
                if report:
                    self.diags.append(Diagnostic("invalid_date", line.byte_start))
                return None
            return _Marker("date", date=d, author=m[4])
        return None

    def entry_marker(self, i: int, *, report: bool, relaxed: bool = False) -> _Marker | None:
        """An entry-opening marker line of this dialect, or None. `relaxed`
        drops context requirements (a forced boundary) but not validity."""
        line = self.lines[i]
        rec = line.rec
        if self.dialect == "timed":
            m = TIMED_TIME.match(rec)
            if not m:
                return None
            start = _valid_clock(int(m[1]), int(m[2]))
            end = None
            valid = start is not None
            if m[3] is not None:
                end = _valid_clock(int(m[3]), int(m[4]))
                valid = valid and end is not None
            if not valid:
                if report:
                    self.diags.append(Diagnostic("invalid_time", line.byte_start))
                return None
            return _Marker("start", clock_start=start, clock_end=end)
        if self.dialect == "daily":
            m = DAILY_TIME.match(rec)
            if not m:
                return None
            if not relaxed and not (i == 0 or self.lines[i - 1].empty):
                return None
            start = _valid_clock(int(m[1]), int(m[2]))
            if start is None:
                if report:
                    self.diags.append(Diagnostic("invalid_time", line.byte_start))
                return None
            return _Marker("start", clock_start=start)
        if self.dialect == "changelog":
            if CHANGELOG_BULLET.match(line.text):
                return _Marker("start")
            return None
        return None

    def boundary_shaped(self, i: int) -> bool:
        line = self.lines[i]
        return (self.date_header(line, report=False) is not None
                or self.entry_marker(i, report=False) is not None)

    # --- protected regions -----------------------------------------------------

    def find_protected(self) -> None:
        lines = self.lines
        n = len(lines)
        i = 0
        while i < n:
            if self.protect[i] is not None:
                i += 1
                continue
            m = FENCE_OPEN.match(lines[i].rec)
            if m:
                i = self._fence(i, m.group(1))
                continue
            if TERMINAL_START.match(lines[i].rec):
                i = self._terminal(i)
                continue
            i += 1

    def _fence(self, i: int, run: str) -> int:
        lines = self.lines
        n = len(lines)
        char, length = run[0], len(run)
        closer = re.compile("^ {0,3}" + re.escape(char) + "{" + str(length) + ",}$")
        limit = lines[i].char_start + PROTECTED_REGION_CAP
        # A closer within the cap wins outright, so a closed fence protects
        # everything inside it, date-shaped lines included. Only an opener with
        # no closer in reach falls to the circuit breaker: it closes before the
        # next forced boundary or date header, or at the cap.
        for j in range(i + 1, n):
            if lines[j].char_start >= limit:
                break
            if closer.match(lines[j].rec):
                for k in range(i, j + 1):
                    if self.protect[k] is None:
                        self.protect[k] = "fenced"
                return j + 1
        end = n - 1                                   # last protected line, inclusive
        close_byte = self.t.byte_length
        for j in range(i + 1, n):
            if j in self.forced or self.date_header(lines[j], report=False) is not None \
                    or lines[j].char_start >= limit:
                end, close_byte = j - 1, lines[j].byte_start
                break
        self.diags.append(Diagnostic("fence_unterminated", lines[i].byte_start,
                                     {"close_byte_offset": close_byte}))
        for k in range(i, end + 1):
            if self.protect[k] is None:
                self.protect[k] = "fenced"
        return end + 1

    def _terminal(self, i: int) -> int:
        lines = self.lines
        n = len(lines)
        limit = lines[i].char_start + PROTECTED_REGION_CAP
        j = i + 1
        while j < n:
            ln = lines[j]
            if j in self.forced or self.protect[j] == "explicit":
                break
            if ln.empty and j + 1 < n and lines[j + 1].empty:
                break
            if self.date_header(ln, report=False) is not None:
                break
            # `HHhMM` after an empty line does not occur in shell output the
            # way four-digit lines do, so it ends the region in `timed`.
            if self.dialect == "timed" and lines[j - 1].empty \
                    and self.entry_marker(j, report=False) is not None:
                break
            if ln.char_start >= limit:
                break
            j += 1
        end = j - 1
        while end > i and lines[end].empty:
            end -= 1
        for k in range(i, end + 1):
            if self.protect[k] is None:
                self.protect[k] = "terminal"
                if k != i and k not in self.forced and self.boundary_shaped(k):
                    self.diags.append(Diagnostic("possible_boundary_in_terminal", lines[k].byte_start))
        return end + 1

    # --- boundaries and entries -----------------------------------------------

    def classify(self) -> list[_Marker | None]:
        out: list[_Marker | None] = []
        for i, line in enumerate(self.lines):
            if i in self.forced:
                marker = self.date_header(line, report=True) or \
                    self.entry_marker(i, report=True, relaxed=True)
                if marker is None:
                    self._override_invalid(line.byte_start)
                out.append(marker)
                continue
            if self.protect[i] is not None or i in self.suppressed or line.empty:
                out.append(None)
                continue
            out.append(self.date_header(line, report=True) or self.entry_marker(i, report=True))
        return out

    def run(self) -> ParsedFile:
        self.apply_overrides()
        self.find_protected()
        if self.dialect == "plain":
            groups = self._plain_groups()
        else:
            groups = self._marker_groups(self.classify())
        entries = self._build_entries(groups)
        coverage = self._coverage(groups)
        return ParsedFile("ok", self.dialect, self.t.byte_length, entries, coverage, self.diags)

    def _plain_groups(self) -> list[dict[str, Any]]:
        groups: list[dict[str, Any]] = []
        cur = None
        for ln in self.lines:
            if ln.empty and self.protect[ln.index] is None:
                cur = None
                continue
            if cur is None:
                cur = {"kind": "entry", "first": ln.index, "last": ln.index, "marker": None,
                       "ctx": self._initial_ctx()}
                groups.append(cur)
            if not ln.empty:
                cur["last"] = ln.index
        return groups

    def _initial_ctx(self) -> dict[str, Any]:
        if self.file_date is not None:
            return {"date": self.file_date, "basis": "filename", "header": None, "author": None}
        return {"date": None, "basis": "unknown", "header": None, "author": None}

    def _marker_groups(self, markers: list[_Marker | None]) -> list[dict[str, Any]]:
        groups: list[dict[str, Any]] = []
        ctx = self._initial_ctx()
        cur: dict[str, Any] | None = None
        mismatch_reported = False
        prev_header_date: date | None = None
        prev_header_byte = 0
        prev_clock: tuple[date | None, time, int] | None = None
        for i, ln in enumerate(self.lines):
            marker = markers[i]
            if marker is not None and marker.kind == "date":
                cur = None
                if (self.file_date is not None and marker.date != self.file_date
                        and not mismatch_reported):
                    self.diags.append(Diagnostic("filename_header_mismatch", ln.byte_start))
                    mismatch_reported = True
                if prev_header_date is not None and marker.date is not None:
                    newest_first = self.dialect == "changelog"
                    regressed = (marker.date > prev_header_date) if newest_first \
                        else (marker.date < prev_header_date)
                    if regressed:
                        self.diags.append(Diagnostic("date_regression", ln.byte_start,
                                                     {"previous_byte_offset": prev_header_byte}))
                prev_header_date, prev_header_byte = marker.date, ln.byte_start
                ctx = {"date": marker.date, "basis": "header",
                       "header": (ln.byte_start, ln.byte_end), "author": marker.author}
                groups.append({"kind": "header", "first": i, "last": i})
                prev_clock = None if self.dialect == "timed" else prev_clock
                continue
            if marker is not None and marker.kind == "start":
                if marker.clock_start is not None:
                    if prev_clock is not None and prev_clock[0] == ctx["date"] \
                            and marker.clock_start < prev_clock[1]:
                        self.diags.append(Diagnostic("clock_regression", ln.byte_start,
                                                     {"previous_byte_offset": prev_clock[2]}))
                    prev_clock = (ctx["date"], marker.clock_start, ln.byte_start)
                cur = {"kind": "entry", "first": i, "last": i, "marker": marker, "ctx": ctx}
                groups.append(cur)
                continue
            if ln.empty:
                continue
            if cur is None:
                cur = {"kind": "entry", "first": i, "last": i, "marker": None, "ctx": ctx}
                groups.append(cur)
            cur["last"] = i
        return groups

    def _time_status(self, d: date | None, marker: _Marker | None) -> str:
        if marker is None or marker.clock_start is None:
            return "date_only" if d is not None else "unknown"
        if marker.clock_end is not None and marker.clock_end < marker.clock_start:
            return "invalid_range"
        if d is not None and self.tz is not None:
            for clock in (marker.clock_start, marker.clock_end):
                if clock is None:
                    continue
                naive = datetime.combine(d, clock)
                if naive.replace(tzinfo=self.tz, fold=0).utcoffset() != \
                        naive.replace(tzinfo=self.tz, fold=1).utcoffset():
                    return "ambiguous"
        return "range" if marker.clock_end is not None else "minute"

    def _build_entries(self, groups: list[dict[str, Any]]) -> list[ParsedEntry]:
        entries: list[ParsedEntry] = []
        for g in groups:
            if g["kind"] != "entry":
                continue
            first, last = self.lines[g["first"]], self.lines[g["last"]]
            ctx, marker = g["ctx"], g["marker"]
            text = self.t.text[first.char_start:last.char_end]
            entry = ParsedEntry(
                ordinal=len(entries),
                byte_start=first.byte_start,
                byte_end=last.byte_end,
                text=text,
                date_local=ctx["date"],
                clock_start=marker.clock_start if marker else None,
                clock_end=marker.clock_end if marker else None,
                date_basis=ctx["basis"] if ctx["date"] is not None else "unknown",
                time_status=self._time_status(ctx["date"], marker),
                author_token=ctx["author"],
                context_ranges=[ctx["header"]] if ctx["header"] else [],
            )
            entry.passages = self._chunk(first.char_start, g["first"], g["last"])
            entry.annotations = self._annotate(entry, g, first.char_start)
            entries.append(entry)
        return entries

    def _chunk(self, start: int, first: int, last: int) -> list[ParsedPassage]:
        cap = self.cap
        spans: list[tuple[int, int]] = []
        s = start
        acc = start
        for li in range(first, last + 1):
            line_end = self.lines[li].char_end
            if line_end - s <= cap:
                acc = line_end
                continue
            # Flush at the last line end only when the new line fits a passage
            # of its own; a line that must be hard-split anyway carries the
            # pending text (often just the entry marker) along with it.
            if acc > s and line_end - acc <= cap:
                spans.append((s, acc))
                s = acc
            while line_end - s > cap:
                spans.append((s, s + cap))
                s += cap
            acc = line_end
        if acc > s:
            spans.append((s, acc))
        out = []
        for k, (a, b) in enumerate(spans):
            piece = self.t.text[a:b]
            out.append(ParsedPassage(k, self.t.byte_at(a), self.t.byte_at(b), piece,
                                     sha256_hex(piece.encode("utf-8"))))
        return out

    def _annotate(self, entry: ParsedEntry, g: dict[str, Any], base: int) -> list[ParsedAnnotation]:
        text = entry.text
        found: dict[tuple, ParsedAnnotation] = {}

        def add(kind: str, subtype: str, value: str, a: int, b: int, basis: str = "rule") -> None:
            if b <= a:
                return
            bs, be = self.t.byte_at(base + a), self.t.byte_at(base + b)
            key = (kind, subtype, bs, be, value)
            found.setdefault(key, ParsedAnnotation(kind, subtype, value, bs, be, basis))

        def trimmed(m: re.Match) -> tuple[int, int, str]:
            value = m.group(0).rstrip(_TRAILING_PUNCT)
            return m.start(), m.start() + len(value), value

        for m in _URL.finditer(text):
            a, b, v = trimmed(m)
            add("identifier", "url", v, a, b)
        for m in _HASH.finditer(text):
            v = m.group(0)
            if any(c.isdigit() for c in v) and any(c.isalpha() for c in v):
                add("identifier", "hash", v, m.start(), m.end())
        for m in _TOKEN.finditer(text):
            tok = m.group(0).strip("\"'(`")
            lead = m.group(0).find(tok) if tok else 0
            tok = tok.rstrip(_TRAILING_PUNCT)
            if "/" not in tok or "://" in tok:
                continue
            segments = [p for p in tok.split("/") if p not in ("", "~", ".", "..")]
            if len(segments) < 2:
                continue   # "/goal", "/tmp": a single segment is too ambiguous
            if tok.startswith(("/", "~/", "./", "../")) or tok.endswith("/") or "." in segments[-1]:
                a = m.start() + lead
                add("identifier", "path", tok, a, a + len(tok))
        for m in _CODE_SPAN.finditer(text):
            add("identifier", "symbol", m.group(1), m.start(1), m.end(1))
        for m in _QUALIFIED.finditer(text):
            add("identifier", "symbol", m.group(0), m.start(), m.end())
        for m in _ISSUE.finditer(text):
            add("identifier", "issue", m.group(0), m.start(), m.end())
        for m in _MODEL.finditer(text):
            add("identifier", "model", m.group(0), m.start(), m.end())

        # Pasted spans inside this entry.
        run_start = None
        run_kind = None
        for li in range(g["first"], g["last"] + 2):
            kind = self.protect[li] if li <= g["last"] else None
            if kind != run_kind:
                if run_kind is not None and run_start is not None:
                    a = self.lines[run_start].char_start - base
                    last_li = li - 1
                    while last_li > run_start and self.lines[last_li].empty:
                        last_li -= 1
                    b = self.lines[last_li].char_end - base
                    value = {"explicit": "explicit", "fenced": "fenced",
                             "terminal": "terminal_likely"}[run_kind]
                    basis = "explicit_override" if run_kind == "explicit" else "rule"
                    add("pasted", "none", value, a, b, basis)
                run_start, run_kind = li, kind

        # Command / idea hints: the first body token, outside pasted text.
        body_line = g["first"]
        body_offset = 0
        marker = g["marker"]
        if marker is not None:
            if self.dialect == "changelog":
                m = CHANGELOG_BULLET.match(self.lines[body_line].text)
                body_offset = m.end() if m else 0
            else:
                body_line += 1
        for li in range(body_line, g["last"] + 1):
            ln = self.lines[li]
            start_in_line = body_offset if li == g["first"] else 0
            tm = _TOKEN.search(ln.text, start_in_line)
            if tm is None or tm.group(0) == "":
                continue
            if self.protect[li] is not None:
                break
            token = tm.group(0)
            a = ln.char_start - base + tm.start()
            if token in self.config.get("command_tokens", []):
                add("command_hint", "none", token, a, a + len(token))
            elif token == "IDEA:":
                add("idea_hint", "none", token, a, a + len(token))
            break
        return sorted(found.values(), key=lambda x: (x.byte_start, x.byte_end, x.kind, x.subtype, x.value))

    def _coverage(self, groups: list[dict[str, Any]]) -> list[tuple[int, int, str]]:
        tags: list[str] = ["separator"] * len(self.lines)
        owner: list[int] = [-1] * len(self.lines)
        ordinal = 0
        for gi, g in enumerate(groups):
            tag = "header" if g["kind"] == "header" else "entry"
            for li in range(g["first"], g["last"] + 1):
                tags[li] = tag
                owner[li] = gi
            if tag == "entry":
                ordinal += 1
        out: list[tuple[int, int, str]] = []
        if self.t.bom:
            out.append((0, self.t.bom, "separator"))
        prev_key = None
        for ln in self.lines:
            key = (tags[ln.index], owner[ln.index])
            if out and prev_key == key and out[-1][1] == ln.byte_start:
                s, _, t = out[-1]
                out[-1] = (s, ln.byte_end, t)
            elif out and key[0] == "separator" and out[-1][2] == "separator" and out[-1][1] == ln.byte_start:
                s, _, t = out[-1]
                out[-1] = (s, ln.byte_end, t)
            else:
                out.append((ln.byte_start, ln.byte_end, key[0]))
            prev_key = key
        return out
