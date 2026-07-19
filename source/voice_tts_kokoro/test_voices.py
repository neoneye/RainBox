from voices import VOICES, is_valid_voice, voice_ids


def test_voices_have_required_fields():
    assert len(VOICES) > 0
    for v in VOICES:
        assert set(v.keys()) == {"id", "name", "lang"}
        assert v["id"] and v["name"] and v["lang"]


def test_known_voice_ids_present():
    ids = voice_ids()
    assert "af_heart" in ids
    assert "am_michael" in ids


def test_all_voices_are_american_english():
    # v1 runs a single American-English pipeline (lang_code='a'); ids start a*.
    for v in VOICES:
        assert v["id"][0] == "a"


def test_is_valid_voice():
    assert is_valid_voice("af_heart") is True
    assert is_valid_voice("nope") is False
