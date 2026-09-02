"""Password hashing and local-password policy.

The local authentication path uses Argon2id exclusively.  This module keeps
the hashing parameters and policy explicit so a deployment can tune them
without changing the authentication flow, and so a successful verification
can transparently upgrade an older hash.

Password verification deliberately has a dummy-hash path.  Callers can use
it for an account lookup that returned no user and still perform the same
expensive Argon2 operation as a real login attempt.  The dummy hash is never
returned or persisted.
"""

from __future__ import annotations

import hmac
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type

from covenant_radar.core.errors import ValidationError

_DEFAULT_TIME_COST: Final[int] = 3
_DEFAULT_MEMORY_COST: Final[int] = 65_536
_DEFAULT_PARALLELISM: Final[int] = 4
_DEFAULT_HASH_LENGTH: Final[int] = 32
_DEFAULT_SALT_LENGTH: Final[int] = 16
_DEFAULT_MIN_LENGTH: Final[int] = 12
_DEFAULT_MAX_LENGTH: Final[int] = 256


class PasswordPolicyError(ValidationError):
    """A candidate password violates the configured password policy."""

    code = "password_policy_error"

    def __init__(self, errors: Iterable[str]) -> None:
        messages = tuple(errors)
        if not messages:  # pragma: no cover - defensive invariant
            messages = ("Password does not meet the configured policy.",)
        self.errors = messages
        super().__init__("Password does not meet policy: " + "; ".join(messages) + ".")


@dataclass(frozen=True, slots=True)
class Argon2Parameters:
    """The complete Argon2id parameter set used for new password hashes."""

    time_cost: int = _DEFAULT_TIME_COST
    memory_cost: int = _DEFAULT_MEMORY_COST
    parallelism: int = _DEFAULT_PARALLELISM
    hash_len: int = _DEFAULT_HASH_LENGTH
    salt_len: int = _DEFAULT_SALT_LENGTH

    def __post_init__(self) -> None:
        if self.time_cost < 1:
            raise ValueError("Argon2 time_cost must be at least 1.")
        if self.memory_cost < 8:
            raise ValueError("Argon2 memory_cost must be at least 8 KiB.")
        if self.parallelism < 1:
            raise ValueError("Argon2 parallelism must be at least 1.")
        if self.hash_len < 4:
            raise ValueError("Argon2 hash_len must be at least 4 bytes.")
        if self.salt_len < 8:
            raise ValueError("Argon2 salt_len must be at least 8 bytes.")

    def hasher(self) -> PasswordHasher:
        """Build an Argon2id hasher for this parameter set."""
        return PasswordHasher(
            time_cost=self.time_cost,
            memory_cost=self.memory_cost,
            parallelism=self.parallelism,
            hash_len=self.hash_len,
            salt_len=self.salt_len,
            type=Type.ID,
        )


@dataclass(frozen=True, slots=True)
class PasswordVerification:
    """The safe result of checking one password against one stored hash."""

    valid: bool
    needs_rehash: bool = False


@dataclass(frozen=True, slots=True)
class PasswordPolicy:
    """Configurable length, complexity and reuse requirements."""

    min_length: int = _DEFAULT_MIN_LENGTH
    max_length: int = _DEFAULT_MAX_LENGTH
    require_uppercase: bool = True
    require_lowercase: bool = True
    require_digit: bool = True
    require_special: bool = True
    history_size: int = 5

    def __post_init__(self) -> None:
        if self.min_length < 1:
            raise ValueError("Password minimum length must be positive.")
        if self.max_length < self.min_length:
            raise ValueError("Password maximum length must not be less than its minimum.")
        if self.history_size < 0:
            raise ValueError("Password history size must not be negative.")

    def validate(
        self,
        password: str,
        *,
        password_service: PasswordService | None = None,
        previous_hashes: Iterable[str] = (),
    ) -> None:
        """Raise ``PasswordPolicyError`` if *password* is not acceptable.

        Reuse checks use Argon2 verification rather than comparing encoded
        hashes, because each valid Argon2 hash contains a fresh salt.
        """
        if not isinstance(password, str):
            raise PasswordPolicyError(("password must be text",))

        errors: list[str] = []
        length = len(password)
        if length < self.min_length:
            errors.append(f"must contain at least {self.min_length} characters")
        if length > self.max_length:
            errors.append(f"must contain no more than {self.max_length} characters")
        if self.require_uppercase and not any(character.isupper() for character in password):
            errors.append("must contain an uppercase character")
        if self.require_lowercase and not any(character.islower() for character in password):
            errors.append("must contain a lowercase character")
        if self.require_digit and not any(character.isdigit() for character in password):
            errors.append("must contain a digit")
        if self.require_special and not any(
            not character.isalnum() and not character.isspace() for character in password
        ):
            errors.append("must contain a non-alphanumeric character")

        if not errors and self.history_size:
            verifier = password_service or PasswordService(policy=self)
            for encoded_hash in tuple(previous_hashes)[: self.history_size]:
                if verifier.verify(password, encoded_hash).valid:
                    errors.append("must not reuse a recent password")
                    break

        if errors:
            raise PasswordPolicyError(errors)


