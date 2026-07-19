"""Curated catalog of American-English Kokoro voices.

v1 runs a single pipeline with lang_code='a' (American English), so only
American-English voices (ids prefixed af_/am_) are exposed to keep
pronunciation correct.
"""

VOICES: list[dict[str, str]] = [
    {"id": "af_heart", "name": "Heart (F)", "lang": "American English"},
    {"id": "af_bella", "name": "Bella (F)", "lang": "American English"},
    {"id": "af_nicole", "name": "Nicole (F)", "lang": "American English"},
    {"id": "af_sarah", "name": "Sarah (F)", "lang": "American English"},
    {"id": "am_michael", "name": "Michael (M)", "lang": "American English"},
    {"id": "am_adam", "name": "Adam (M)", "lang": "American English"},
    {"id": "am_echo", "name": "Echo (M)", "lang": "American English"},
]


def voice_ids() -> list[str]:
    return [v["id"] for v in VOICES]


def is_valid_voice(voice_id: str) -> bool:
    return voice_id in voice_ids()
