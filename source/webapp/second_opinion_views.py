"""The /second-opinion page — the pre-execution gate's judgments as a working
surface, and where the operator records their own assessment of each one.

It answers the two questions the review records exist for
(notes/proposals/2026-07-28-second-opinion-review-records.md):

- *Why did this run go wrong?* Filter by verdict and follow a row into its
  trace. `skipped` / `error` separate "the gate never ran" from "the gate
  approved it" — the same bug looks identical without that split.
- *Why was this right for the wrong reasons?* `verdict=approved` +
  `category=identity_mismatch`: the reviewer saw the ground the gate exists for
  and let the program through anyway.

Server-rendered with GET filters rather than a JS-hydrated table like
/assistant-overview: review volume is low, the operator reads and judges rather
than scans thousands of rows, and a plain form keeps the assessment write
honest without an inline-script escaping hazard.
"""
from datetime import UTC, datetime, timedelta
from uuid import UUID

from flask import redirect, render_template_string, request

import db
from agents.assistant import SecondOpinionProblem, problem_texts

from .core import app

_VERDICTS = ("approved", "rejected", "skipped", "error")
_CATEGORIES = tuple(
    SecondOpinionProblem.model_fields["category"].annotation.__args__
)
_ASSESSMENTS = ("agree", "over_blocked", "under_blocked", "unsure")
# What each assessment means, on the control itself — the labels are only
# useful if their meaning is at hand while judging.
_ASSESSMENT_HELP = {
    "agree": "the verdict was right",
    "over_blocked": "rejected something fine; cost a step for nothing",
    "under_blocked": "approved something that should have been stopped",
    "unsure": "looked at it, could not decide",
}
_RANGE_DELTAS = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}
_PER_PAGE = 25

