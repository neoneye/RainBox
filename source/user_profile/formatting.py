"""Deterministic formatting guide: compile the active person profile's locale
fields into code-owned prompt directives with examples.

Injected by the main assistant as `<formatting_guide>` next to
`<user_settings_json>`. The guide reads as the defaults the reply follows, so
every imperative sentence here is owned by code and every interpolated value
passes the strict prompt-boundary validation below — the profile form
deliberately accepts uncommon free-text timezone/language/currency values, and
a value such as "ignore previous instructions" must never reach the model
inside a code-owned directive merely because it was stored in a locale field.
Unusable values are omitted and logged, never spliced into a directive.

Everything is lookup-driven from two fixed samples (1234567.89 for the numbers
line, 1234.56 for the currency line): enum-derived wording and examples are
exhaustive-table output, never free-typed templates, so the prompt examples
stay deterministic for tests. The browser preview may use the current year;
this module's examples are pinned (31 December 2026, 23:59).
"""

import logging
import re
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from language_tags import canonical_language_tag, effective_language_rows

logger = logging.getLogger(__name__)

# Construction is bounded; exceeding the cap raises (fail loudly in
# development) rather than truncating a rule mid-directive.
MAX_FORMATTING_GUIDE_CHARS = 1_200

# Prompt-example minor-unit exceptions, not an ISO 4217 validator: zero-decimal
# currencies render the integer sample (1,234 JPY — "1,234.00 JPY" is wrong),
# three-decimal ones render thousandths (their dinar/rial minor units).
# Everything unknown defaults to two decimals: money is where a misread
# separator costs the most, so the money example must demonstrate it.
ZERO_DECIMAL_CURRENCIES_V1 = frozenset({"JPY", "KRW", "VND", "CLP", "ISK"})
THREE_DECIMAL_CURRENCIES_V1 = frozenset({"BHD", "KWD", "OMR", "JOD", "TND", "LYD"})

# ---- exhaustive enum lookups (one entry per registry enum value; the
# exhaustiveness test in test_formatting.py keeps these in lockstep with
# profile_fields.PROFILE_FIELDS) ------------------------------------------

# stored value -> (wording, {minor-unit digits: currency example})
# The stored value doubles as the numbers-line example (it IS the rendering of
# the shared sample 1234567.89 under that convention).
NUMBER_FORMATS: dict[str, tuple[str, dict[int, str]]] = {
    "1,234,567.89": ("decimal point with comma grouping",
                     {2: "1,234.56", 0: "1,234", 3: "1,234.567"}),
    "1.234.567,89": ("decimal comma with point grouping",
                     {2: "1.234,56", 0: "1.234", 3: "1.234,567"}),
    "1 234 567,89": ("decimal comma with space grouping",
                     {2: "1 234,56", 0: "1 234", 3: "1 234,567"}),
    "1'234'567.89": ("decimal point with apostrophe grouping",
                     {2: "1'234.56", 0: "1'234", 3: "1'234.567"}),
    "12,34,567.89": ("decimal point with Indian comma grouping",
                     {2: "1,234.56", 0: "1,234", 3: "1,234.567"}),
    "1234567.89": ("decimal point without thousands separators",
                   {2: "1234.56", 0: "1234", 3: "1234.567"}),
    "1234567,89": ("decimal comma without thousands separators",
                   {2: "1234,56", 0: "1234", 3: "1234,567"}),
}

# stored value -> the code-owned comment the IDENTITY block attaches next to
# the raw enum value ("number_format.comment"). The bare stored value is
# opaque to a small model reading context JSON; this spells the convention
# out even while the gated formatting guide is off. Derived from the
# validated enum only — never operator text — so it is safe inside the
# context-authority block.
NUMBER_FORMAT_COMMENTS: dict[str, str] = {
    "1,234,567.89": "Use COMMA as thousands separator and DOT as decimal "
                    "separator.",
    "1.234.567,89": "Use DOT as thousands separator and COMMA as decimal "
                    "separator.",
    "1 234 567,89": "Use SPACE as thousands separator and COMMA as decimal "
                    "separator.",
    "1'234'567.89": "Use APOSTROPHE as thousands separator and DOT as "
                    "decimal separator.",
    "12,34,567.89": "Use Indian digit grouping with COMMA separators and "
                    "DOT as decimal separator.",
    "1234567.89": "Don't show thousand separators. Use DOT as decimal "
                  "separator.",
    "1234567,89": "Don't show thousand separators. Use COMMA as decimal "
                  "separator.",
}

