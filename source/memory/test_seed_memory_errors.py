"""Q&A JSONL parse errors must name the file and line so the operator can fix
the offending entry (surfaced in the /settings repopulate result and the log)."""
import json

import pytest

import memory.seed_memory as seed_memory


def _write(p, lines):
    p.write_text("".join(l + "\n" for l in lines))


def test_load_jsonl_reports_file_and_line_on_bad_json(tmp_path, monkeypatch):
    p = tmp_path / "question_answer.jsonl"
    _write(p, [
        '{"id": "a", "questions": ["ok"], "answer": "x"}',
        '{"id": "b", "questions": ["ok"], "answer": "y"}',
        # line 3: two array strings with no comma between them
        '{"id": "c", "questions": ["bad" "missing comma"], "answer": "z"}',
    ])
    monkeypatch.setattr(seed_memory, "QA_JSONL_PATH", p)
    monkeypatch.setattr(seed_memory, "_overlay_path", lambda: None)

    with pytest.raises(ValueError) as ei:
        seed_memory._load_jsonl()
    msg = str(ei.value)
    assert str(p) in msg, f"error should name the file: {msg!r}"
    assert ":3" in msg, f"error should name the 1-based line number: {msg!r}"


def test_load_jsonl_reports_line_in_overlay_file(tmp_path, monkeypatch):
    base = tmp_path / "base.jsonl"
    _write(base, ['{"id": "a", "questions": ["ok"], "answer": "x"}'])
    overlay = tmp_path / "overlay.jsonl"
    _write(overlay, [
        '{"id": "b", "questions": ["ok"], "answer": "y"}',
        '{"id": "c", "questions": ["x"], "answer": }',  # line 2: malformed
    ])
    monkeypatch.setattr(seed_memory, "QA_JSONL_PATH", base)
    monkeypatch.setattr(seed_memory, "_overlay_path", lambda: overlay)

    with pytest.raises(ValueError) as ei:
        seed_memory._load_jsonl()
    msg = str(ei.value)
    assert str(overlay) in msg and ":2" in msg, msg


def test_load_jsonl_refuses_duplicate_id_within_file(tmp_path, monkeypatch):
    p = tmp_path / "question_answer.jsonl"
    _write(p, [
        '{"id": "a", "questions": ["ok"], "answer": "x"}',
        '{"id": "b", "questions": ["ok"], "answer": "y"}',
        # line 3: reuses id "a" — would silently overwrite line 1's answer
        '{"id": "a", "questions": ["dup"], "answer": "z"}',
    ])
    monkeypatch.setattr(seed_memory, "QA_JSONL_PATH", p)
    monkeypatch.setattr(seed_memory, "_overlay_path", lambda: None)

    with pytest.raises(ValueError) as ei:
        seed_memory._load_jsonl()
    msg = str(ei.value)
    assert str(p) in msg, f"error should name the file: {msg!r}"
    assert ":3" in msg, f"error should name the duplicate's line: {msg!r}"
    assert "line 1" in msg, f"error should name the first line: {msg!r}"
    assert "'a'" in msg, f"error should name the duplicate id: {msg!r}"


def test_load_jsonl_allows_overlay_to_override_base_id(tmp_path, monkeypatch):
    # Cross-file id reuse is intentional (overlay overrides base), so the
    # per-file duplicate check must NOT fire across the base+overlay merge.
    base = tmp_path / "base.jsonl"
    _write(base, ['{"id": "a", "questions": ["ok"], "answer": "base"}'])
    overlay = tmp_path / "overlay.jsonl"
    _write(overlay, ['{"id": "a", "questions": ["ok"], "answer": "overlay"}'])
    monkeypatch.setattr(seed_memory, "QA_JSONL_PATH", base)
    monkeypatch.setattr(seed_memory, "_overlay_path", lambda: overlay)

    entries = seed_memory._load_jsonl()
    assert [e["answer"] for e in entries] == ["overlay"]


def test_load_jsonl_ok_when_valid(tmp_path, monkeypatch):
    p = tmp_path / "question_answer.jsonl"
    _write(p, ['{"id": "a", "questions": ["ok"], "answer": "x"}'])
    monkeypatch.setattr(seed_memory, "QA_JSONL_PATH", p)
    monkeypatch.setattr(seed_memory, "_overlay_path", lambda: None)
    entries = seed_memory._load_jsonl()
    assert [e["id"] for e in entries] == ["a"]
