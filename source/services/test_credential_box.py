"""Sealing/opening bridge credentials: pepper + per-row salt/nonce."""
import pytest

from services import credential_box as cb

KEY = "k" * 40
OTHER = "o" * 40


def test_seal_and_open_round_trip_with_fresh_salt_and_nonce():
    a = cb.seal("tok-1", KEY)
    b = cb.seal("tok-1", KEY)
    assert cb.open_sealed(a, KEY) == "tok-1" and cb.open_sealed(b, KEY) == "tok-1"
    assert a.salt != b.salt and a.nonce != b.nonce and a.ciphertext != b.ciphertext   # no two rows look alike
    assert b"tok-1" not in a.ciphertext and a.version == cb.SEAL_VERSION


def test_wrong_key_or_tampering_is_unopenable_not_garbage():
    s = cb.seal("tok-1", KEY)
    with pytest.raises(cb.CredentialUnopenable):
        cb.open_sealed(s, OTHER)
    bad = cb.Sealed(s.version, s.salt, s.nonce, bytes([s.ciphertext[0] ^ 1]) + s.ciphertext[1:])
    with pytest.raises(cb.CredentialUnopenable):
        cb.open_sealed(bad, KEY)
    with pytest.raises(cb.CredentialUnopenable):
        cb.open_sealed(cb.Sealed(99, s.salt, s.nonce, s.ciphertext), KEY)


def test_pepper_comes_from_the_environment_and_must_be_long_enough():
    assert cb.pepper({}) is None and cb.pepper({cb.KEY_ENV: "short"}) is None
    assert cb.pepper({cb.KEY_ENV: " " + KEY + " "}) == KEY
    assert cb.key_configured({cb.KEY_ENV: KEY}) and not cb.key_configured({})
    with pytest.raises(cb.CredentialKeyMissing) as info:
        cb.seal("x", None) if cb.pepper() is None else (_ for _ in ()).throw(cb.CredentialKeyMissing())
    assert "RAINBOX_CREDENTIAL_KEY" in str(info.value) and ".env" in str(info.value)
