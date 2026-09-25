"""parse_file: byte fidelity, dialects, protected regions, overrides, chunking
and one fixture per diagnostic code. All inputs are synthetic."""

import copy
from datetime import date, time
from pathlib import Path

import pytest

from diary.config import build_parser_config, load_manifest
from diary.parsing import (
    BOM,
    DIAGNOSTIC_CODES,
    PROTECTED_REGION_CAP,
    TERMINAL_START,
    parse_file,
    sha256_hex,
)
from diary.test_config import BASE

FIXTURE = Path(__file__).resolve().parent.parent / "data" / "diary_fixture"


def config(**changes):
    raw = copy.deepcopy(BASE)
    raw.update(changes)
    return build_parser_config(load_manifest(raw, check_root=False))


CFG = config()


def fixture(path: str) -> bytes:
    return (FIXTURE / path).read_bytes()


def assert_well_formed(raw: bytes, parsed) -> None:
    """Coverage tiles [0, len); every emitted range slices to its text."""
    cov = parsed.coverage
    if raw:
        assert cov[0][0] == 0 and cov[-1][1] == len(raw)
        assert all(a[1] == b[0] for a, b in zip(cov, cov[1:]))
        assert all(s < e for s, e, _ in cov)
    else:
        assert cov == []
    for e in parsed.entries:
        assert raw[e.byte_start:e.byte_end].decode("utf-8") == e.text
        assert (e.byte_start, e.byte_end, "entry") in cov
        parts = e.passages
        assert parts[0].byte_start == e.byte_start and parts[-1].byte_end == e.byte_end
        assert all(a.byte_end == b.byte_start for a, b in zip(parts, parts[1:]))
        for p in parts:
            assert raw[p.byte_start:p.byte_end].decode("utf-8") == p.text
            assert len(p.text) <= CFG["passage_cap"]
            assert p.text_hash == sha256_hex(p.text.encode("utf-8"))
        for a in e.annotations:
            assert e.byte_start <= a.byte_start < a.byte_end <= e.byte_end
        for s, t in e.context_ranges:
            assert (s, t, "header") in cov


def codes(parsed) -> list[str]:
    return [d.code for d in parsed.diagnostics]


# --- fixtures -----------------------------------------------------------------


@pytest.mark.parametrize("path", ["current/2027.txt", "archive/ChangeLog.txt",
                                  "daily/2027_07_26.txt"])
def test_fixture_files_are_well_formed_and_deterministic(path):
    raw = fixture(path)
    first = parse_file(raw, path, CFG)
    assert first.status == "ok"
    assert_well_formed(raw, first)
    assert parse_file(raw, path, CFG) == first


def test_timed_fixture():
    raw = fixture("current/2027.txt")
    parsed = parse_file(raw, "current/2027.txt", CFG)
    assert parsed.dialect == "timed"
    entries = parsed.entries
    assert entries[0].time_status == "date_only" and entries[0].text.startswith("Woke at")
    assert (entries[1].clock_start, entries[1].clock_end) == (time(9, 30), time(10, 0))
    assert entries[1].time_status == "range" and entries[1].date_basis == "header"
    # A closed fence protects the date line inside it: no 2027-03-14 entry.
    assert {e.date_local for e in entries} == {date(2027, 3, 12), date(2027, 3, 13)}
    assert entries[-1].time_status == "invalid_range"
    assert "fence_unterminated" not in codes(parsed)

    def anns(entry):
        return {(a.kind, a.subtype, a.value) for a in entry.annotations}

    goal = next(e for e in entries if "/goal" in e.text)
    assert anns(goal) == {("command_hint", "none", "/goal")}   # not a path
    idea = next(e for e in entries if "IDEA:" in e.text)
    assert ("idea_hint", "none", "IDEA:") in anns(idea)
    issue = next(e for e in entries if "issue #41" in e.text)
    assert ("identifier", "issue", "#41") in anns(issue)
    tech = next(e for e in entries if "embeddinggemma" in e.text)
    assert {("identifier", "model", "embeddinggemma:300m"),
            ("identifier", "hash", "3fa9c2e1"),
            ("identifier", "symbol", "load_index"),
            ("pasted", "none", "fenced")} <= anns(tech)
    url = next(e for e in entries if "threat-model" in e.text)
    assert ("identifier", "url", "https://example.org/notes/threat-model") in anns(url)


