"""The /memory/qa page — one Q&A registry entry, read-only, by its id.

The registry lives in jsonl files (the base data/question_answer.jsonl and the
operator's customize-dir overlay), not in a table with pages of its own, so
this is where a Q&A entry's `?id=` deep link lands: Find's Open link, a reply
source's link. It shows what retrieval reads — the entry's path, kind,
questions, answer or handler — and which file it came from.

A shielded entry is shown only while its shield is unlocked
(qa.unlocked_shields), exactly as retrieval and Find honor shields: a locked
entry reads as not found, never as "hidden here", so the page says nothing
about what a shield covers.
"""

from flask import render_template_string, request

from .core import app

QA_ENTRY_TEMPLATE = """
<!doctype html>
<title>Q&amp;A entry &mdash; rainbox</title>
{% include "_nav.html" %}
<style>
  body { margin: 0; font-family: system-ui, sans-serif; }
  .pp-qa { max-width: 900px; margin: 1rem auto; padding: 0 1rem; }
  .pp-qa h1 { margin: 0.2rem 0 0.8rem; }
  .pp-qa .status { color: #6c757d; }
  .pp-qa table { border-collapse: collapse; width: 100%; }
  .pp-qa th { text-align: left; vertical-align: top; white-space: nowrap;
              font-size: 0.8rem; color: #6c757d; text-transform: uppercase;
              letter-spacing: 0.04em; padding: 0.55rem 1rem 0.55rem 0;
              border-bottom: 1px solid #eee; width: 1%; }
  .pp-qa td { padding: 0.5rem 0; border-bottom: 1px solid #eee; vertical-align: top; }
  .pp-qa td.answer { white-space: pre-wrap; }
  .pp-qa ul { margin: 0; padding-left: 1.2rem; }
  .pp-qa code { font-family: ui-monospace, monospace; font-size: 0.85rem;
                background: #f6f8fa; padding: 1px 5px; border-radius: 4px; }
</style>
<main class="pp-qa">
  <h1>Q&amp;A entry</h1>
  {% if entry %}
  <table>
    <tr><th>Id</th><td><code>{{ entry.id }}</code></td></tr>
    <tr><th>Path</th><td><code>{{ entry.path or '' }}</code></td></tr>
    <tr><th>Kind</th><td>{{ entry.kind or '' }}</td></tr>
    <tr><th>Questions</th><td>
      <ul>{% for q in entry.questions or [] %}<li>{{ q }}</li>{% endfor %}</ul>
    </td></tr>
    {% if entry.answer %}
    <tr><th>Answer</th><td class="answer">{{ entry.answer }}</td></tr>
    {% endif %}
    {% if entry.handler %}
    <tr><th>Handler</th><td><code>{{ entry.handler }}</code></td></tr>
    {% endif %}
    <tr><th>File</th><td><code>{{ file }}</code></td></tr>
  </table>
  {% else %}
  <p class="status">No Q&amp;A entry has the id <code>{{ wanted }}</code>.</p>
  {% endif %}
</main>
"""


def qa_entry(entry_id: str) -> tuple[dict | None, str | None]:
    """`(entry, file)` for the visible registry entry with this id, or
    `(None, None)` — no such id, a locked shield, or an unreadable registry."""
    from memory.seed_memory import (QA_JSONL_PATH, _entry_locked, _load_jsonl,
                                    _overlay_path, _unlocked_shields)

    wanted = str(entry_id or "").strip().lower()
    if not wanted:
        return None, None
    try:
        entries = _load_jsonl()
        overlay = _overlay_path()
    except Exception:   # noqa: BLE001 — a malformed overlay reads as not found
        return None, None
    unlocked = _unlocked_shields()
    for entry in entries:
        if str(entry.get("id") or "").lower() != wanted:
            continue
        if _entry_locked(entry, unlocked):
            return None, None
        file = (str(overlay) if entry.get("_source") == "user-overlay" and overlay
                else str(QA_JSONL_PATH))
        return entry, file
    return None, None


@app.route("/memory/qa")
def qa_entry_page() -> str:
    wanted = request.args.get("id", "")
    entry, file = qa_entry(wanted)
    return render_template_string(
        QA_ENTRY_TEMPLATE, entry=entry, file=file, wanted=wanted)
