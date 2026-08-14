"""The /activity page — LLM cache behaviour and call KPIs.

Read-only. Every control is a plain `<select>` in a GET form, so the page's
whole state lives in the query string: a view worth showing someone is a URL
you can paste. No client-side state, no charting library — the bar chart is
inline SVG built server-side.

The cache story this page tells has two halves that must stay visibly
separate. `cached` is what the runtime evidently reused, and on a local
backend that is inferred from prefill timing rather than reported by the
provider. `reusable` is how much of each prompt rainbox had already sent
before, which needs no provider cooperation and so is exact. Blending them
into one number would hide the only diagnostic that matters: whether a low
hit rate is the runtime's fault or our own prompt construction's.

NOTE: this template is a plain (non-raw) Python string — no backslash
escapes in any inline script.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from flask import render_template_string, request

import db
from llm.activity_metrics import MIN_CALIBRATION_CALLS

from .core import app

# --- Controls ---------------------------------------------------------------

# Rolling windows, in the order the picker shows them.
_ROLLING: list[tuple[str, str, timedelta]] = [
    ("15m", "Past 15 minutes", timedelta(minutes=15)),
    ("30m", "Past 30 minutes", timedelta(minutes=30)),
    ("1h", "Past 1 hour", timedelta(hours=1)),
    ("3h", "Past 3 hours", timedelta(hours=3)),
    ("24h", "Past 24 hours", timedelta(hours=24)),
    ("48h", "Past 48 hours", timedelta(hours=48)),
    ("1w", "Past 1 week", timedelta(weeks=1)),
    ("1mo", "Past 1 month", timedelta(days=30)),
    ("1y", "Past 1 year", timedelta(days=365)),
]

# Calendar windows, which answer "how was today?" rather than "how were the
# last 24 hours?" — a different question, and often the one being asked.
_CALENDAR: list[tuple[str, str]] = [
    ("today", "Today"),
    ("yesterday", "Yesterday"),
    ("this_week", "This week"),
    ("prev_week", "Previous week"),
    ("this_month", "This month"),
    ("prev_month", "Previous month"),
]

DEFAULT_RANGE = "24h"

# Bucket widths the chart may choose from, coarsest-last. The chart picks the
# finest width that keeps the bar count readable.
_NICE_BUCKETS: list[int] = [
    60, 300, 900, 1800, 3600, 10800, 21600, 43200, 86400, 604800,
]
_MAX_BARS = 60

# metric key -> (label, bucket field, kind). "stacked" is the cached/uncached
# split; everything else is a single series. `kind` drives both the axis
# formatting and how a value is drawn.
_METRICS: dict[str, tuple[str, str, str]] = {
    "cached_tokens": ("Cached tokens", "cached_tokens", "stacked"),
    "hit_rate": ("Cache hit rate", "hit_rate", "ratio"),
    "prompt_tokens": ("Prompt tokens", "prompt_tokens", "count"),
    "completion_tokens": ("Completion tokens", "completion_tokens", "count"),
    "calls": ("Calls", "calls", "count"),
    "avg_latency_ms": ("Avg latency", "avg_latency_ms", "ms"),
    "p50_latency_ms": ("P50 latency", "p50_latency_ms", "ms"),
    "p90_latency_ms": ("P90 latency", "p90_latency_ms", "ms"),
    "p99_latency_ms": ("P99 latency", "p99_latency_ms", "ms"),
    "avg_throughput_tps": ("Avg throughput", "avg_throughput_tps", "tps"),
    "p50_throughput_tps": ("P50 throughput", "p50_throughput_tps", "tps"),
    "p90_throughput_tps": ("P90 throughput", "p90_throughput_tps", "tps"),
}
DEFAULT_METRIC = "cached_tokens"

_DIMENSION_LABELS = {"model": "Model", "caller": "Caller", "provider": "Provider"}
DEFAULT_DIMENSION = "model"


def resolve_range(key: str, now: datetime) -> tuple[datetime, datetime, str]:
    """(start, end, label) for a picker key. An unknown key falls back to the
    default rather than erroring — a stale bookmark should still render."""
    for k, label, delta in _ROLLING:
        if k == key:
            return now - delta, now, label
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if key == "today":
        return midnight, now, "Today"
    if key == "yesterday":
        return midnight - timedelta(days=1), midnight, "Yesterday"
    if key == "this_week":
        start = midnight - timedelta(days=midnight.weekday())
        return start, now, "This week"
    if key == "prev_week":
        this = midnight - timedelta(days=midnight.weekday())
        return this - timedelta(weeks=1), this, "Previous week"
    if key == "this_month":
        return midnight.replace(day=1), now, "This month"
    if key == "prev_month":
        first = midnight.replace(day=1)
        prev_last = first - timedelta(days=1)
        return prev_last.replace(day=1), first, "Previous month"
    return resolve_range(DEFAULT_RANGE, now)


def pick_bucket_seconds(span_seconds: float) -> int:
    """The finest bucket width that keeps the chart under `_MAX_BARS` bars."""
    for width in _NICE_BUCKETS:
        if span_seconds / width <= _MAX_BARS:
            return width
    return _NICE_BUCKETS[-1]


# --- Formatting -------------------------------------------------------------


# Digit-group separator for four-digit counts. One knob, one place: switch it
# to "," or " " and every grouped number on the page follows.
_GROUP_SEP: str = "."

_SI_UNITS: tuple[tuple[float, str], ...] = ((1e9, "B"), (1e6, "M"), (1e3, "k"))


def si(value: float | int | None) -> str:
    """Compact magnitude, five characters wide from a thousand upwards:
    999, 1.000, 9.999, 10.0k, 999.9k, 8.2M.

    Four-digit counts keep all four digits. Abbreviating 2234 to "2.2k" throws
    away exactly the precision that makes a number comparable against what a
    provider's own dashboard reports, and saves nothing — "1.000" and "10.0k"
    occupy the same width. Above 9999 the abbreviation earns its place."""
    if value is None:
        return "—"
    value = float(value)
    magnitude = abs(value)
    if magnitude < 10_000:
        return f"{value:,.0f}".replace(",", _GROUP_SEP)
    for limit, suffix in _SI_UNITS:
        # 0.9995 promotes the values that would otherwise round up into a
        # nonsense unit, so 999_999 reads as 1.0M and never as 1000.0k.
        if magnitude >= limit * 0.9995:
            return f"{value / limit:.1f}{suffix}"
    return f"{value:,.0f}".replace(",", _GROUP_SEP)


def exact(value: float | int | None, unit: str = "") -> str:
    """Full-precision hover text for a cell that si()/ms() abbreviates.

    Plain digits, ungrouped, so the number can be read or pasted straight
    against a provider's own reporting without anyone having to agree on what
    a separator means."""
    if value is None:
        return "not recorded"
    return f"{value:,.0f}".replace(",", "") + (f" {unit}" if unit else "")


def cached_title(call: Any) -> str:
    """Hover text for a Cached cell, which carries the one distinction the
    number alone hides: whether the provider reported the figure or rainbox
    inferred it from prefill timing."""
    if call.cached_tokens_reported is not None:
        return f"{exact(call.cached_tokens_reported, 'tokens')} — reported by the provider"
    if call.cached_tokens_estimated is not None:
        return (
            f"{exact(call.cached_tokens_estimated, 'tokens')} — estimated from "
            "prefill timing, not reported"
        )
    return "not recorded"


def pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def ms(value: float | None) -> str:
    if value is None:
        return "—"
    if value >= 1000:
        return f"{value / 1000:.1f}s"
    return f"{value:.0f}ms"


def duration(seconds: float | None) -> str:
    """Human-scaled elapsed time, for the seconds-saved panel."""
    if not seconds:
        return "0s"
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


def format_metric(value: float | None, kind: str) -> str:
    if value is None:
        return "—"
    if kind == "ratio":
        return pct(value)
    if kind == "ms":
        return ms(value)
    if kind == "tps":
        return f"{value:.0f} tok/s"
    return si(value)


def _nice_ceiling(value: float) -> float:
    """Round an axis maximum up to 1, 2 or 5 times a power of ten, so the
    gridline labels are numbers a person would have chosen."""
    if value <= 0:
        return 1.0
    from math import ceil, floor, log10

    exponent = floor(log10(value))
    base = 10**exponent
    for step in (1, 2, 5, 10):
        if value <= step * base:
            return float(step * base)
    return float(ceil(value / base) * base)


# --- Chart ------------------------------------------------------------------

_CHART_W = 1000
_CHART_H = 300
_PAD_LEFT = 64
_PAD_RIGHT = 12
_PAD_TOP = 16
_PLOT_H = 232  # leaves room under the axis for x labels


def build_chart(buckets: list[dict], metric: str, bucket_seconds: int) -> dict:
    """Geometry for the inline SVG: bars, gridlines and axis labels.

    Returned as plain data rather than markup so the template stays readable
    and the arithmetic stays testable.
    """
    label, field, kind = _METRICS[metric]
    stacked = kind == "stacked"

    if stacked:
        tops = [b["cached_tokens"] + b["uncached_tokens"] for b in buckets]
    else:
        tops = [b.get(field) or 0 for b in buckets]
    peak = _nice_ceiling(max(tops) if tops else 0)

    plot_w = _CHART_W - _PAD_LEFT - _PAD_RIGHT
    slot = plot_w / max(1, len(buckets))
    bar_w = max(1.0, slot * 0.72)
    baseline = _PAD_TOP + _PLOT_H

    def height(value: float) -> float:
        return 0.0 if peak <= 0 else (value / peak) * _PLOT_H

    bars = []
    for i, bucket in enumerate(buckets):
        x = _PAD_LEFT + slot * i + (slot - bar_w) / 2
        if stacked:
            lower = height(bucket["uncached_tokens"])
            upper = height(bucket["cached_tokens"])
        else:
            lower, upper = height(bucket.get(field) or 0), 0.0
        bars.append(
            {
                "x": round(x, 2),
                "width": round(bar_w, 2),
                "lower_y": round(baseline - lower, 2),
                "lower_h": round(lower, 2),
                "upper_y": round(baseline - lower - upper, 2),
                "upper_h": round(upper, 2),
                "title": _bar_title(bucket, metric, bucket_seconds),
            }
        )

    gridlines = []
    for step in range(5):
        value = peak * step / 4
        gridlines.append(
            {
                "y": round(baseline - height(value), 2),
                "label": format_metric(value, kind),
            }
        )

    every = max(1, len(buckets) // 8)
    x_labels = [
        {
            "x": round(_PAD_LEFT + slot * i + slot / 2, 2),
            "label": _time_label(b["start"], bucket_seconds),
        }
        for i, b in enumerate(buckets)
        if i % every == 0
    ]

    return {
        "width": _CHART_W,
        "height": _CHART_H,
        "baseline": baseline,
        "bars": bars,
        "gridlines": gridlines,
        "x_labels": x_labels,
        "stacked": stacked,
        "metric_label": label,
        "empty": not any(t for t in tops),
    }


def _time_label(moment: datetime, bucket_seconds: int) -> str:
    if bucket_seconds >= 86400:
        return moment.strftime("%b %-d")
    if bucket_seconds >= 3600:
        return moment.strftime("%-Hh")
    return moment.strftime("%H:%M")


def _bar_title(bucket: dict, metric: str, bucket_seconds: int) -> str:
    """Native SVG tooltip text — no hover JS, and it survives in a saved page."""
    when = _time_label(bucket["start"], bucket_seconds)
    if metric == "cached_tokens":
        return (
            f"{when} — {si(bucket['cached_tokens'])} cached, "
            f"{si(bucket['uncached_tokens'])} uncached "
            f"({bucket['calls']} calls)"
        )
    _label, field, kind = _METRICS[metric]
    return f"{when} — {format_metric(bucket.get(field), kind)} ({bucket['calls']} calls)"


# --- Page -------------------------------------------------------------------


def _hit_rate_cell(row: dict) -> str:
    """A model with no judged calls is calibrating, not at zero percent."""
    if row["judged_calls"] == 0:
        return "calibrating"
    return pct(row["hit_rate"])


def build_context(args: Any, now: datetime) -> dict:
    """Everything the template needs, from the query string. Unknown values
    fall back to defaults so a stale or hand-edited URL still renders."""
    range_key = args.get("range", DEFAULT_RANGE)
    metric = args.get("metric", DEFAULT_METRIC)
    if metric not in _METRICS:
        metric = DEFAULT_METRIC
    dimension = args.get("by", DEFAULT_DIMENSION)
    if dimension not in _DIMENSION_LABELS:
        dimension = DEFAULT_DIMENSION

    start, end, range_label = resolve_range(range_key, now)
    bucket_seconds = pick_bucket_seconds((end - start).total_seconds())

    summary = db.activity_summary(start, end)
    buckets = db.activity_series(start, end, bucket_seconds)
    by_model = db.activity_rollup(start, end, dimension="model")
    grouped = (
        by_model
        if dimension == "model"
        else db.activity_rollup(start, end, dimension=dimension)
    )
    # The caller breakdown is a fixed panel, not just a grouping option: it is
    # the one that points at a file to go edit. Skipped when the selector is
    # already showing it, to avoid printing the same table twice.
    by_caller = (
        grouped
        if dimension == "caller"
        else db.activity_rollup(start, end, dimension="caller")
    )
    recent = db.recent_llm_calls(limit=50)

    _label, _field, kind = _METRICS[metric]
    return {
        "range_key": range_key,
        "range_label": range_label,
        "rolling": _ROLLING,
        "calendar": _CALENDAR,
        "metric": metric,
        "metrics": _METRICS,
        "metric_kind": kind,
        "dimension": dimension,
        "dimension_label": _DIMENSION_LABELS[dimension],
        "dimensions": _DIMENSION_LABELS,
        "summary": summary,
        "chart": build_chart(buckets, metric, bucket_seconds),
        "by_model": by_model,
        "by_caller": by_caller,
        "grouped": grouped,
        "recent": recent,
        "any_reported": any(
            c.cached_tokens_reported is not None for c in recent
        ),
        "calibrating": [
            r["key"] for r in by_model if r["judged_calls"] == 0 and r["calls"]
        ],
        "min_calibration_calls": MIN_CALIBRATION_CALLS,
        "si": si,
        "exact": exact,
        "cached_title": cached_title,
        "pct": pct,
        "ms": ms,
        "duration": duration,
        "format_metric": format_metric,
        "hit_rate_cell": _hit_rate_cell,
    }


ACTIVITY_TEMPLATE = """
<!doctype html>
<title>Activity &mdash; rainbox</title>
{% include "_nav.html" %}
<style>
  /* Explicit light canvas: the palette below is a light one, and a browser
     in dark mode would otherwise paint a black page behind near-black text.
     The nav pins its own background for the same reason. */
  body { margin: 0; font-family: system-ui, sans-serif; color: #1a1a2e;
         background: #fff; }
  .pp-act { max-width: 1180px; margin: 1rem auto; padding: 0 1rem 3rem; }
  .pp-act h1 { margin: 0.2rem 0 0.2rem; }
  .pp-act .sub { color: #6c757d; margin: 0 0 1.2rem; }
  .pp-act .bar { display: flex; gap: 0.6rem; align-items: center;
                 flex-wrap: wrap; margin-bottom: 1.4rem; }
  .pp-act select { font: inherit; padding: 0.4rem 0.6rem; border: 1px solid #cbd5e1;
                   border-radius: 8px; background: #fff; }
  .pp-act .by { color: #6c757d; }
  .pp-act .tiles { display: grid; gap: 0.9rem; margin-bottom: 1.6rem;
                   grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); }
  .pp-act .tile { border: 1px solid #e5e7eb; border-radius: 10px; padding: 0.8rem 0.9rem; }
  .pp-act .tile .k { color: #6c757d; font-size: 0.8rem; text-transform: uppercase;
                     letter-spacing: 0.04em; }
  .pp-act .tile .v { font-size: 1.7rem; font-weight: 600; margin-top: 0.15rem; }
  .pp-act .tile .n { color: #6c757d; font-size: 0.82rem; margin-top: 0.2rem; }
  .pp-act .panel { border: 1px solid #e5e7eb; border-radius: 10px; padding: 1rem;
                   margin-bottom: 1.6rem; }
  .pp-act .panel h2 { margin: 0 0 0.1rem; font-size: 1.05rem; }
  .pp-act .panel .note { color: #6c757d; font-size: 0.85rem; margin: 0 0 0.8rem; }
  .pp-act svg { width: 100%; height: auto; display: block; }
  .pp-act .legend { display: flex; gap: 1.1rem; align-items: center; margin-top: 0.6rem;
                    color: #6c757d; font-size: 0.85rem; }
  .pp-act .swatch { display: inline-block; width: 10px; height: 10px; border-radius: 50%;
                    margin-right: 0.35rem; vertical-align: middle; }
  .pp-act table { border-collapse: collapse; width: 100%; }
  .pp-act th { text-align: left; font-size: 0.78rem; color: #6c757d;
               text-transform: uppercase; letter-spacing: 0.04em;
               padding: 0.4rem 0.6rem; border-bottom: 2px solid #e5e7eb;
               white-space: nowrap; }
  .pp-act td { padding: 0.45rem 0.6rem; border-bottom: 1px solid #f1f5f9;
               white-space: nowrap; }
  .pp-act td.num, .pp-act th.num { text-align: right; font-variant-numeric: tabular-nums; }
  .pp-act td.name { font-weight: 600; }
  .pp-act .muted { color: #6c757d; }
  .pp-act td.origin { font-family: ui-monospace, monospace; font-size: 0.8rem; }
  .pp-act .warn { color: #b06f00; }
  .pp-act .bad { color: #c0392b; }
  .pp-act .empty { color: #6c757d; padding: 2rem 0; text-align: center; }
  .pp-act .gap { display: flex; gap: 2.2rem; flex-wrap: wrap; align-items: flex-start; }
  .pp-act .gap .reading { max-width: 34rem; color: #374151; font-size: 0.9rem;
                          line-height: 1.45; }
</style>
<main class="pp-act">
  <h1>Activity</h1>
  <p class="sub">Every LLM call rainbox makes, and how much of each prompt the
     cache spared.</p>

  <form class="bar" method="get" id="pp-act-controls">
    <select name="range" onchange="this.form.submit()">
      {% for k, label, _d in rolling %}
        <option value="{{ k }}" {{ 'selected' if range_key == k }}>{{ label }}</option>
      {% endfor %}
      {% for k, label in calendar %}
        <option value="{{ k }}" {{ 'selected' if range_key == k }}>{{ label }}</option>
      {% endfor %}
    </select>
    <select name="metric" onchange="this.form.submit()">
      {% for k, spec in metrics.items() %}
        <option value="{{ k }}" {{ 'selected' if metric == k }}>{{ spec[0] }}</option>
      {% endfor %}
    </select>
    <span class="by">by</span>
    <select name="by" onchange="this.form.submit()">
      {% for k, label in dimensions.items() %}
        <option value="{{ k }}" {{ 'selected' if dimension == k }}>{{ label }}</option>
      {% endfor %}
    </select>
  </form>

  {% if summary.calls == 0 %}
    <div class="panel">
      <div class="empty">
        No LLM calls recorded in {{ range_label|lower }}.<br>
        Recording starts the moment rainbox next talks to a model &mdash; try
        the chat, or a benchmark, then come back.
      </div>
    </div>
  {% else %}

  <div class="tiles">
    <div class="tile">
      <div class="k">Cache hit rate</div>
      <div class="v">{{ pct(summary.hit_rate) }}</div>
      <div class="n">{{ si(summary.cached_tokens) }} of
          {{ si(summary.prompt_tokens) }} prompt tokens</div>
    </div>
    <div class="tile">
      <div class="k">Reusable prefix</div>
      <div class="v">{{ pct(summary.reusable_rate) }}</div>
      <div class="n">what a warm cache could have served</div>
    </div>
    <div class="tile">
      <div class="k">Time saved</div>
      <div class="v">{{ duration(summary.seconds_saved) }}</div>
      <div class="n">prefill avoided, per model's own cold rate</div>
    </div>
    <div class="tile">
      <div class="k">Calls</div>
      <div class="v">{{ summary.calls }}</div>
      <div class="n">{% if summary.failures %}<span class="bad">{{ summary.failures }}
          failed</span>{% else %}all succeeded{% endif %}</div>
    </div>
  </div>

  <div class="panel">
    <h2>{{ chart.metric_label }}</h2>
    <p class="note">
      {{ range_label }}{% if chart.stacked %} &mdash; prompt tokens served from
      cache against those the model had to evaluate{% endif %}.
    </p>
    {% if chart.empty %}
      <div class="empty">Nothing to plot for this metric in this window.</div>
    {% else %}
    <svg viewBox="0 0 {{ chart.width }} {{ chart.height }}" role="img"
         aria-label="{{ chart.metric_label }} over {{ range_label|lower }}">
      {% for line in chart.gridlines %}
        <line x1="64" y1="{{ line.y }}" x2="{{ chart.width - 12 }}" y2="{{ line.y }}"
              stroke="#e5e7eb" stroke-dasharray="3 3" />
        <text x="56" y="{{ line.y + 4 }}" text-anchor="end" font-size="11"
              fill="#9aa3af">{{ line.label }}</text>
      {% endfor %}
      {% for bar in chart.bars %}
        {% if bar.lower_h > 0 or bar.upper_h > 0 %}
        <g><title>{{ bar.title }}</title>
          {% if bar.lower_h > 0 %}
          <rect x="{{ bar.x }}" y="{{ bar.lower_y }}" width="{{ bar.width }}"
                height="{{ bar.lower_h }}" fill="#9aa3af" />
          {% endif %}
          {% if bar.upper_h > 0 %}
          <rect x="{{ bar.x }}" y="{{ bar.upper_y }}" width="{{ bar.width }}"
                height="{{ bar.upper_h }}" fill="#e8a33d" />
          {% endif %}
        </g>
        {% endif %}
      {% endfor %}
      {% for tick in chart.x_labels %}
        <text x="{{ tick.x }}" y="{{ chart.baseline + 18 }}" text-anchor="middle"
              font-size="11" fill="#9aa3af">{{ tick.label }}</text>
      {% endfor %}
    </svg>
    {% if chart.stacked %}
    <div class="legend">
      <span><span class="swatch" style="background:#9aa3af"></span>Uncached</span>
      <span><span class="swatch" style="background:#e8a33d"></span>Cached</span>
      {% if not any_reported %}
      <span class="muted">&mdash; cached is estimated from prefill timing;
        local backends report no cache field</span>
      {% endif %}
    </div>
    {% endif %}
    {% endif %}
  </div>

  <div class="panel">
    <h2>Measured against reusable</h2>
    <p class="note">The gap between what the cache did and what it could have done.</p>
    <div class="gap">
      <div class="tiles" style="flex:1 1 320px; margin:0">
        <div class="tile">
          <div class="k">Measured</div>
          <div class="v">{{ pct(summary.hit_rate) }}</div>
          <div class="n">{{ si(summary.cached_tokens) }} tokens</div>
        </div>
        <div class="tile">
          <div class="k">Reusable</div>
          <div class="v">{{ pct(summary.reusable_rate) }}</div>
          <div class="n">{{ si(summary.reusable_tokens) }} tokens</div>
        </div>
      </div>
      <p class="reading">
        {% if summary.reusable_rate is none or summary.hit_rate is none %}
          Not enough judged calls yet to read the gap.
        {% elif summary.reusable_rate - summary.hit_rate > 0.15 %}
          <strong>The runtime is losing prefixes it could have kept.</strong>
          {{ pct(summary.reusable_rate) }} of prompt tokens repeated text the
          model had already seen, but only {{ pct(summary.hit_rate) }} came back
          from cache. Fewer models in rotation, or less interleaving between
          them, would close this.
        {% elif summary.reusable_rate < 0.2 %}
          <strong>The prompts themselves are breaking the prefix.</strong>
          Only {{ pct(summary.reusable_rate) }} of prompt tokens repeated
          anything sent before, so there is little for a cache to hold on to.
          Look for a timestamp or a reordered block near the top of the prompt.
        {% else %}
          The cache is serving close to everything it could
          ({{ pct(summary.hit_rate) }} measured against
          {{ pct(summary.reusable_rate) }} reusable).
        {% endif %}
      </p>
    </div>
  </div>

  <div class="panel">
    <h2>By {{ dimension_label|lower }}</h2>
    <p class="note">{{ metrics[metric][0] }} and cache behaviour per
       {{ dimension_label|lower }}.</p>
    <table>
      <tr>
        <th>{{ dimension_label }}</th>
        <th class="num">Calls</th>
        <th class="num">{{ metrics[metric][0] }}</th>
        <th class="num">Hit rate</th>
        <th class="num">Reusable</th>
        <th class="num">Prompt tokens</th>
        <th class="num">Avg prefill</th>
        <th class="num">P50 latency</th>
        <th class="num">Saved</th>
      </tr>
      {% for row in grouped %}
      <tr>
        <td class="name">{{ row.key }}</td>
        <td class="num">{{ row.calls }}</td>
        <td class="num">{{ format_metric(row[metric], metric_kind)
                           if metric in row else '—' }}</td>
        <td class="num {{ 'warn' if row.judged_calls == 0 }}">{{ hit_rate_cell(row) }}</td>
        <td class="num">{{ pct(row.reusable_rate) }}</td>
        <td class="num" title="{{ exact(row.prompt_tokens, 'tokens') }}">{{ si(row.prompt_tokens) }}</td>
        <td class="num">{{ (row.avg_prefill_tps|round|int ~ ' tok/s')
                           if row.avg_prefill_tps else '—' }}</td>
        <td class="num" title="{{ exact(row.p50_latency_ms, 'ms') }}">{{ ms(row.p50_latency_ms) }}</td>
        <td class="num">{{ duration(row.seconds_saved) }}</td>
      </tr>
      {% endfor %}
    </table>
    {% if calibrating %}
    <p class="note" style="margin-top:0.8rem">
      Still calibrating: {{ calibrating|join(', ') }}. A model needs
      {{ min_calibration_calls }} recorded calls before its cold prefill rate
      means anything, and until then its cache use is left unjudged rather than
      guessed.
    </p>
    {% endif %}
  </div>

  {% if dimension != 'caller' %}
  <div class="panel">
    <h2>By caller</h2>
    <p class="note">Which part of rainbox is producing cache-hostile prompts.
       Always shown, whatever the grouping above is set to &mdash; this is the
       table that says where to go and fix something.</p>
    <table>
      <tr>
        <th>Caller</th>
        <th class="num">Calls</th>
        <th class="num">Hit rate</th>
        <th class="num">Reusable</th>
        <th class="num">Prompt tokens</th>
        <th class="num">P50 latency</th>
        <th class="num">Saved</th>
      </tr>
      {% for row in by_caller %}
      <tr>
        <td class="name">{{ row.key }}</td>
        <td class="num">{{ row.calls }}</td>
        <td class="num {{ 'warn' if row.judged_calls == 0 }}">{{ hit_rate_cell(row) }}</td>
        <td class="num">{{ pct(row.reusable_rate) }}</td>
        <td class="num" title="{{ exact(row.prompt_tokens, 'tokens') }}">{{ si(row.prompt_tokens) }}</td>
        <td class="num" title="{{ exact(row.p50_latency_ms, 'ms') }}">{{ ms(row.p50_latency_ms) }}</td>
        <td class="num">{{ duration(row.seconds_saved) }}</td>
      </tr>
      {% endfor %}
    </table>
  </div>
  {% endif %}

  <div class="panel">
    <h2>Recent calls</h2>
    <p class="note">The last {{ recent|length }} calls, newest first.</p>
    <table>
      <tr>
        <th>Started</th>
        <th>Model</th>
        <th>Caller</th>
        <th>Origin</th>
        <th class="num" title="Prompt tokens sent on this call.">Prompt</th>
        <th class="num" title="Prompt tokens the runtime evidently reused. Reported by the provider where it says so, otherwise inferred from prefill timing.">Cached</th>
        <th class="num" title="Prompt tokens rainbox had already sent before this call: what a perfect cache could have reused. Exact, and needs no provider cooperation.">Reusable</th>
        <th class="num" title="Tokens the model generated in reply — the &quot;Completion tokens&quot; metric, per call.">Output</th>
        <th class="num" title="Time spent processing the prompt before the first output token.">Prefill</th>
        <th class="num" title="Wall-clock time for the whole call.">Total</th>
        <th></th>
      </tr>
      {% for call in recent %}
      <tr>
        <td class="muted">{{ call.started_at.strftime('%b %-d %H:%M:%S')
                             if call.started_at else '—' }}</td>
        <td class="name">{{ call.model or '—' }}</td>
        <td class="muted">{{ call.caller }}</td>
        <td class="muted origin">{{ call.origin or '—' }}</td>
        <td class="num" title="{{ exact(call.prompt_tokens, 'tokens') }}">{{ si(call.prompt_tokens) }}</td>
        <td class="num" title="{{ cached_title(call) }}">{{ si(call.cached_tokens_reported
                              if call.cached_tokens_reported is not none
                              else call.cached_tokens_estimated) }}</td>
        <td class="num" title="{{ exact(call.reusable_prefix_tokens, 'tokens') }}">{{ si(call.reusable_prefix_tokens) }}</td>
        <td class="num" title="{{ exact(call.completion_tokens, 'tokens') }}">{{ si(call.completion_tokens) }}</td>
        <td class="num" title="{{ exact(call.prefill_ms, 'ms') }}">{{ ms(call.prefill_ms) }}</td>
        <td class="num" title="{{ exact(call.total_ms, 'ms') }}">{{ ms(call.total_ms) }}</td>
        <td>{% if not call.ok %}<span class="bad">{{ call.error_category
            or 'failed' }}</span>{% endif %}</td>
      </tr>
      {% endfor %}
    </table>
  </div>

  {% endif %}
</main>
"""


@app.route("/activity")
def activity_page() -> str:
    return render_template_string(
        ACTIVITY_TEMPLATE, **build_context(request.args, datetime.now(UTC))
    )
