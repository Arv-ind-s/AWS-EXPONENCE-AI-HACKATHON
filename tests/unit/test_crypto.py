"""Unit tests for authenticated fields, fingerprints, and secret loading."""

from __future__ import annotations

import base64

import pytest

from covenant_radar.core.errors import ExternalServiceError
from covenant_radar.security.crypto import FieldEncryptor, HMACFingerprinter
from covenant_radar.security.secrets import (
    CIN_FINGERPRINT_KEY_ENV,
    FIELD_ENCRYPTION_KEY_ENV,
    SecretLoadError,
    load_crypto_secrets,
)

_FIELD_KEY = b"F" * 32
_OLD_FIELD_KEY = b"O" * 32
_FINGERPRINT_KEY = b"P" * 32


def _encryptor() -> FieldEncryptor:
    return FieldEncryptor({"v1": _FIELD_KEY}, "v1")


def _encoded(key: bytes) -> str:
    return base64.urlsafe_b64encode(key).decode("ascii").rstrip("=")


def test_round_trip() -> None:
    encryptor = _encryptor()

    ciphertext = encryptor.encrypt("Promoter: Rhea Holdings")

    assert ciphertext is not None
    assert ciphertext != "Promoter: Rhea Holdings"
    assert encryptor.decrypt(ciphertext) == "Promoter: Rhea Holdings"
    assert encryptor.encrypt(None) is None
    assert encryptor.decrypt(None) is None


def test_ciphertext_carries_key_id() -> None:
    encryptor = FieldEncryptor({"current-2026": _FIELD_KEY}, "current-2026")

    ciphertext = encryptor.encrypt("U12345MH2000PLC000001")

    assert ciphertext is not None
    assert encryptor.key_id(ciphertext) == "current-2026"
    assert ciphertext.split(".")[0] == "cr1"


def test_unknown_key_id_raises() -> None:
    old_encryptor = FieldEncryptor({"previous": _OLD_FIELD_KEY}, "previous")
    current_encryptor = FieldEncryptor({"current": _FIELD_KEY}, "current")

    ciphertext = old_encryptor.encrypt("PAN1234X")
    assert ciphertext is not None

    with pytest.raises(ExternalServiceError, match="previous"):
        current_encryptor.decrypt(ciphertext)


def test_fingerprint_deterministic_and_distinct() -> None:
    fingerprinter = HMACFingerprinter(_FINGERPRINT_KEY)

    first = fingerprinter.fingerprint(" u12345 mh 2000 plc 000001 ")
    equivalent = fingerprinter.fingerprint("U12345MH2000PLC000001")
    different = fingerprinter.fingerprint("U12345MH2000PLC000002")

    assert first == equivalent
    assert first != different
    assert first is not None
    assert len(first) == 64


def test_missing_key_refuses_start() -> None:
    with pytest.raises(SecretLoadError, match=FIELD_ENCRYPTION_KEY_ENV):
        load_crypto_secrets(
            environment={CIN_FINGERPRINT_KEY_ENV: _encoded(_FINGERPRINT_KEY)},
        )