# stored value -> (example: 31 December 2026 in the selected order, the
# ambiguity warning for the opposite convention)
DATE_FORMATS: dict[str, tuple[str, str]] = {
    "YYYY-MM-DD": ("2026-12-31", "do not use month-first dates"),
    "DD/MM/YYYY": ("31/12/2026", "do not use month-first dates"),
    "MM/DD/YYYY": ("12/31/2026", "do not use day-first dates"),
    "DD.MM.YYYY": ("31.12.2026", "do not use month-first dates"),
    "DD-MM-YYYY": ("31-12-2026", "do not use month-first dates"),
}

# stored value -> clock wording with the pinned example (23:59 / 11:59 pm)
TIME_FORMATS: dict[str, str] = {
    "24h": "24-hour clock, for example 23:59",
    "12h": "12-hour clock, for example 11:59 pm",
}

# stored value -> the calendar directive. Monday-start pairs with ISO 8601
# week numbering; naming that removes the models' habitual Sunday-first
# calendar layout (and week-number arithmetic) for European profiles.
WEEK_STARTS: dict[str, str] = {
    "monday": "weeks start on Monday (ISO 8601; week numbers follow ISO)",
    "sunday": "weeks start on Sunday",
    "saturday": "weeks start on Saturday",
}

# stored value -> unit-system wording with the preferred unit names.
# Temperature is deliberately NOT here — it renders as its own line (the
# `temperature` field, derived from units when unset), because the
# combinations are real: UK metric-leaning + Celsius, US customary + °F.
UNITS: dict[str, str] = {
    "metric": "metric. Prefer km and kg",
    "imperial": "US customary. Prefer mi and lb",
    "uk": "metric with UK exceptions. Prefer kg, but miles for road "
          "distances",
}

# stored value -> the temperature directive; `_derived_temperature` supplies
# the units-implied default when the field is unset.
TEMPERATURES: dict[str, str] = {
    "celsius": "Celsius (°C)",
    "fahrenheit": "Fahrenheit (°F)",
}

_UNITS_DEFAULT_TEMPERATURE: dict[str, str] = {
    "metric": "celsius", "uk": "celsius", "imperial": "fahrenheit",
}

def _variant_clause(tag: str | None) -> str:
    """The variant directive for one declared tag, or "" when it has none.

    A tag carrying a region or script subtag ("en-GB", "pt-BR", "zh-Hans")
    names a specific variant of its language; a bare primary tag ("en",
    "da") does not, and there is no default variant to state. The clause is
    rendered from the tag itself rather than from a table of languages: a
    per-language table would need an entry before any language could be
    handled, which makes English structurally privileged and every other
    language an addition. It also says spelling AND vocabulary, because a
    directive naming only spelling gets applied to orthography alone — a
    live run wrote one variant's spelling beside the other's word choice.

    The variant is NAMED by its tag and never exemplified: contrastive
    example words in a prompt get parroted into unrelated replies.
    """
    if not tag or "-" not in tag:
        return ""
    return (f" When writing {tag.split('-')[0]}, use the {tag} variant — "
            f"spelling and vocabulary alike; never mix in another variant "
            f"of the same language.")

_GUIDE_HEADER = ("Use these defaults unless the current request or exact "
                 "source notation says otherwise:")


# ---- prompt-boundary validation (stricter than the form's soft checks) ----

def _valid_timezone(raw: Any) -> str | None:
    """The IANA zone name when zoneinfo accepts it, else None."""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        ZoneInfo(text)
    except Exception:
        return None
    return text


