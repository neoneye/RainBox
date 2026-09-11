"""Sealing bridge credentials for storage in Postgres.

A credential row holds AES-256-GCM ciphertext under a key derived with
HKDF-SHA256 from the operator's `RAINBOX_CREDENTIAL_KEY` (the "pepper", which
lives only in the core's environment / repo-root `.env`) and a random
per-row salt; each row also has its own random nonce. A dump of the database
alone yields nothing usable; the pepper alone yields nothing either.

Design: docs/superpowers/specs/2026-09-09-bridge-settings-design.md
("Three homes", "Credentials and process identity").
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

KEY_ENV = "RAINBOX_CREDENTIAL_KEY"
MIN_KEY_CHARS = 32
SEAL_VERSION = 1
_INFO = b"rainbox bridge credential v1"


class CredentialKeyMissing(RuntimeError):
    """`RAINBOX_CREDENTIAL_KEY` is unset or too short: nothing can be sealed
    or opened. The message tells the operator how to fix it."""

    def __init__(self) -> None:
        super().__init__(
            f"{KEY_ENV} is not set (or is shorter than {MIN_KEY_CHARS} characters). Add a line "
            f"{KEY_ENV}=<random string> to the repo-root .env — for example the output of "
            "`python3 -c \"import secrets; print(secrets.token_urlsafe(48))\"` — and restart the core. "
            "Keep it with your backups: credentials sealed under it cannot be opened without it."
        )


class CredentialUnopenable(RuntimeError):
    """The row does not open under the configured key (a different key, or a
    corrupted row): the operator must save the value again."""


@dataclass(frozen=True)
class Sealed:
    version: int
    salt: bytes
    nonce: bytes
    ciphertext: bytes


def pepper(env: dict[str, str] | os._Environ[str] | None = None) -> str | None:
    """The configured key, or None when absent/too short."""
    value = (env if env is not None else os.environ).get(KEY_ENV, "").strip()
    return value if len(value) >= MIN_KEY_CHARS else None


def key_configured(env: dict[str, str] | os._Environ[str] | None = None) -> bool:
    return pepper(env) is not None


def _derive(secret: str, salt: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=_INFO).derive(secret.encode("utf-8"))


def seal(value: str, secret: str | None = None) -> Sealed:
    secret = secret if secret is not None else pepper()
    if secret is None:
        raise CredentialKeyMissing()
    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(_derive(secret, salt)).encrypt(nonce, value.encode("utf-8"), _INFO)
    return Sealed(SEAL_VERSION, salt, nonce, ciphertext)


def open_sealed(sealed: Sealed, secret: str | None = None) -> str:
    secret = secret if secret is not None else pepper()
    if secret is None:
        raise CredentialKeyMissing()
    if sealed.version != SEAL_VERSION:
        raise CredentialUnopenable(f"unknown seal version {sealed.version}")
    try:
        plain = AESGCM(_derive(secret, sealed.salt)).decrypt(sealed.nonce, sealed.ciphertext, _INFO)
    except InvalidTag:
        raise CredentialUnopenable(
            f"the stored credential does not open under the current {KEY_ENV} "
            "(the key changed, or the row is corrupt); save the value again") from None
    return plain.decode("utf-8")
