"""Person-profile field registry — the single source of truth for the /profile
page's data schema. One row per field; drives server-side validation
(db/profile.py), the form pane's fieldsets (webapp/profile_views.py), and
(later) prompt rendering. All person fields live in the profile row's sparse
`data` JSONB: every field is optional, absent means unset (never ""), and
connector-written observations live under data["dynamic"], which is not a
registry field and is never writable through the human-facing PUT.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Field:
    key: str
    group: str
    kind: str  # "text" | "enum" | "date" | "email" — the complete set for v1
    label: str
    hint: str = ""
    choices: tuple[str, ...] = ()
    multiline: bool = False
    datalist: str = ""  # datalist id suffix in the form ("tz", "lang", …)


PROFILE_FIELDS = [
    # group "Identity"
    Field("full_name",      "Identity", kind="text",  label="Full name",
          hint="However they write it — any script, order, or particles; "
               "one field, never split into first/last."),
    Field("native_name",    "Identity", kind="text",  label="Native name",
          hint="The name in its native script when that differs from the "
               "Latin form — e.g. 湯川秀樹, יובל נאמן, యల్లాప్రగడ సుబ్బారావు."),
    Field("preferred_name", "Identity", kind="text",  label="Address them as",
          hint="What the assistant calls them, e.g. “Simon” or “you”."),
    Field("handle",         "Identity", kind="text",  label="Internet nickname",
          hint="Online handle / username, e.g. “neoneye”."),
    Field("gender",         "Identity", kind="enum",  label="Gender",
          choices=("male", "female", "other")),
    Field("about",          "Identity", kind="text",  label="About",
          multiline=True,
          hint="Self-description in their own words, e.g. “programmer, "
               "modern day alchemist doing code”."),
    Field("birthday",       "Identity", kind="date",  label="Birthday"),
    # group "Locale & formats"
    # "uk" is the hybrid macOS calls "Measurement System: UK": kilograms and
    # Celsius, but road distances in miles. "imperial" is US customary.
    Field("units",          "Locale & formats", kind="enum", label="Units",
          choices=("metric", "imperial", "uk")),
    # Separate from `units` because the combinations are real (the UK is
    # metric-leaning with Celsius; US customary pairs with Fahrenheit).
    # Unset derives from units: metric/uk → celsius, imperial → fahrenheit.
    Field("temperature",    "Locale & formats", kind="enum", label="Temperature",
          choices=("celsius", "fahrenheit")),
    Field("timezone",       "Locale & formats", kind="text", label="Timezone",
          datalist="tz", hint="IANA name, e.g. Europe/Copenhagen"),
    Field("date_format",    "Locale & formats", kind="enum", label="Date format",
          choices=("YYYY-MM-DD", "DD/MM/YYYY", "MM/DD/YYYY",
                   "DD.MM.YYYY", "DD-MM-YYYY")),
    Field("time_format",    "Locale & formats", kind="enum", label="Time format",
          choices=("24h", "12h")),
    # The values double as previews: every choice renders the SAME sample,
    # 1234567.89 — seven integer digits are the minimum that disambiguates
    # Indian grouping from Western grouping. A deliberately finite preference
    # enum, not full CLDR coverage; unsupported conventions stay unset. The
    # space-grouping value stores a normal ASCII space (rendering may swap in
    # a non-breaking space, storage does not).
    Field("number_format",  "Locale & formats", kind="enum", label="Number format",
          choices=("1,234,567.89", "1.234.567,89", "1 234 567,89",
                   "1'234'567.89", "12,34,567.89")),
    # Monday covers most of Europe (and ISO 8601), Sunday the US/Canada and
    # much of Asia, Saturday parts of the Middle East — together virtually
    # every calendar convention in use.
    Field("first_day_of_week", "Locale & formats", kind="enum",
          label="First day of week",
          choices=("monday", "sunday", "saturday")),
    Field("language",       "Locale & formats", kind="text", label="Language (primary)",
          datalist="lang", hint="BCP-47, e.g. da, en-US, zh-Hans"),
    Field("language_2",     "Locale & formats", kind="text", label="Language (secondary)",
          datalist="lang"),
    Field("currency",       "Locale & formats", kind="text", label="Currency (primary)",
          datalist="currency", hint="ISO 4217, e.g. DKK, USD"),
    Field("currency_2",     "Locale & formats", kind="text", label="Currency (secondary)",
          datalist="currency"),
    # group "Contact & location"
    Field("country",        "Contact & location", kind="text", label="Country",
          datalist="country"),
    Field("city",           "Contact & location", kind="text", label="City"),
    Field("address",        "Contact & location", kind="text", label="Address",
          multiline=True),
    Field("email",          "Contact & location", kind="email", label="Email"),
]

FIELDS_BY_KEY = {f.key: f for f in PROFILE_FIELDS}

# Group names in first-appearance order — the form renders one <fieldset> each.
FIELD_GROUPS = list(dict.fromkeys(f.group for f in PROFILE_FIELDS))

# Keys projected onto tree rows as the read-only `summary`, sized for the
# folder detail table (Name / Person / Language / Units / Time / Country).
SUMMARY_KEYS = ("full_name", "language", "units", "time_format", "country")
