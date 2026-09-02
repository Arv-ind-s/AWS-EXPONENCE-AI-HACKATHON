"""TOTP second-factor enrollment and verification.

Secrets are generated locally, encrypted before they are handed to a user
store, and verified with the RFC 6238 algorithm. The code intentionally has
no QR-code dependency: the provisioning URI works with any authenticator,
and the HTML route also presents the secret for an accessible manual setup.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from urllib.parse import quote

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.errors import ValidationError

_SECRET_BYTES: Final[int] = 20
_NONCE_BYTES: Final[int] = 12
_KEY_BYTES: Final[int] = 32
_CODE_DIGITS: Final[int] = 6
_TIME_STEP_SECONDS: Final[int] = 30
_AAD: Final[bytes] = b"covenant-radar/mfa-secret/v1"
_CIPHER_PREFIX: Final[str] = "v1."


class MfaError(ValidationError):
    """The MFA input or protected secret is invalid."""

    code = "mfa_error"


@dataclass(frozen=True, slots=True)
class MfaSettings:
    """TOTP and enrollment settings."""

    enabled: bool = False
    issuer: str = "Covenant Radar"
    period_seconds: int = _TIME_STEP_SECONDS
    digits: int = _CODE_DIGITS
    allowed_clock_skew_steps: int = 1

    def __post_init__(self) -> None:
        if not self.issuer.strip() or len(self.issuer) > 64:
            raise ValueError("MFA issuer must contain between 1 and 64 characters.")
        if self.period_seconds < 15 or self.period_seconds > 300:
            raise ValueError("MFA TOTP period must be between 15 and 300 seconds.")
        if self.digits not in {6, 8}:
            raise ValueError("MFA TOTP digits must be 6 or 8.")
        if self.allowed_clock_skew_steps < 0 or self.allowed_clock_skew_steps > 3:
            raise ValueError("MFA clock skew must be between 0 and 3 steps.")


@dataclass(frozen=True, slots=True)
class MfaEnrollment:
    """The material shown during enrollment; ``secret`` is shown once."""

    secret: str
    encrypted_secret: str
    provisioning_uri: str


class MfaSecretCipher:
    """AES-256-GCM protection for a TOTP secret at rest."""

    def __init__(self, key: bytes | str) -> None:
        material = key.encode("utf-8") if isinstance(key, str) else key
        if len(material) != _KEY_BYTES:
            raise ValueError("MFA secret key must contain exactly 32 bytes.")
        self._key = material

    def encrypt(self, secret: str) -> str:
        """Encrypt a canonical base32 secret with a fresh nonce."""
        canonical = _canonical_secret(secret)
        nonce = secrets.token_bytes(_NONCE_BYTES)
        ciphertext = AESGCM(self._key).encrypt(nonce, canonical.encode("ascii"), _AAD)
        encoded = base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii").rstrip("=")
        return _CIPHER_PREFIX + encoded

    def decrypt(self, protected: str) -> str:
        """Decrypt and validate a protected base32 secret."""
        if not isinstance(protected, str) or not protected.startswith(_CIPHER_PREFIX):
            raise MfaError("The stored MFA secret has an unsupported protection format.")
        encoded = protected.removeprefix(_CIPHER_PREFIX)
        if not encoded or len(encoded) > 512:
            raise MfaError("The stored MFA secret is malformed.")
        try:
            padded = encoded + "=" * (-len(encoded) % 4)
            decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
            if len(decoded) <= _NONCE_BYTES:
                raise ValueError
            plaintext = AESGCM(self._key).decrypt(
                decoded[:_NONCE_BYTES], decoded[_NONCE_BYTES:], _AAD
            )
            return _canonical_secret(plaintext.decode("ascii"))
        except (binascii.Error, ValueError, UnicodeDecodeError, InvalidTag) as error:
            raise MfaError("The stored MFA secret could not be authenticated.") from error


class TOTPService:
    """Generate, protect and verify RFC 6238 TOTP credentials."""

    def __init__(
        self,
        secret_key: bytes | str,
        *,
        settings: MfaSettings | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.settings = settings or MfaSettings()
        self.clock = clock or SystemClock()
        self.cipher = MfaSecretCipher(secret_key)

    def enroll(self, account_name: str) -> MfaEnrollment:
        """Create an enrollment secret and standards-compatible URI."""
        account = _account_name(account_name)
        secret = base64.b32encode(secrets.token_bytes(_SECRET_BYTES)).decode("ascii").rstrip("=")
        return MfaEnrollment(
            secret=secret,
            encrypted_secret=self.cipher.encrypt(secret),
            provisioning_uri=self.provisioning_uri(account, secret),
        )

    begin_enrollment = enroll

    def provisioning_uri(self, account_name: str, secret: str) -> str:
        """Return an ``otpauth://totp`` URI for authenticator apps."""
        account = _account_name(account_name)
        canonical = _canonical_secret(secret)
        label = f"{self.settings.issuer}:{account}"
        return (
            "otpauth://totp/"
            + quote(label, safe="")
            + "?secret="
            + canonical
            + "&issuer="
            + quote(self.settings.issuer, safe="")
            + f"&algorithm=SHA1&digits={self.settings.digits}&period={self.settings.period_seconds}"
        )

    def verify(self, protected_secret: str, code: str, *, now: datetime | None = None) -> bool:
        """Verify a user-entered code against an encrypted stored secret."""
        try:
            secret = self.cipher.decrypt(protected_secret)
        except MfaError:
            return False
        return self.verify_secret(secret, code, now=now)

    def verify_secret(self, secret: str, code: str, *, now: datetime | None = None) -> bool:
        """Verify a code against a plaintext enrollment secret."""
        if not isinstance(code, str) or len(code) != self.settings.digits or not code.isdecimal():
            return False
        try:
            canonical = _canonical_secret(secret)
        except MfaError:
            return False
        instant = now or self.clock.now()
        timestamp = _timestamp(instant)
        for offset in range(
            -self.settings.allowed_clock_skew_steps,
            self.settings.allowed_clock_skew_steps + 1,
        ):
            expected = self.code_for_secret(
                canonical, timestamp + offset * self.settings.period_seconds
            )
            if hmac.compare_digest(expected, code):
                return True
        return False

    def code_for_secret(self, secret: str, timestamp: int | float | datetime | None = None) -> str:
        """Generate a testable TOTP code for a secret and instant."""
        canonical = _canonical_secret(secret)
        if timestamp is None:
            timestamp_value = _timestamp(self.clock.now())
        elif isinstance(timestamp, datetime):
            timestamp_value = _timestamp(timestamp)
        else:
            timestamp_value = int(timestamp)
        counter = timestamp_value // self.settings.period_seconds
        message = counter.to_bytes(8, byteorder="big", signed=False)
        digest = hmac.new(
            base64.b32decode(_pad_base32(canonical), casefold=True), message, hashlib.sha1
        ).digest()
        offset = digest[-1] & 0x0F
        binary = int.from_bytes(digest[offset : offset + 4], byteorder="big") & 0x7FFFFFFF
        return str(binary % (10**self.settings.digits)).zfill(self.settings.digits)


def _canonical_secret(secret: str) -> str:
    if not isinstance(secret, str):
        raise MfaError("MFA secret must be text.")
    canonical = "".join(secret.split()).upper().rstrip("=")
    if (
        not canonical
        or len(canonical) > 128
        or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for character in canonical)
    ):
        raise MfaError("MFA secret is not valid base32.")
    try:
        decoded = base64.b32decode(_pad_base32(canonical), casefold=True)
    except (binascii.Error, ValueError) as error:
        raise MfaError("MFA secret is not valid base32.") from error
    if not decoded:
        raise MfaError("MFA secret is empty.")
    return canonical


def _pad_base32(value: str) -> str:
    return value + "=" * (-len(value) % 8)


def _account_name(account_name: str) -> str:
    if not isinstance(account_name, str):
        raise ValueError("MFA account name must be text.")
    clean = account_name.strip()
    if not clean or len(clean) > 128 or any(ord(character) < 32 for character in clean):
        raise ValueError("MFA account name is invalid.")
    return clean


def _timestamp(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("MFA timestamps must be timezone-aware.")
    return int(value.timestamp())


MFAService = TOTPService
