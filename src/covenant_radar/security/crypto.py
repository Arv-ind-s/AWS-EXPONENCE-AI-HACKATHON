"""Authenticated field encryption, fingerprints, and safe key rotation."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import secrets as random_secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from covenant_radar.core.errors import ExternalServiceError
from covenant_radar.security.secrets import CryptoSecrets, KeyringBackend, load_crypto_secrets

_FORMAT = "cr1"
_NONCE_BYTES = 12
_KEY_BYTES = 32
_KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_AAD_PREFIX = b"covenant-radar/field-encryption/v1"
T = TypeVar("T")
Cursor = str | None


class CryptoConfigurationError(ValueError):
    """Raised when an explicitly supplied cryptographic configuration is invalid."""


class FieldEncryptor:
    """Encrypt text using AES-256-GCM and a versioned key envelope.

    The envelope is ``cr1.<key-id>.<nonce>.<ciphertext-and-tag>`` where the
    key ID and binary values use unpadded URL-safe base64.  The key ID is
    authenticated as additional data, so changing it cannot redirect a value
    to another key without failing authentication.
    """

    def __init__(
        self,
        keys: Mapping[str, bytes],
        active_key_id: str,
        *,
        associated_data: bytes = _AAD_PREFIX,
    ) -> None:
        copied_keys = dict(keys)
        if not copied_keys:
            raise CryptoConfigurationError("At least one field-encryption key is required.")
        if active_key_id not in copied_keys:
            raise CryptoConfigurationError(
                "The active field-encryption key identifier is not in the supplied key set."
            )
        if not associated_data:
            raise CryptoConfigurationError("Field-encryption associated data cannot be empty.")
        for key_id, key in copied_keys.items():
            if _KEY_ID_PATTERN.fullmatch(key_id) is None:
                raise CryptoConfigurationError(
                    "Field-encryption key identifiers have invalid syntax."
                )
            if not isinstance(key, bytes) or len(key) != _KEY_BYTES:
                raise CryptoConfigurationError(
                    "Field-encryption keys must contain exactly 32 bytes."
                )
        self._keys = copied_keys
        self._active_key_id = active_key_id
        self._associated_data = bytes(associated_data)

    @classmethod
    def from_environment(
        cls,
        *,
        environment: Mapping[str, str] | None = None,
        keyring_backend: KeyringBackend | None = None,
    ) -> FieldEncryptor:
        """Create an encryptor from environment/keyring-only key material."""
        loaded = load_crypto_secrets(
            environment=environment,
            keyring_backend=keyring_backend,
        )
        return cls.from_secrets(loaded)

    @classmethod
    def from_secrets(cls, loaded: CryptoSecrets) -> FieldEncryptor:
        """Create an encryptor from already validated secret material."""
        return cls(loaded.field_keys, loaded.active_field_key_id)

    @property
    def active_key_id(self) -> str:
        """The ID used for newly encrypted values."""
        return self._active_key_id

    def encrypt(self, value: str | bytes | None) -> str | None:
        """Return an authenticated ciphertext, preserving SQL ``NULL``."""
        if value is None:
            return None
        plaintext = _as_bytes(value)
        nonce = random_secrets.token_bytes(_NONCE_BYTES)
        key_id = self._active_key_id.encode("ascii")
        ciphertext = AESGCM(self._keys[self._active_key_id]).encrypt(
            nonce,
            plaintext,
            self._associated_data + b":" + key_id,
        )
        return ".".join(
            (
                _FORMAT,
                _encode(key_id),
                _encode(nonce),
                _encode(ciphertext),
            )
        )

    def decrypt(self, value: str | None) -> str | None:
        """Authenticate and decrypt one value or raise a safe domain error."""
        if value is None:
            return None
        key_id, nonce, ciphertext = self._parse(value)
        key = self._keys.get(key_id)
        if key is None:
            raise ExternalServiceError(f"Encrypted value uses unknown key identifier {key_id!r}.")
        try:
            plaintext = AESGCM(key).decrypt(
                nonce,
                ciphertext,
                self._associated_data + b":" + key_id.encode("ascii"),
            )
        except (InvalidTag, ValueError) as error:
            raise ExternalServiceError("Encrypted value failed authentication.") from error
        try:
            return plaintext.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ExternalServiceError("Encrypted value is not valid UTF-8 text.") from error

    def key_id(self, value: str) -> str:
        """Return the non-secret key ID carried by a ciphertext envelope."""
        return self._parse(value)[0]

    def is_current(self, value: str) -> bool:
        """Return whether a value already uses the active encryption key."""
        return self.key_id(value) == self._active_key_id

    def reencrypt(self, value: str | None) -> str | None:
        """Decrypt with its recorded key and encrypt with the active key."""
        if value is None or self.is_current(value):
            return value
        return self.encrypt(self.decrypt(value))

    def _parse(self, value: str) -> tuple[str, bytes, bytes]:
        if not isinstance(value, str):
            raise ExternalServiceError("Encrypted value is not text.")
        parts = value.split(".")
        if len(parts) != 4 or parts[0] != _FORMAT:
            raise ExternalServiceError("Encrypted value has an unsupported format.")
        try:
            key_id_bytes = _decode(parts[1])
            key_id = key_id_bytes.decode("ascii")
            nonce = _decode(parts[2])
            ciphertext = _decode(parts[3])
        except (binascii.Error, ValueError, UnicodeDecodeError) as error:
            raise ExternalServiceError("Encrypted value is malformed.") from error
        if _KEY_ID_PATTERN.fullmatch(key_id) is None:
            raise ExternalServiceError("Encrypted value contains an invalid key identifier.")
        if len(nonce) != _NONCE_BYTES or len(ciphertext) < 16:
            raise ExternalServiceError("Encrypted value is malformed.")
        return key_id, nonce, ciphertext


class HMACFingerprinter:
    """Produce deterministic, non-reversible identity fingerprints."""

    def __init__(self, key: bytes, *, normalize: Callable[[str], str] | None = None) -> None:
        if not isinstance(key, bytes) or len(key) != _KEY_BYTES:
            raise CryptoConfigurationError("The fingerprint key must contain exactly 32 bytes.")
        self._key = key
        self._normalize = normalize or _canonical_identity

    @classmethod
    def from_environment(
        cls,
        *,
        environment: Mapping[str, str] | None = None,
        keyring_backend: KeyringBackend | None = None,
    ) -> HMACFingerprinter:
        """Create a fingerprinter from environment/keyring-only key material."""
        loaded = load_crypto_secrets(
            environment=environment,
            keyring_backend=keyring_backend,
        )
        return cls(loaded.fingerprint_key)

    def fingerprint(self, value: str | bytes | None) -> str | None:
        """Return a full SHA-256 HMAC digest as lowercase hexadecimal."""
        if value is None:
            return None
        if isinstance(value, bytes):
            try:
                text = value.decode("utf-8")
            except UnicodeDecodeError as error:
                raise TypeError("Fingerprint input bytes must be valid UTF-8.") from error
        elif isinstance(value, str):
            text = value
        else:
            raise TypeError(f"Fingerprint input must be text or bytes, not {type(value).__name__}.")
        canonical = self._normalize(text)
        if not isinstance(canonical, str):
            raise TypeError("The fingerprint normalizer must return text.")
        return hmac.new(self._key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class EncryptedField(Generic[T]):
    """An encrypted attribute that a rotation job can read and replace."""

    name: str
    read: Callable[[T], str | None]
    write: Callable[[T, str | None], None]

    @classmethod
    def attribute(cls, name: str) -> EncryptedField[T]:
        """Build a field descriptor for an object attribute."""
        if not name or not name.isidentifier():
            raise ValueError("Encrypted field names must be non-empty identifiers.")
        return cls(
            name, lambda row: getattr(row, name), lambda row, value: setattr(row, name, value)
        )


class RotationCheckpointStore(Protocol):
    """Durable cursor storage supplied by the database adapter."""

    def load(self, job_name: str) -> str | None:
        """Return the last cursor committed for ``job_name``."""

    def save(self, job_name: str, cursor: str) -> None:
        """Persist a cursor after its corresponding batch is committed."""


@dataclass(frozen=True)
class RotationProgress:
    """Non-sensitive progress emitted after a successful committed batch."""

    job_name: str
    processed: int
    rotated: int
    unchanged: int
    last_cursor: str
    complete: bool


class ResumableRotation(Generic[T]):
    """Rotate rows through caller-owned, transactional batch callbacks.

    ``commit_batch`` must commit one database transaction.  The checkpoint is
    saved only after that callback returns.  If a process stops between those
    two operations, the batch is replayed; current-key rows are idempotently
    skipped, so no row is half-rotated and no cursor can skip an uncommitted
    row.  The checkpoint store is intentionally a port: its implementation
    belongs beside the database schema that owns its durable state.
    """

    def __init__(
        self,
        *,
        job_name: str,
        fetch_batch: Callable[[Cursor, int], Sequence[T]],
        commit_batch: Callable[[Sequence[T]], None],
        row_id: Callable[[T], object],
        fields: Sequence[EncryptedField[T]],
        encryptor: FieldEncryptor,
        checkpoint_store: RotationCheckpointStore,
        batch_size: int = 100,
        progress_callback: Callable[[RotationProgress], None] | None = None,
    ) -> None:
        if not job_name or not job_name.strip():
            raise ValueError("Rotation job name must be non-empty.")
        if batch_size < 1:
            raise ValueError("Rotation batch size must be at least one.")
        if not fields:
            raise ValueError("At least one encrypted field is required for rotation.")
        names = [field.name for field in fields]
        if len(names) != len(set(names)):
            raise ValueError("Rotation encrypted field names must be unique.")
        self._job_name = job_name
        self._fetch_batch = fetch_batch
        self._commit_batch = commit_batch
        self._row_id = row_id
        self._fields = tuple(fields)
        self._encryptor = encryptor
        self._checkpoint_store = checkpoint_store
        self._batch_size = batch_size
        self._progress_callback = progress_callback

    def run(self, *, max_batches: int | None = None) -> RotationProgress:
        """Process batches until exhausted or an explicit batch limit is met."""
        if max_batches is not None and max_batches < 1:
            raise ValueError("The maximum number of rotation batches must be at least one.")
        cursor = self._checkpoint_store.load(self._job_name)
        processed = rotated = unchanged = batches = 0
        while max_batches is None or batches < max_batches:
            batch = tuple(self._fetch_batch(cursor, self._batch_size))
            if not batch:
                return self._progress(
                    processed=processed,
                    rotated=rotated,
                    unchanged=unchanged,
                    cursor=cursor,
                    complete=True,
                )
            if len(batch) > self._batch_size:
                raise RuntimeError("Rotation fetch_batch returned more rows than its batch size.")

            next_cursor = str(self._row_id(batch[-1]))
            if not next_cursor or next_cursor == cursor:
                raise RuntimeError("Rotation cursor did not advance after a non-empty batch.")
            batch_rotated = self._rotate_batch(batch)
            self._commit_batch(batch)
            self._checkpoint_store.save(self._job_name, next_cursor)

            batch_processed = len(batch)
            batch_unchanged = batch_processed - batch_rotated
            processed += batch_processed
            rotated += batch_rotated
            unchanged += batch_unchanged
            batches += 1
            cursor = next_cursor
            self._emit(
                processed=processed,
                rotated=rotated,
                unchanged=unchanged,
                cursor=cursor,
                complete=False,
            )

        return self._progress(
            processed=processed,
            rotated=rotated,
            unchanged=unchanged,
            cursor=cursor,
            complete=False,
        )

    def _rotate_batch(self, batch: Sequence[T]) -> int:
        changed_rows = 0
        for row in batch:
            row_changed = False
            for field in self._fields:
                value = field.read(row)
                if value is None:
                    continue
                if not self._encryptor.is_current(value):
                    field.write(row, self._encryptor.reencrypt(value))
                    row_changed = True
            if row_changed:
                changed_rows += 1
        return changed_rows

    def _progress(
        self,
        *,
        processed: int,
        rotated: int,
        unchanged: int,
        cursor: str | None,
        complete: bool,
    ) -> RotationProgress:
        if cursor is None:
            cursor = ""
        progress = RotationProgress(
            self._job_name,
            processed,
            rotated,
            unchanged,
            cursor,
            complete,
        )
        if self._progress_callback is not None:
            self._progress_callback(progress)
        return progress

    def _emit(
        self,
        *,
        processed: int,
        rotated: int,
        unchanged: int,
        cursor: str,
        complete: bool,
    ) -> None:
        self._progress(
            processed=processed,
            rotated=rotated,
            unchanged=unchanged,
            cursor=cursor,
            complete=complete,
        )


def _as_bytes(value: str | bytes) -> bytes:
    if isinstance(value, str):
        return value.encode("utf-8")
    if isinstance(value, bytes):
        return value
    raise TypeError(f"Encrypted value must be text or bytes, not {type(value).__name__}.")


def _canonical_identity(value: str) -> str:
    return "".join(value.split()).upper()


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    if not value or any(
        character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        for character in value
    ):
        raise ValueError("invalid base64")
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded)


__all__ = [
    "CryptoConfigurationError",
    "EncryptedField",
    "FieldEncryptor",
    "HMACFingerprinter",
    "ResumableRotation",
    "RotationCheckpointStore",
    "RotationProgress",
]
