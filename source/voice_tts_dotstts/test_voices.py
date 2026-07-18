from pathlib import Path

import pytest

from voices import create_voice, delete_voice, get_voice, list_voices, slugify

WAV = b"RIFF....WAVEfake"


def test_slugify_basic():
    assert slugify("Simon's Mac Voice") == "simon-s-mac-voice"
    assert slugify("  Ärlig røst  ") == "rlig-r-st"
    assert slugify("!!!") == "voice"


def test_create_and_list(tmp_path: Path):
    voice = create_voice(tmp_path, "My Voice", WAV, "hello there")
    assert voice["id"] == "my-voice"
    assert voice["name"] == "My Voice"
    assert voice["transcript"] == "hello there"
    assert Path(voice["reference_path"]).read_bytes() == WAV

    listed = list_voices(tmp_path)
    assert [v["id"] for v in listed] == ["my-voice"]


def test_create_collision_gets_suffix(tmp_path: Path):
    first = create_voice(tmp_path, "Dup", WAV, "one")
    second = create_voice(tmp_path, "dup!", WAV, "two")
    assert first["id"] == "dup"
    assert second["id"] == "dup-2"
    assert len(list_voices(tmp_path)) == 2


@pytest.mark.parametrize(
    "name,wav,transcript",
    [("", WAV, "t"), ("n", b"", "t"), ("n", WAV, "  ")],
)
def test_create_rejects_empty_inputs(tmp_path: Path, name, wav, transcript):
    with pytest.raises(ValueError):
        create_voice(tmp_path, name, wav, transcript)


def test_get_rejects_traversal_ids(tmp_path: Path):
    create_voice(tmp_path, "ok", WAV, "t")
    assert get_voice(tmp_path, "ok") is not None
    assert get_voice(tmp_path, "../ok") is None
    assert get_voice(tmp_path, ".") is None
    assert get_voice(tmp_path, "") is None


def test_incomplete_voice_dir_is_ignored(tmp_path: Path):
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "transcript.txt").write_text("t")
    assert list_voices(tmp_path) == []
    assert get_voice(tmp_path, "broken") is None


def test_delete(tmp_path: Path):
    create_voice(tmp_path, "gone", WAV, "t")
    assert delete_voice(tmp_path, "gone") is True
    assert list_voices(tmp_path) == []
    assert delete_voice(tmp_path, "gone") is False
    assert delete_voice(tmp_path, "../etc") is False


def test_list_missing_base_dir(tmp_path: Path):
    assert list_voices(tmp_path / "nope") == []
