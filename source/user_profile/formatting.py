"""Deterministic formatting guide: compile the active person profile's locale
fields into code-owned prompt directives with examples.

Injected by the main assistant as `<formatting_guide authority="instructions">`
next to `<operator_identity>`. That authority is justified only because every
imperative sentence here is owned by code and every interpolated value passes
the strict prompt-boundary validation below — the profile form deliberately
accepts uncommon free-text timezone/language/currency values, and a value such
as "ignore previous instructions" must never be elevated into an
instruction-authority block merely because it was stored in a locale field.
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

logger = logging.getLogger(__name__)

# Construction is bounded; exceeding the cap raises (fail loudly in
# development) rather than truncating a rule mid-directive.
MAX_FORMATTING_GUIDE_CHARS = 1_100

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

# stored value -> unit-system wording with the preferred unit names
UNITS: dict[str, str] = {
    "metric": "metric. Prefer km, kg, and °C",
    "imperial": "imperial. Prefer mi, lb, and °F",
}

# canonical language tag -> spelling clause (bare "en" adds none; only the
# two tags the profile can meaningfully disambiguate).
ENGLISH_SPELLING: dict[str, str] = {
    "en-GB": "Use British English spelling when writing English.",
    "en-US": "Use American English spelling when writing English.",
}

# A deliberately safe BCP-47 subset, not a complete validator: a valid tag
# outside it stays stored but is omitted from prompt instructions.
_LANGUAGE_RE = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,3}")
_MAX_LANGUAGE_LEN = 35

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
    """The canonicalized tag when it matches the safe BCP-47 subset, else
    None. Primary subtag lowercase, two-letter alphabetic region uppercase,
    four-letter alphabetic script title case, other subtags lowercase."""
    text = str(raw or "").strip()
    if not text or len(text) > _MAX_LANGUAGE_LEN:
        return None
    if not _LANGUAGE_RE.fullmatch(text):
        return None
    parts = text.split("-")
    out = [parts[0].lower()]
    for sub in parts[1:]:
        if len(sub) == 2 and sub.isalpha():
            out.append(sub.upper())
        elif len(sub) == 4 and sub.isalpha():
            out.append(sub.title())
        else:
            out.append(sub.lower())
    return "-".join(out)


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


# ---- the renderer --------------------------------------------------------

def format_formatting_guide(profile: dict[str, Any],
                            now: datetime | None = None) -> str:
    """Render one profile's locale fields as the formatting-guide body
    (deterministic; no DB access). Returns "" when no directive is usable.
    `now` is the injectable clock for the timezone offset; tests pin it on
    both sides of a DST boundary."""
    data = profile.get("data") or {}
    if now is None:
        now = datetime.now(UTC)
    lines: list[str] = []

    date_entry = DATE_FORMATS.get(str(data.get("date_format") or "").strip())
    if date_entry is not None:
        example, warning = date_entry
        lines.append(f"- Dates: {data['date_format'].strip()}, for example "
                     f"{example}; {warning}.")

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

    units = UNITS.get(str(data.get("units") or "").strip())
    if units is not None:
        lines.append(f"- Units: {units}; preserve a source value when "
                     "precision matters and add the conversion.")

    number_entry = NUMBER_FORMATS.get(str(data.get("number_format") or "").strip())
    if number_entry is not None:
        wording, _ = number_entry
        lines.append(f"- Numbers: {wording}, for example "
                     f"{data['number_format'].strip()}.")

    currency, currency_2 = _first_valid(
        [data.get("currency"), data.get("currency_2")], _valid_currency)
    if currency is not None:
        if number_entry is not None:
            _, currency_examples = number_entry
            digits = (0 if currency in ZERO_DECIMAL_CURRENCIES_V1
                      else 3 if currency in THREE_DECIMAL_CURRENCIES_V1 else 2)
            example = currency_examples[digits]
            head = (f"use the ISO code {currency} with the preferred number "
                    f"format, for example {example} {currency}.")
        else:
            # Without a usable number_format the line states the code and the
            # conversion rule without inventing separators.
            head = f"use the ISO code {currency}."
        secondary = (f" {currency_2} is a secondary option when the task "
                     "already involves it." if currency_2 else "")
        lines.append(f"- Currency: {head}{secondary} Convert currencies only "
                     "with a supplied or freshly retrieved rate.")

    language, language_2 = _first_valid(
        [data.get("language"), data.get("language_2")], _valid_language)
    if language is not None:
        fallback = (f"prefer {language}, with {language_2} as fallback"
                    if language_2 else f"prefer {language}")
        spelling = ""
        for tag in (language, language_2):
            if tag in ENGLISH_SPELLING:
                spelling = " " + ENGLISH_SPELLING[tag]
                break
        lines.append("- Language: follow the language of the current message; "
                     f"otherwise {fallback}.{spelling}")

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