SECOND_OPINION_TEMPLATE = """
<!doctype html>
<title>Second opinion &mdash; rainbox</title>
<style>
  body{font-family:system-ui,sans-serif;margin:0;background:#fbfbfb;color:#374151}
  .so-wrap{max-width:1320px;margin:0 auto;padding:24px 28px 56px}
  .so-head{margin:0 0 4px;font-size:1.35rem;color:#1a1a2e}
  .so-lede{margin:0 0 20px;font-size:0.86rem;color:#6b7280;max-width:70ch}
  .so-filters{display:flex;flex-wrap:wrap;align-items:flex-end;gap:14px;margin-bottom:18px}
  .so-field{display:flex;flex-direction:column;gap:5px}
  .so-field label{font-size:0.68rem;font-weight:700;text-transform:uppercase;
    letter-spacing:0.05em;color:#9ca3af}
  .so-select{padding:7px 11px;border:1px solid #e5e7eb;border-radius:6px;background:#fff;
    font:inherit;font-size:0.88rem;color:#1a1a2e;cursor:pointer}
  .so-select:focus{outline:none;border-color:#2563eb}
  .so-go{padding:7px 15px;border:1px solid #2563eb;border-radius:6px;background:#2563eb;
    color:#fff;font:inherit;font-size:0.88rem;font-weight:600;cursor:pointer}
  .so-chips{display:flex;flex-wrap:wrap;gap:7px;margin-bottom:18px}
  .so-chip{display:inline-flex;align-items:center;gap:7px;font-size:0.75rem;font-weight:600;
    padding:5px 12px;border-radius:999px;text-decoration:none;color:#6b7280;background:#eef0f4}
  .so-chip.sel{outline:2px solid #2563eb;outline-offset:1px}
  .so-chip .n{font-variant-numeric:tabular-nums;opacity:0.75}
  .so-chip.approved{color:#166534;background:#dcfce7}
  .so-chip.rejected{color:#b91c1c;background:#fee2e2}
  .so-chip.skipped{color:#92400e;background:#fef3c7}
  .so-chip.error{color:#b91c1c;background:#fee2e2}
  .so-card{border:1px solid #e5e7eb;border-radius:8px;background:#fff;margin-bottom:12px}
  .so-card .hd{display:flex;align-items:center;gap:12px;flex-wrap:wrap;padding:10px 14px;
    background:#fbfdff;border-bottom:1px solid #e5e7eb}
  .so-verdict{font-size:0.75rem;font-weight:700;padding:3px 10px;border-radius:999px}
  .so-verdict.approved{color:#166534;background:#dcfce7}
  .so-verdict.rejected{color:#b91c1c;background:#fee2e2}
  .so-verdict.skipped{color:#92400e;background:#fef3c7}
  .so-verdict.error{color:#b91c1c;background:#fee2e2}
  .so-when{font-size:0.76rem;color:#9ca3af;font-family:ui-monospace,Menlo,monospace}
  .so-trace{margin-left:auto;font-size:0.82rem;color:#2563eb;text-decoration:none}
  .so-trace:hover{text-decoration:underline}
  .so-body{padding:12px 14px}
  .so-prob{margin:0 0 6px;font-size:0.88rem;color:#1a1a2e}
  .so-prob:last-of-type{margin-bottom:0}
  .so-cat{font-size:0.7rem;font-weight:700;color:#6b7280;text-transform:uppercase;
    letter-spacing:0.04em;margin-right:7px}
  .so-none{font-size:0.85rem;color:#9ca3af;font-style:italic;margin:0}
  .so-meta{margin-top:10px;font-size:0.74rem;color:#9ca3af;display:flex;gap:14px;flex-wrap:wrap}
  .so-assess{display:flex;align-items:center;gap:8px;flex-wrap:wrap;
    padding:10px 14px;border-top:1px solid #f1f3f5;background:#fcfcfd}
  .so-assess input[type=text]{flex:1 1 240px;min-width:160px;padding:6px 10px;
    border:1px solid #e5e7eb;border-radius:6px;font:inherit;font-size:0.84rem}
  .so-assess button{padding:6px 13px;border:1px solid #cbd5e1;border-radius:6px;
    background:#fff;font:inherit;font-size:0.84rem;cursor:pointer;color:#374151}
  .so-assess button:hover{border-color:#9aa3af;color:#1a1a2e}
  .so-verdicted{font-size:0.78rem;color:#374151}
  .so-verdicted b{color:#1a1a2e}
  .so-empty{border:1px dashed #d1d5db;border-radius:8px;padding:52px 24px;
    text-align:center;background:#fff}
  .so-empty .t{font-size:0.95rem;color:#1a1a2e;font-weight:600;margin-bottom:6px}
  .so-empty .s{font-size:0.8rem;color:#6b7280}
  .so-pager{display:flex;gap:8px;align-items:center;margin-top:18px;font-size:0.82rem}
  .so-pager a{color:#2563eb;text-decoration:none}
</style>
{% include "_nav.html" %}
<style>.pp-nav{margin-bottom:0}</style>
<div class="so-wrap">
  <h1 class="so-head">Second opinion</h1>
  <p class="so-lede">Every pre-execution review of a gated action. An approval
    that still lists problems is the one to look at: the reviewer saw something
    and let the program run anyway.</p>

  <form class="so-filters" method="get" action="/second-opinion">
    <div class="so-field">
      <label for="so-category">Category</label>
      <select class="so-select" id="so-category" name="category">
        <option value="all">Any category</option>
        {% for c in categories %}
        <option value="{{ c }}" {{ 'selected' if c == category }}>{{ c }}</option>
        {% endfor %}
      </select>
    </div>
    <div class="so-field">
      <label for="so-assessed">Assessed</label>
      <select class="so-select" id="so-assessed" name="assessed">
        <option value="all" {{ 'selected' if assessed == 'all' }}>Any</option>
        <option value="no" {{ 'selected' if assessed == 'no' }}>Not yet judged</option>
        <option value="yes" {{ 'selected' if assessed == 'yes' }}>Judged</option>
      </select>
    </div>
    <div class="so-field">
      <label for="so-range">When</label>
      <select class="so-select" id="so-range" name="range">
        <option value="all" {{ 'selected' if range_ == 'all' }}>Any time</option>
        <option value="24h" {{ 'selected' if range_ == '24h' }}>Last 24 hours</option>
        <option value="7d" {{ 'selected' if range_ == '7d' }}>Last 7 days</option>
        <option value="30d" {{ 'selected' if range_ == '30d' }}>Last 30 days</option>
      </select>
    </div>
    <input type="hidden" name="verdict" value="{{ verdict }}">
    <button class="so-go" type="submit">Apply</button>
  </form>

  <div class="so-chips">
    <a class="so-chip {{ 'sel' if verdict == 'all' }}"
       href="{{ chip_hrefs['all'] }}">all <span class="n">{{ counts['all'] }}</span></a>
    {% for v in verdicts %}
    <a class="so-chip {{ v }} {{ 'sel' if verdict == v }}"
       href="{{ chip_hrefs[v] }}">{{ v }} <span class="n">{{ counts[v] }}</span></a>
    {% endfor %}
  </div>

  {% if not rows %}
  <div class="so-empty">
    <div class="t">No reviews match these filters</div>
    <div class="s">Try another verdict, category, or time range.</div>
  </div>
  {% endif %}

  {% for r in rows %}
  <div class="so-card">
    <div class="hd">
      <span class="so-verdict {{ r.verdict }}">{{ r.verdict }}</span>
      <span class="so-when">{{ r.created_at.strftime('%Y-%m-%d %H:%M:%S') }}</span>
      <span class="so-when">{{ r.action }}</span>
      <a class="so-trace" href="{{ r.trace_href }}">trace &#8599;</a>
    </div>
    <div class="so-body">
      {% for p in r.problems_view %}
      <p class="so-prob"><span class="so-cat">{{ p.category }}</span>{{ p.text }}</p>
      {% endfor %}
      {% if not r.problems_view %}
      <p class="so-none">{{ r.no_problem_note }}</p>
      {% endif %}
      <div class="so-meta">
        {% if r.group_from %}<span>group: {{ r.group_from }}</span>{% endif %}
        {% if r.input_tokens %}<span>in {{ r.input_tokens }}</span>{% endif %}
        {% if r.output_tokens %}<span>out {{ r.output_tokens }}</span>{% endif %}
        {% if r.duration_ms %}<span>{{ r.duration_ms }} ms</span>{% endif %}
      </div>
    </div>
    <form class="so-assess" method="post" action="{{ assess_action }}">
      <input type="hidden" name="review_uuid" value="{{ r.uuid }}">
      {% if r.assessment %}
      <span class="so-verdicted">judged <b>{{ r.assessment.assessment }}</b>{% if r.assessment.note %} &mdash; {{ r.assessment.note }}{% endif %}</span>
      {% endif %}
      <select class="so-select" name="assessment" aria-label="Your assessment">
        {% for a in assessments %}
        <option value="{{ a }}" title="{{ assessment_help[a] }}">{{ a }}</option>
        {% endfor %}
      </select>
      <input type="text" name="note" placeholder="Why &mdash; for future you">
      <button type="submit">Save</button>
    </form>
  </div>
  {% endfor %}

  {% if pages > 1 %}
  <div class="so-pager">
    {% if page > 1 %}<a href="{{ prev_href }}">&larr; Newer</a>{% endif %}
    <span>Page {{ page }} of {{ pages }} &middot; {{ total }} reviews</span>
    {% if page < pages %}<a href="{{ next_href }}">Older &rarr;</a>{% endif %}
  </div>
  {% endif %}
</div>
"""