def test_changelog_fixture():
    raw = fixture("archive/ChangeLog.txt")
    parsed = parse_file(raw, "archive/ChangeLog.txt", CFG)
    assert parsed.dialect == "changelog"
    dates = [e.date_local for e in parsed.entries]
    assert dates == [date(2027, 7, 27)] * 3 + [date(2027, 7, 26)] + [date(2027, 7, 25)] * 2
    assert {e.author_token for e in parsed.entries} == {"kreese"}
    assert parsed.entries[0].text.count("\n") == 3   # indented continuations kept
    assert parsed.entries[4].text.startswith("Notes before")   # pre-bullet, date-only
    assert parsed.diagnostics == []   # newest-first order is not a regression
    symbols = {a.value for e in parsed.entries for a in e.annotations if a.subtype == "symbol"}
    assert symbols == {"Edit::VSpace#move_left", "Buffer#test_exception_xxx"}
    paths = {a.value for e in parsed.entries for a in e.annotations if a.subtype == "path"}
    assert paths == set()   # "true/false", "undo/redo" are prose


def test_daily_fixture_terminal_region_suppresses_four_digit_line():
    raw = fixture("daily/2027_07_26.txt")
    parsed = parse_file(raw, "daily/2027_07_26.txt", CFG)
    assert [e.clock_start for e in parsed.entries] == [time(8, 30), time(9, 10)]
    assert all(e.date_local == date(2027, 7, 26) and e.date_basis == "filename"
               for e in parsed.entries)
    assert codes(parsed) == ["possible_boundary_in_terminal"]
    second = parsed.entries[1]
    assert second.text.rstrip().endswith("the repository is locked.")
    pasted = [a for a in second.annotations if a.kind == "pasted"]
    assert [a.value for a in pasted] == ["terminal_likely"]
    assert raw[pasted[0].byte_start:pasted[0].byte_end].startswith(b"www:/srv/skynet/www# svn st")


# --- byte fidelity --------------------------------------------------------------


def test_bom_and_crlf_offsets():
    body = "20270312\r\n09h30\r\nRücken\r\n\r\n10h00\r\nzwei\r\n".encode()
    raw = BOM + body
    parsed = parse_file(raw, "current/x.txt", CFG)
    assert_well_formed(raw, parsed)
    assert parsed.coverage[0] == (0, 3, "separator")
    first = parsed.entries[0]
    assert first.byte_start == raw.index(b"09h30")
    assert first.text == "09h30\r\nRücken\r\n"
    assert first.context_ranges == [(3, 3 + len(b"20270312\r\n"))]


def test_empty_and_bom_only_files():
    assert parse_file(b"", "current/x.txt", CFG).coverage == []
    only_bom = parse_file(BOM, "current/x.txt", CFG)
    assert only_bom.coverage == [(0, 3, "separator")] and only_bom.entries == []


def test_nul_and_invalid_utf8_quarantine():
    nul = parse_file(b"ab\x00cd", "current/x.txt", CFG)
    assert nul.status == "quarantined" and nul.diagnostics[0].byte_offset == 2
    bad = parse_file(BOM + b"ok\n\xff\n", "current/x.txt", CFG)
    assert bad.status == "quarantined" and codes(bad) == ["invalid_utf8"]
    assert bad.diagnostics[0].byte_offset == 6


# --- chunking ---------------------------------------------------------------------


def test_long_entries_chunk_at_line_ends_then_at_the_cap():
    lines = "".join(f"line {i:03d} " + "x" * 90 + "\n" for i in range(20))
    one_line = "y" * 1600 + "\n"
    raw = f"20270312\n09h00\n{lines}\n10h00\n{one_line}".encode()
    parsed = parse_file(raw, "current/x.txt", CFG)
    assert_well_formed(raw, parsed)
    multi, single = parsed.entries
    assert len(multi.passages) > 1
    assert all(p.text.endswith("\n") for p in multi.passages)
    assert [len(p.text) for p in single.passages] == [600, 600, 407]


def test_short_entry_is_one_passage():
    raw = b"20270312\n09h00\nshort\n"
    entry = parse_file(raw, "current/x.txt", CFG).entries[0]
    assert len(entry.passages) == 1 and entry.passages[0].text == entry.text


# --- protected regions ---------------------------------------------------------------


@pytest.mark.parametrize("line, matches", [
    ("www:/srv/skynet/www# svn st", True),
    ("www:/srv/skynet/www#", True),
    ("$ ls", True),
    ("https://example.org/notes/threat-model#top", False),
    ("note: /tmp is full # really", False),
    ("host:/path#", True),   # known false positive, documented in the proposal
])
def test_terminal_start_regex(line, matches):
    assert bool(TERMINAL_START.match(line)) is matches


def test_writing_past_midnight_is_not_a_regression():
    raw = b"20270312\n22h00\na\n\n23h30 - 24h15\nb\n\n00h30\nc\n\n24h40\nd\n\n00h10\ne\n"
    parsed = parse_file(raw, "current/x.txt", CFG)
    assert [e.clock_start for e in parsed.entries] == \
        [time(22, 0), time(23, 30), time(0, 30), time(0, 40), time(0, 10)]
    assert parsed.entries[1].time_status == "range"          # 24h15 is not invalid
    assert parsed.entries[1].clock_end == time(0, 15)
    assert codes(parsed) == ["clock_regression"]             # only 00h10 after 00h40
    assert parsed.diagnostics[0].byte_offset == raw.index(b"00h10")