def _valid_currency(raw: Any) -> str | None:
    """Exactly three ASCII letters, canonicalized to uppercase. Validates
    shape, not economic existence."""
    text = str(raw or "").strip()
    if not re.fullmatch(r"[A-Za-z]{3}", text):
        return None
    return text.upper()


def _valid_language(raw: Any) -> str | None:
    """Compatibility wrapper around the shared prompt/storage boundary."""
    return canonical_language_tag(raw)


def valid_language_tag(raw: Any) -> str | None:
    """Public language-tag boundary used by model-output resolution."""
    return canonical_language_tag(raw)


def _utc_offset(zone: str, now: datetime) -> str | None:
    """The zone's current UTC offset as "UTC+02:00", or None when it cannot
    be computed (the line then renders the zone name alone rather than
    guessing). Stating the offset removes daylight-saving arithmetic from the
    model entirely — small models cannot be trusted to know whether Berlin is
    UTC+1 or UTC+2 on a given date."""
    try:
        offset = now.astimezone(ZoneInfo(zone)).utcoffset()
        if offset is None:
            return None
        total = int(offset.total_seconds()) // 60
        sign = "+" if total >= 0 else "-"
        hours, minutes = divmod(abs(total), 60)
        return f"UTC{sign}{hours:02d}:{minutes:02d}"
    except Exception:
        return None


def _first_valid(values: list[Any], validator: Any) -> tuple[str | None, str | None]:
    """(preferred, secondary): the first valid value becomes preferred (a
    missing/invalid primary never makes the whole line disappear); a later
    distinct valid value becomes the secondary."""
    valid = []
    for raw in values:
        v = validator(raw)
        if v is not None and v not in valid:
            valid.append(v)
        elif v is None and str(raw or "").strip():
            logger.warning("formatting guide: unusable profile value %r omitted", raw)
    preferred = valid[0] if valid else None
    secondary = valid[1] if len(valid) > 1 else None
    return preferred, secondary


def valid_profile_languages(profile: dict[str, Any]) -> tuple[str | None, str | None]:
    """The first two declared languages through the shared prompt boundary.

    A ``prefer`` row sorts first; declaration order settles the remainder.
    """
    data = (profile or {}).get("data") or {}
    rows = effective_language_rows(data)
    ordered = sorted(
        enumerate(rows),
        key=lambda item: (0 if item[1].get("stance") == "prefer" else 1,
                          item[0]))
    return _first_valid(
        [row.get("tag") for _, row in ordered], _valid_language)


# ---- the renderer --------------------------------------------------------

