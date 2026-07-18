"""Voice library for the dots.tts service.

Each voice is a folder under the base directory:

    <base>/<slug>/
      reference.wav    the reference audio sample (~8-12 s of clean speech)
      transcript.txt   exact transcript of the reference audio (UTF-8)
      name.txt         display name

The folder name is the voice id: a slug derived from the display name.
Folders missing any of the three files are ignored by list/get, so a
half-written voice never surfaces through the API.
"""

import re
import shutil
from pathlib import Path

REFERENCE_FILENAME = "reference.wav"
TRANSCRIPT_FILENAME = "transcript.txt"
NAME_FILENAME = "name.txt"

_SLUG_RUN = re.compile(r"[a-z0-9]+")
_VALID_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def slugify(name: str) -> str:
    """Lowercase alnum runs joined by dashes; 'voice' if nothing survives."""
    runs = _SLUG_RUN.findall(name.lower())
    return "-".join(runs) or "voice"


def _voice_from_dir(voice_dir: Path) -> dict | None:
    reference = voice_dir / REFERENCE_FILENAME
    transcript = voice_dir / TRANSCRIPT_FILENAME
    name = voice_dir / NAME_FILENAME
    if not (reference.is_file() and transcript.is_file() and name.is_file()):
        return None
    return {
        "id": voice_dir.name,
        "name": name.read_text(encoding="utf-8").strip(),
        "transcript": transcript.read_text(encoding="utf-8").strip(),
        "reference_path": str(reference),
    }


def list_voices(base: Path) -> list[dict]:
    """All complete voices under `base`, sorted by display name."""
    if not base.is_dir():
        return []
    found = []
    for child in base.iterdir():
        if child.is_dir():
            voice = _voice_from_dir(child)
            if voice is not None:
                found.append(voice)
    return sorted(found, key=lambda v: v["name"].lower())


def get_voice(base: Path, voice_id: str) -> dict | None:
    """The voice with the given id, or None. Rejects non-slug ids so a
    crafted id can never escape the base directory."""
    if not _VALID_ID.match(voice_id):
        return None
    return _voice_from_dir(base / voice_id)


def create_voice(base: Path, name: str, wav_bytes: bytes, transcript: str) -> dict:
    """Store a new voice and return it. Raises ValueError on empty inputs.
    Slug collisions get a -2/-3/... suffix."""
    name = name.strip()
    transcript = transcript.strip()
    if not name:
        raise ValueError("name must not be empty")
    if not transcript:
        raise ValueError("transcript must not be empty")
    if not wav_bytes:
        raise ValueError("audio must not be empty")

    base.mkdir(parents=True, exist_ok=True)
    slug = slugify(name)
    candidate = slug
    counter = 2
    while (base / candidate).exists():
        candidate = f"{slug}-{counter}"
        counter += 1

    voice_dir = base / candidate
    voice_dir.mkdir()
    (voice_dir / REFERENCE_FILENAME).write_bytes(wav_bytes)
    (voice_dir / TRANSCRIPT_FILENAME).write_text(transcript + "\n", encoding="utf-8")
    (voice_dir / NAME_FILENAME).write_text(name + "\n", encoding="utf-8")

    voice = _voice_from_dir(voice_dir)
    assert voice is not None
    return voice


def delete_voice(base: Path, voice_id: str) -> bool:
    """Remove a voice folder. Returns False if the id is invalid or absent."""
    if get_voice(base, voice_id) is None:
        return False
    shutil.rmtree(base / voice_id)
    return True