def test_real_regression_in_the_afternoon_still_flags():
    raw = b"20270312\n16h30 - 17h50\na\n\n15h00 - 19h00\nb\n"
    assert codes(parse_file(raw, "current/x.txt", CFG)) == ["clock_regression"]


@pytest.mark.parametrize("clock", ["0830", "08:30", "8:30"])
def test_daily_accepts_colon_times(clock):
    raw = f"{clock}\nfirst\n\n09:10\nsecond\n".encode()
    parsed = parse_file(raw, "daily/2027_07_26.txt", CFG)
    assert [e.clock_start for e in parsed.entries] == [time(8, 30), time(9, 10)]


def test_timed_time_line_after_blank_ends_terminal_region():
    raw = b"20270312\n09h00\n$ make\nbuilding\n\n10h00\nnext entry\n"
    parsed = parse_file(raw, "current/x.txt", CFG)
    assert [e.clock_start for e in parsed.entries] == [time(9, 0), time(10, 0)]


def test_unterminated_fence_closes_at_cap():
    filler = ("z" * 99 + "\n") * (PROTECTED_REGION_CAP // 100 + 5)
    raw = f"20270312\n09h00\n```\n{filler}10h00\nafter\n".encode()
    parsed = parse_file(raw, "current/x.txt", CFG)
    assert "fence_unterminated" in codes(parsed)
    assert parsed.entries[-1].clock_start == time(10, 0)


def test_fence_closer_beyond_cap_is_not_honoured():
    filler = ("z" * 99 + "\n") * (PROTECTED_REGION_CAP // 100 + 5)
    raw = f"20270312\n09h00\n```\n{filler}```\n10h00\nafter\n".encode()
    parsed = parse_file(raw, "current/x.txt", CFG)
    assert "fence_unterminated" in codes(parsed)


def test_fenced_date_lines_are_not_headers():
    raw = b"20270312\n09h00\n~~~~\n20270313\n10h00\n~~~~\n"
    parsed = parse_file(raw, "current/x.txt", CFG)
    assert len(parsed.entries) == 1 and codes(parsed) == []


# --- overrides -----------------------------------------------------------------------


def with_override(raw: bytes, path: str, **ov):
    body = {"content_sha256": sha256_hex(raw)}
    body.update(ov)
    return config(file_overrides={path: body})


def test_override_forces_and_suppresses_boundaries():
    raw = b"20270312\n09h00\none\n10h00\ntwo\n"
    plain = parse_file(raw, "current/x.txt", CFG)
    assert len(plain.entries) == 2
    suppressed = parse_file(raw, "current/x.txt", with_override(
        raw, "current/x.txt", suppress_boundary_offsets=[raw.index(b"10h00")]))
    assert len(suppressed.entries) == 1 and codes(suppressed) == []


def test_forced_boundary_inside_terminal_region():
    raw = b"0900\n$ tail -f log\n1000 lines\n1000\nnot output\n"
    cfg = with_override(raw, "daily/2027_07_26.txt",
                        force_boundary_offsets=[raw.index(b"1000\nnot")])
    parsed = parse_file(raw, "daily/2027_07_26.txt", cfg)
    assert [e.clock_start for e in parsed.entries] == [time(9, 0), time(10, 0)]


def test_explicit_pasted_range_protects_and_annotates():
    raw = b"20270312\n09h00\nlog:\n10h00 started\n10h05\nreal entry\n"
    start = raw.index(b"10h00 started")
    end = raw.index(b"10h05")
    cfg = with_override(raw, "current/x.txt", pasted_ranges=[[start, end]])
    parsed = parse_file(raw, "current/x.txt", cfg)
    pasted = [a for e in parsed.entries for a in e.annotations if a.kind == "pasted"]
    assert [(a.value, a.basis, a.byte_start, a.byte_end) for a in pasted] == \
        [("explicit", "explicit_override", start, end)]
    assert [e.clock_start for e in parsed.entries] == [time(9, 0), time(10, 5)]


def test_stale_override_is_skipped_not_quarantined():
    raw = b"20270312\n09h00\none\n10h00\ntwo\n"
    cfg = config(file_overrides={"current/x.txt": {
        "content_sha256": "0" * 64, "suppress_boundary_offsets": [raw.index(b"10h00")]}})
    parsed = parse_file(raw, "current/x.txt", cfg)
    assert parsed.status == "ok" and len(parsed.entries) == 2
    assert codes(parsed) == ["override_stale"]
    assert parsed.diagnostics[0].detail["actual_sha256"] == sha256_hex(raw)


def test_invalid_override_offset_is_skipped():
    raw = b"20270312\n09h00\none\n10h00\ntwo\n"
    cfg = with_override(raw, "current/x.txt", suppress_boundary_offsets=[3])
    parsed = parse_file(raw, "current/x.txt", cfg)
    assert parsed.status == "ok" and len(parsed.entries) == 2
    assert codes(parsed) == ["override_invalid"]


# --- dates, clocks and context -----------------------------------------------------------


def test_text_before_first_date_is_undated_and_filename_date_fills_in():
    raw = b"preamble\n\n20270312\n09h00\nx\n"
    parsed = parse_file(raw, "current/x.txt", CFG)
    assert parsed.entries[0].date_local is None and parsed.entries[0].time_status == "unknown"
    named = parse_file(raw, "current/20270311.txt", CFG)
    assert named.entries[0].date_local == date(2027, 3, 11)
    assert named.entries[0].date_basis == "filename"
    assert named.entries[1].date_local == date(2027, 3, 12)   # header wins


@pytest.mark.parametrize("clock, status", [("02h30", "ambiguous"), ("03h30", "minute")])
def test_dst_gap_is_ambiguous(clock, status):
    raw = f"20270328\n{clock}\nx\n".encode()   # spring forward in Europe/Copenhagen
    assert parse_file(raw, "current/x.txt", CFG).entries[0].time_status == status


def test_dst_overlap_is_ambiguous():
    raw = b"20271031\n02h30\nx\n"
    assert parse_file(raw, "current/x.txt", CFG).entries[0].time_status == "ambiguous"


def test_plain_dialect_blocks():
    raw = b"first block\nstill first\n\nsecond block\n"
    parsed = parse_file(raw, "notes/2027_03_12.txt", CFG)
    assert parsed.dialect == "plain"
    assert [e.text for e in parsed.entries] == ["first block\nstill first\n", "second block\n"]
    assert all(e.date_local == date(2027, 3, 12) and e.clock_start is None for e in parsed.entries)


# --- one fixture per diagnostic code ------------------------------------------------------


def _stale_cfg():
    return config(file_overrides={"current/x.txt": {"content_sha256": "0" * 64}})


DIAGNOSTIC_CASES = {
    "nul_byte": (b"a\x00", "current/x.txt", None),
    "invalid_utf8": (b"\xff", "current/x.txt", None),
    "override_stale": (b"20270312\n", "current/x.txt", _stale_cfg),
    "override_invalid": (b"20270312\n09h00\n", "current/x.txt",
                         lambda: with_override(b"20270312\n09h00\n", "current/x.txt",
                                               force_boundary_offsets=[1])),
    "fence_unterminated": (b"20270312\n09h00\n```\nopen\n20270313\n10h00\nx\n",
                           "current/x.txt", None),
    "possible_boundary_in_terminal": (b"0900\n$ ls\n\n1234\n", "daily/2027_07_26.txt", None),
    "invalid_date": (b"20271340\n", "current/x.txt", None),
    "invalid_time": (b"20270312\n25h00\n", "current/x.txt", None),
    "unknown_month": (b"27-foo-2027 kreese\n", "archive/ChangeLog.txt", None),
    "filename_header_mismatch": (b"20270313\n09h00\nx\n", "current/20270312.txt", None),
    "invalid_daily_filename": (b"0900\nx\n", "daily/notes.txt", None),
    "clock_regression": (b"20270312\n10h00\na\n\n09h00\nb\n", "current/x.txt", None),
    "date_regression": (b"20270313\n09h00\na\n20270312\n09h00\nb\n", "current/x.txt", None),
}


def test_every_diagnostic_code_has_a_case():
    assert set(DIAGNOSTIC_CASES) == DIAGNOSTIC_CODES


@pytest.mark.parametrize("code", sorted(DIAGNOSTIC_CASES))
def test_diagnostic_case(code):
    raw, path, make_cfg = DIAGNOSTIC_CASES[code]
    parsed = parse_file(raw, path, make_cfg() if make_cfg else CFG)
    assert code in codes(parsed)
    if parsed.status == "ok":
        assert_well_formed(raw, parsed)
    for d in parsed.diagnostics:
        assert all(isinstance(v, (int, str)) and (not isinstance(v, str) or len(v) == 64)
                   for v in d.detail.values()), "diagnostics carry offsets/hashes only"


def test_fence_breaker_closes_before_next_date_header():
    raw, path, _ = DIAGNOSTIC_CASES["fence_unterminated"]
    parsed = parse_file(raw, path, CFG)
    assert parsed.entries[-1].date_local == date(2027, 3, 13)


def test_changelog_date_regression_is_newest_first():
    raw = b"26-juli-2027 kreese\n*\ta\n27-juli-2027 kreese\n*\tb\n"
    assert "date_regression" in codes(parse_file(raw, "archive/ChangeLog.txt", CFG))