class PasswordService:
    """Hash and verify passwords using one configured Argon2id policy."""

    def __init__(
        self,
        parameters: Argon2Parameters | None = None,
        *,
        policy: PasswordPolicy | None = None,
    ) -> None:
        self.parameters = parameters or Argon2Parameters()
        self.policy = policy or PasswordPolicy()
        self._hasher = self.parameters.hasher()
        # Generate a process-local dummy hash.  It has no usable secret value
        # and is used only to equalise the work for an unknown account.
        self._dummy_hash = self._hasher.hash(secrets.token_urlsafe(32))

    def hash(self, password: str, *, validate: bool = True) -> str:
        """Return a new Argon2id hash for *password*.

        Normal callers should retain the default policy validation.  The
        ``validate=False`` path is only for rehashing a password that has
        already passed verification against an older policy.
        """
        if validate:
            self.policy.validate(password, password_service=self)
        if not isinstance(password, str):
            raise TypeError("password must be text")
        return self._hasher.hash(password)

    hash_password = hash

    def verify(self, password: str, encoded_hash: str | None) -> PasswordVerification:
        """Verify a password without exposing Argon2 implementation errors."""
        candidate_hash = encoded_hash or self._dummy_hash
        valid = False
        try:
            valid = self._hasher.verify(candidate_hash, password)
        except (InvalidHashError, VerificationError, VerifyMismatchError, TypeError):
            # An invalid stored hash is treated like a bad credential, but the
            # dummy verification keeps the missing/corrupt-hash path costly.
            if candidate_hash != self._dummy_hash:
                try:
                    self._hasher.verify(self._dummy_hash, password)
                except (InvalidHashError, VerificationError, VerifyMismatchError, TypeError):
                    pass
            valid = False

        return PasswordVerification(
            valid=valid,
            needs_rehash=valid and self._hasher.check_needs_rehash(candidate_hash),
        )

    def verify_password(self, password: str, encoded_hash: str | None) -> bool:
        """Boolean compatibility spelling for callers that need only validity."""
        return self.verify(password, encoded_hash).valid

    def validate_new_password(
        self,
        password: str,
        *,
        previous_hashes: Iterable[str] = (),
    ) -> None:
        """Apply this service's policy, including recent-password reuse."""
        self.policy.validate(
            password,
            password_service=self,
            previous_hashes=previous_hashes,
        )

    @property
    def dummy_hash(self) -> str:
        """The internal dummy hash, exposed only for authentication tests."""
        return self._dummy_hash


def constant_time_compare(left: str | bytes, right: str | bytes) -> bool:
    """Compare equal-type byte/text values without early-exit timing leaks."""
    if isinstance(left, str) and isinstance(right, str):
        return hmac.compare_digest(left, right)
    if isinstance(left, bytes) and isinstance(right, bytes):
        return hmac.compare_digest(left, right)
    if isinstance(left, str) and isinstance(right, bytes):
        return hmac.compare_digest(left.encode("utf-8"), right)
    if isinstance(left, bytes) and isinstance(right, str):
        return hmac.compare_digest(left, right.encode("utf-8"))
    return False


compare_passwords = constant_time_compare