def format_formatting_guide(profile: dict[str, Any],
                            now: datetime | None = None, *,
                            mirror_conversation: bool = True) -> str:
    """Render one profile's locale fields as the formatting-guide body
    (deterministic; no DB access). Returns "" when no directive is usable.
    `now` is the injectable clock for the timezone offset; tests pin it on
    both sides of a DST boundary. `mirror_conversation` says whether the
    Language line may state "reply in the language of the current message;
    never switch on your own" — see the language block below for why only
    that one clause is conditional, and why the default renders it. The
    caller computes this: it is not a setting, so nothing here reads one."""
    data = profile.get("data") or {}
    if now is None:
        now = datetime.now(UTC)
    lines: list[str] = []

    date_entry = DATE_FORMATS.get(str(data.get("date_format") or "").strip())
    if date_entry is not None:
        example, warning = date_entry
        lines.append(f"- Dates: {data['date_format'].strip()}, for example "
                     f"{example}; {warning}.")

    week = WEEK_STARTS.get(str(data.get("first_day_of_week") or "").strip())
    if week is not None:
        lines.append(f"- Calendar: {week}.")

    clock = TIME_FORMATS.get(str(data.get("time_format") or "").strip())
    zone = _valid_timezone(data.get("timezone"))
    if data.get("timezone") and zone is None:
        logger.warning("formatting guide: unusable timezone %r omitted",
                       data.get("timezone"))
    if clock is not None or zone is not None:
        clauses = []
        if clock is not None:
            clauses.append(f"{clock}.")
        if zone is not None:
            offset = _utc_offset(zone, now)
            where = f"{zone} (currently {offset})" if offset else zone
            prefix = "Present" if clock is not None else "present"
            clauses.append(f"{prefix} local times in {where}; name another "
                           "zone when relevant.")
        lines.append("- Times: " + " ".join(clauses))

    units_value = str(data.get("units") or "").strip()
    units = UNITS.get(units_value)
    if units is not None:
        lines.append(f"- Units: {units}; preserve a source value when "
                     "precision matters and add the conversion.")

    temperature_value = (str(data.get("temperature") or "").strip()
                         or _UNITS_DEFAULT_TEMPERATURE.get(units_value, ""))
    temperature = TEMPERATURES.get(temperature_value)
    if temperature is not None:
        lines.append(f"- Temperature: {temperature}.")

    number_entry = NUMBER_FORMATS.get(str(data.get("number_format") or "").strip())
    if number_entry is not None:
        wording, _ = number_entry
        # No sentence-ending period: the example IS separator punctuation,
        # and a trailing dot right after the digits could read as part of
        # the convention being demonstrated.
        lines.append(f"- Numbers: {wording}, for example: "
                     f"{data['number_format'].strip()}")

    currency, currency_2 = _first_valid(
        [data.get("currency"), data.get("currency_2")], _valid_currency)
    if currency is not None:
        if number_entry is not None:
            _, currency_examples = number_entry
            digits = (0 if currency in ZERO_DECIMAL_CURRENCIES_V1
                      else 3 if currency in THREE_DECIMAL_CURRENCIES_V1 else 2)
            example = currency_examples[digits]
            head = (f"use the currency code {currency} with the preferred number "
                    f"format, for example {example} {currency}.")
        else:
            # Without a usable number_format the line states the code and the
            # conversion rule without inventing separators.
            head = f"use the currency code {currency}."
        secondary = (f" {currency_2} is a secondary option when the task "
                     "already involves it." if currency_2 else "")
        lines.append(f"- Currency: {head}{secondary} Convert currencies only "
                     "with a supplied or freshly retrieved rate.")

    language, secondary_language = valid_profile_languages(profile)
    if language is not None:
        # The preferred language is NOT the output language: replies mirror
        # the conversation. Small models read a bare "prefer da" as a
        # directive to switch, so the rule is spelled out as absolute and
        # the profile languages are demoted to explicit-request-only.
        #
        # Only the mirroring sentence is conditional on `mirror_conversation`.
        # A room's first message has no conversation to mirror, so "reply in
        # the language of the current message" points at nothing the turn
        # can read yet -- the caller passes False there. The explicit-request
        # clause and the variant clause need no conversation to be true: an
        # explicit request and a declared variant are both well-defined with
        # no history at all, so they render regardless.
        known = (
            f"{language} or {secondary_language}"
            if secondary_language else language)
        # The first declared tag that names a variant states it; a profile
        # whose tags are all bare adds nothing.
        variant = next(
            (c for c in (_variant_clause(language),
                         _variant_clause(secondary_language)) if c), "")
        mirror = ("reply in the language of the current message; never "
                  "switch on your own. " if mirror_conversation else "")
        lines.append(f"- Language: {mirror}Use "
                     f"{known} only when the message asks for it; an "
                     f"explicit request always wins.{variant}")

    if not lines:
        return ""
    body = "\n".join([_GUIDE_HEADER, *lines])
    if len(body) > MAX_FORMATTING_GUIDE_CHARS:
        raise ValueError(
            f"formatting guide exceeds {MAX_FORMATTING_GUIDE_CHARS} chars "
            f"({len(body)}) — a lookup entry grew past the budget")
    return body


def build_formatting_guide() -> str:
    """Convenience wrapper for tests and ad-hoc callers: renders the active
    profile, "" when none is selected. NEVER wire this into the main handle
    path — that path performs exactly one profile-context lookup per turn and
    passes context.profile to format_formatting_guide directly."""
    from user_profile.identity import current_profile

    profile = current_profile()
    if profile is None:
        return ""
    return format_formatting_guide(profile)
