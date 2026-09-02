"""Secret loading for cryptographic services.

Secrets are deliberately restricted to two sources: the process environment
and the operating system keyring.  In particular, this module does not accept
paths, configuration mappings, or a file fallback.  Keeping that boundary in
one small module makes it difficult for a caller to accidentally turn a
configuration value into a credential source.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import import_module
from types import MappingProxyType
from typing import Protocol, cast

FIELD_ENCRYPTION_KEY_ENV = "COVENANT_RADAR_SECURITY_FIELD_ENCRYPTION_KEY"
FIELD_ENCRYPTION_KEYS_ENV = "COVENANT_RADAR_SECURITY_FIELD_ENCRYPTION_KEYS"
FIELD_ENCRYPTION_ACTIVE_KEY_ID_ENV = "COVENANT_RADAR_SECURITY_FIELD_ENCRYPTION_ACTIVE_KEY_ID"
CIN_FINGERPRINT_KEY_ENV = "COVENANT_RADAR_SECURITY_CIN_FINGERPRINT_KEY"
KEYRING_SERVICE = "covenant-radar"

_KEY_BYTES = 32
_KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_ENVIRONMENT_VARIABLE_PATTERN = re.compile(r"^COVENANT_RADAR_[A-Z0-9_]+$")


class SecretLoadError(RuntimeError):
    """Raised when a required secret cannot be loaded safely."""


class KeyringBackend(Protocol):
    """The minimal optional OS-keyring interface used by this package."""

    def get_password(self, service_name: str, username: str) -> str | None:
        """Return a secret, or ``None`` when it is not present."""


@dataclass(frozen=True)
class CryptoSecrets:
    """Validated key material used by the field cipher and fingerprinter."""

    field_keys: Mapping[str, bytes]
    active_field_key_id: str
    fingerprint_key: bytes

    def __post_init__(self) -> None:
        keys = dict(self.field_keys)
        if not keys:
            raise SecretLoadError("At least one field-encryption key is required.")
        if self.active_field_key_id not in keys:
            raise SecretLoadError(
                "The active field-encryption key identifier is not present in the loaded key set."
            )
        for key_id, key in keys.items():
            _validate_key_id(key_id, "field-encryption key identifier")
            _validate_key_bytes(key, "field-encryption key")
        _validate_key_bytes(self.fingerprint_key, "CIN fingerprint key")
        if any(key == self.fingerprint_key for key in keys.values()):
            raise SecretLoadError(
                "The CIN fingerprint key must be different from every field-encryption key."
            )
        object.__setattr__(self, "field_keys", MappingProxyType(keys))


@dataclass(frozen=True)
class SecretLoader:
    """Read secrets from an explicit environment and optional keyring.

    ``environment`` and ``keyring_backend`` are injectable for deterministic
    tests and for process hosts that provide their own keyring integration.
    Neither injection accepts a path or reads a file.
    """

    environment: Mapping[str, str] | None = None
    keyring_backend: KeyringBackend | None = None
    keyring_service: str = KEYRING_SERVICE

    def get(self, environment_variable: str, *, required: bool = True) -> str | None:
        """Read one secret, preferring the environment over the OS keyring."""
        _validate_environment_variable(environment_variable)
        environment = os.environ if self.environment is None else self.environment

        if environment_variable in environment:
            value = environment[environment_variable]
            if not isinstance(value, str) or not value:
                raise SecretLoadError(
                    f"Secret environment variable {environment_variable} is empty."
                )
            return value

        try:
            keyring = self._keyring()
        except ImportError as error:
            raise SecretLoadError(
                f"OS keyring provider could not be loaded for {environment_variable}."
            ) from error
        if keyring is not None:
            try:
                keyring_value = keyring.get_password(self.keyring_service, environment_variable)
            except Exception as error:
                raise SecretLoadError(
                    f"OS keyring could not be read for {environment_variable}."
                ) from error
            if keyring_value:
                if not isinstance(keyring_value, str):
                    raise SecretLoadError(
                        f"OS keyring returned a non-text value for {environment_variable}."
                    )
                return keyring_value

        if required:
            raise SecretLoadError(f"Missing required secret: {environment_variable}.")
        return None

    def _keyring(self) -> KeyringBackend | None:
        if self.keyring_backend is not None:
            return self.keyring_backend
        try:
            module = import_module("keyring")
        except ModuleNotFoundError:
            return None
        return cast(KeyringBackend, module)


def load_crypto_secrets(
    *,
    environment: Mapping[str, str] | None = None,
    keyring_backend: KeyringBackend | None = None,
) -> CryptoSecrets:
    """Load and validate the field-encryption and fingerprint keys.

    ``FIELD_ENCRYPTION_KEYS_ENV`` accepts a JSON object mapping key IDs to
    encoded keys.  The singular key variable remains supported for a fresh
    deployment and uses the active ID (or ``v1`` when no ID is supplied).
    Existing keys can therefore remain available while rows are rotated to a
    newly active key.
    """
    loader = SecretLoader(environment=environment, keyring_backend=keyring_backend)
    encoded_key_set = loader.get(FIELD_ENCRYPTION_KEYS_ENV, required=False)
    encoded_single_key = loader.get(FIELD_ENCRYPTION_KEY_ENV, required=False)
    if encoded_key_set is not None and encoded_single_key is not None:
        raise SecretLoadError(
            f"Configure only one of {FIELD_ENCRYPTION_KEY_ENV} and {FIELD_ENCRYPTION_KEYS_ENV}."
        )

    active_key_id = _active_key_id(environment)
    if encoded_key_set is not None:
        field_keys = _decode_key_set(encoded_key_set, FIELD_ENCRYPTION_KEYS_ENV)
    else:
        if encoded_single_key is None:
            encoded_single_key = _require_secret(
                loader.get(FIELD_ENCRYPTION_KEY_ENV, required=True), FIELD_ENCRYPTION_KEY_ENV
            )
        if active_key_id is None:
            active_key_id = "v1"
        field_keys = {
            active_key_id: _decode_key(
                encoded_single_key, FIELD_ENCRYPTION_KEY_ENV, allow_text_key=True
            )
        }

    if active_key_id is None:
        if len(field_keys) != 1:
            raise SecretLoadError(
                f"{FIELD_ENCRYPTION_ACTIVE_KEY_ID_ENV} is required when multiple "
                "field-encryption keys are configured."
            )
        active_key_id = next(iter(field_keys))
    _validate_key_id(active_key_id, "active field-encryption key identifier")

    encoded_fingerprint_key = loader.get(CIN_FINGERPRINT_KEY_ENV)
    fingerprint_key = _decode_key(
        _require_secret(encoded_fingerprint_key, CIN_FINGERPRINT_KEY_ENV),
        CIN_FINGERPRINT_KEY_ENV,
        allow_text_key=True,
    )
    return CryptoSecrets(field_keys, active_key_id, fingerprint_key)


def load_secret(
    environment_variable: str,
    *,
    environment: Mapping[str, str] | None = None,
    keyring_backend: KeyringBackend | None = None,
) -> str:
    """Load one required secret without exposing it in diagnostics."""
    value = SecretLoader(environment=environment, keyring_backend=keyring_backend).get(
        environment_variable
    )
    return _require_secret(value, environment_variable)


def _active_key_id(environment: Mapping[str, str] | None) -> str | None:
    values = os.environ if environment is None else environment
    if FIELD_ENCRYPTION_ACTIVE_KEY_ID_ENV not in values:
        return None
    value = values[FIELD_ENCRYPTION_ACTIVE_KEY_ID_ENV]
    if not value:
        raise SecretLoadError(
            f"Secret environment variable {FIELD_ENCRYPTION_ACTIVE_KEY_ID_ENV} is empty."
        )
    return value


def _decode_key_set(encoded: str, environment_variable: str) -> dict[str, bytes]:
    try:
        parsed = json.loads(encoded)
    except (json.JSONDecodeError, TypeError) as error:
        raise SecretLoadError(
            f"Secret {environment_variable} must contain a JSON object of key IDs."
        ) from error
    if not isinstance(parsed, dict) or not parsed:
        raise SecretLoadError(
            f"Secret {environment_variable} must contain a non-empty JSON object of key IDs."
        )

    result: dict[str, bytes] = {}
    for key_id, value in parsed.items():
        if not isinstance(key_id, str) or not _KEY_ID_PATTERN.fullmatch(key_id):
            raise SecretLoadError(
                f"Secret {environment_variable} contains an invalid field-encryption "
                "key identifier."
            )
        if not isinstance(value, str):
            raise SecretLoadError(
                f"Secret {environment_variable} contains a key that is not text-encoded."
            )
        result[key_id] = _decode_key(value, environment_variable, allow_text_key=True)
    return result


def _decode_key(value: str, environment_variable: str, *, allow_text_key: bool) -> bytes:
    if not isinstance(value, str) or not value:
        raise SecretLoadError(f"Secret {environment_variable} is empty.")

    if value.startswith("hex:"):
        try:
            decoded = binascii.unhexlify(value[4:])
        except (binascii.Error, ValueError) as error:
            raise SecretLoadError(
                f"Secret {environment_variable} is not valid hexadecimal key material."
            ) from error
    elif value.startswith("base64:"):
        decoded = _decode_base64(value[7:], environment_variable)
    else:
        decoded = _decode_base64(value, environment_variable, strict=False)
        if len(decoded) != _KEY_BYTES and allow_text_key:
            decoded = value.encode("utf-8")

    _validate_key_bytes(decoded, environment_variable)
    return decoded


def _require_secret(value: str | None, environment_variable: str) -> str:
    if value is None:
        raise SecretLoadError(f"Missing required secret: {environment_variable}.")
    return value


def _decode_base64(value: str, environment_variable: str, *, strict: bool = True) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    try:
        return base64.b64decode(padded, altchars=b"-_", validate=strict)
    except (binascii.Error, ValueError) as error:
        raise SecretLoadError(
            f"Secret {environment_variable} is not valid base64 key material."
        ) from error


def _validate_key_bytes(key: bytes, label: str) -> None:
    if not isinstance(key, bytes) or len(key) != _KEY_BYTES:
        raise SecretLoadError(f"{label} must decode to exactly {_KEY_BYTES} bytes.")


def _validate_key_id(key_id: str, label: str) -> None:
    if not isinstance(key_id, str) or _KEY_ID_PATTERN.fullmatch(key_id) is None:
        raise SecretLoadError(f"{label} must match [A-Za-z0-9][A-Za-z0-9_.-]{{0,63}}.")


def _validate_environment_variable(environment_variable: str) -> None:
    if _ENVIRONMENT_VARIABLE_PATTERN.fullmatch(environment_variable) is None:
        raise SecretLoadError("Secret names must be COVENANT_RADAR_* environment variables.")


__all__ = [
    "CIN_FINGERPRINT_KEY_ENV",
    "CryptoSecrets",
    "FIELD_ENCRYPTION_ACTIVE_KEY_ID_ENV",
    "FIELD_ENCRYPTION_KEY_ENV",
    "FIELD_ENCRYPTION_KEYS_ENV",
    "KEYRING_SERVICE",
    "KeyringBackend",
    "SecretLoadError",
    "SecretLoader",
    "load_crypto_secrets",
    "load_secret",
]