def _filters() -> tuple[dict, str | None]:
    """Validated filter state from the query string, or (partial, error)."""
    verdict = request.args.get("verdict", "all")
    category = request.args.get("category", "all")
    assessed = request.args.get("assessed", "all")
    range_ = request.args.get("range", "all")
    if verdict != "all" and verdict not in _VERDICTS:
        return {}, "bad verdict"
    if category != "all" and category not in _CATEGORIES:
        return {}, "bad category"
    if assessed not in ("all", "yes", "no"):
        return {}, "bad assessed"
    if range_ != "all" and range_ not in _RANGE_DELTAS:
        return {}, "bad range"
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        return {}, "bad page"
    return {"verdict": verdict, "category": category, "assessed": assessed,
            "range_": range_, "page": page}, None


def _query_string(f: dict, **over) -> str:
    """The filter state as a query string, so chips, the pager and the
    post-assessment redirect all land back on the same view."""
    state = {"verdict": f["verdict"], "category": f["category"],
             "assessed": f["assessed"], "range": f["range_"], "page": f["page"]}
    state.update(over)
    return "&".join(f"{k}={v}" for k, v in state.items())


def _row_view(row, assessment) -> dict:
    """One review flattened for the template. `problems_view` pairs each
    finding's category with its text; `no_problem_note` says why a card has no
    findings, which differs by verdict — a clean approval, a rejection with no
    stated problem, or a gate that never ran."""
    texts = problem_texts(row.problems)
    problems_view = [
        {"category": (p or {}).get("category", "other") if isinstance(p, dict)
         else "other", "text": t}
        for p, t in zip(row.problems or [], texts)
    ]
    if row.verdict == "skipped":
        note = f"the gate did not run: {row.skip_reason or 'reason not recorded'}"
    elif row.verdict == "error":
        note = f"the review failed open: {row.error or 'error not recorded'}"
    elif row.verdict == "rejected":
        note = "rejected without naming a problem"
    else:
        note = "no problems found"
    href = f"/assistant?id={row.run_uuid}"
    if row.step_uuid:
        href += f"#step-{row.step_uuid}"
    return {
        "uuid": str(row.uuid), "verdict": row.verdict, "action": row.action,
        "created_at": row.created_at, "problems_view": problems_view,
        "no_problem_note": note, "trace_href": href,
        "group_from": row.group_from, "input_tokens": row.input_tokens,
        "output_tokens": row.output_tokens, "duration_ms": row.duration_ms,
        "assessment": assessment,
    }


@app.route("/second-opinion")
def second_opinion_page():
    f, error = _filters()
    if error is not None:
        return error, 400
    delta = _RANGE_DELTAS.get(f["range_"])
    since = datetime.now(UTC) - delta if delta else None
    rows, total, counts = db.list_second_opinion_reviews_page(
        verdict=f["verdict"], category=f["category"], assessed=f["assessed"],
        since=since, offset=(f["page"] - 1) * _PER_PAGE, limit=_PER_PAGE,
    )
    assessments = db.second_opinion_assessments_for([r.uuid for r in rows])
    views = [_row_view(r, assessments.get(r.uuid)) for r in rows]
    pages = max(1, -(-total // _PER_PAGE))
    chip_hrefs = {
        v: "/second-opinion?" + _query_string(f, verdict=v, page=1)
        for v in ("all", *_VERDICTS)
    }
    return render_template_string(
        SECOND_OPINION_TEMPLATE,
        rows=views, counts=counts, total=total, pages=pages, page=f["page"],
        verdict=f["verdict"], category=f["category"], assessed=f["assessed"],
        range_=f["range_"], verdicts=_VERDICTS, categories=_CATEGORIES,
        assessments=_ASSESSMENTS, assessment_help=_ASSESSMENT_HELP,
        chip_hrefs=chip_hrefs,
        assess_action="/second-opinion/assess?" + _query_string(f),
        prev_href="/second-opinion?" + _query_string(f, page=f["page"] - 1),
        next_href="/second-opinion?" + _query_string(f, page=f["page"] + 1),
    )


@app.route("/second-opinion/assess", methods=["POST"])
def second_opinion_assess():
    """Record the operator's judgment of one review and return to the same
    filtered view, so working through a backlog does not reset the filters."""
    assessment = request.form.get("assessment", "")
    if assessment not in _ASSESSMENTS:
        return "bad assessment", 400
    try:
        review_uuid = UUID(request.form.get("review_uuid", ""))
    except ValueError:
        return "bad review", 400
    if db.get_second_opinion_review(review_uuid) is None:
        return "no such review", 404
    db.record_second_opinion_assessment(
        review_uuid, assessment, note=request.form.get("note", "").strip())
    f, error = _filters()
    query = "" if error is not None else "?" + _query_string(f)
    return redirect(f"/second-opinion{query}")
