"""Defence-in-depth redaction for application and model-call logs.

Redaction is deliberately performed on the structured event before a logger
has a chance to format it.  That matters for file sinks, stdout, exception
rendering and any future sink added to the process: no sink gets the original
value to accidentally persist.

The application logger is an operational record, not a data export.  Prompt
bodies and full document/clause bodies therefore fail closed instead of being
silently shortened.  Callers must log a prompt version, content hash or
other safe reference instead.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

REDACTED_PLACEHOLDER: Final[str] = "***REDACTED***"


class PromptLoggingError(ValueError):
    """Raised when an event attempts to put prompt or body content in a log."""


# These are field names, rather than values.  The application should log
# references and aggregates for these classes, never the underlying value.
DEFAULT_PERSONAL_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "aadhaar",
        "aadhar",
        "account_number",
        "address",
        "bank_account",
        "cin",
        "cin_number",
        "date_of_birth",
        "dob",
        "email",
        "email_address",
        "first_name",
        "full_name",
        "last_name",
        "mobile",
        "mobile_number",
        "name",
        "pan",
        "pan_number",
        "phone",
        "phone_number",
        "principal",
        "principal_id",
        "postal_address",
        "tax_id",
        "telephone",
        "user",
        "user_id",
        "username",
        "user_name",
    }
)

DEFAULT_SECRET_KEY_TOKENS: Final[tuple[str, ...]] = (
    "access_key",
    "api_key",
    "authorization",
    "cookie",
    "mfa",
    "password",
    "private_key",
    "secret",
    "token",
)

DEFAULT_SECRET_PATTERNS: Final[tuple[str, ...]] = (
    # Private-key blocks must be handled before the more general patterns.
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+",
    r"\b(?:sk|rk)-[A-Za-z0-9_-]{16,}\b",
    r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
    r"\b(?:gh[pousr]|xox[baprs])-?[A-Za-z0-9-]{16,}\b",
    # A compact JWT shape is safe to identify without decoding untrusted data.
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",
    r"(?i)\b(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|"
    r"private[_-]?key|authorization|cookie)\b\s*[:=]\s*[\"']?[^\s,;\"']+",
    # Personal-class values can also arrive in a legacy/free-form message.
    r"\b[A-Z]{5}[0-9]{4}[A-Z]\b",
    r"(?<!\d)(?:\+91[- ]?)?[6-9][0-9]{9}(?!\d)",
    r"\b[0-9]{4}[- ]?[0-9]{4}[- ]?[0-9]{4}\b",
    r"\b[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+\b",
)

_PROMPT_FIELD_NAMES: Final[frozenset[str]] = frozenset(
    {
        "clause_body",
        "clause_content",
        "clause_text",
        "document_body",
        "document_content",
        "document_text",
        "full_document",
        "model_prompt",
        "messages",
        "prompt",
        "prompt_body",
        "prompt_content",
        "prompt_messages",
        "prompt_text",
        "raw_document",
    }
)
_PROMPT_EVENT_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)\b(?:prompt|full\s+document|document|clause)\s+body\s*[:=]"
)
_SAFE_METADATA_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "event_name",
        "job_run_id",
        "logger_name",
        "model_name",
        "model_version",
        "permission_name",
        "prompt_hash",
        "prompt_id",
        "prompt_version",
        "request_id",
        "route_name",
        "token_count",
        "tokens_in",
        "tokens_out",
        "token_usage",
    }
)


def normalise_field_name(value: object) -> str:
    """Return a separator-insensitive field name for matching policy names."""

    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(value).strip())
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def is_personal_field(field_name: object, configured: frozenset[str]) -> bool:
    """Return whether ``field_name`` is a configured personal-class field.

    Exact matching handles fields such as ``email`` and ``pan_number``.  The
    suffix rule covers namespaced ORM/event keys such as ``borrower.full_name``
    without classifying safe fields such as ``model_name`` as personal data.
    """

    normalised = normalise_field_name(field_name)
    if normalised in _SAFE_METADATA_FIELDS:
        return False
    if normalised in configured:
        return True
    return any(
        normalised.endswith(f"_{field}")
        for field in configured
        if field in {"aadhaar", "aadhar", "address", "cin", "dob", "email", "name", "pan"}
    )


@dataclass(frozen=True, slots=True)
class RedactionPolicy:
    """Immutable policy used by :class:`RedactionProcessor`."""

    personal_fields: frozenset[str] = DEFAULT_PERSONAL_FIELDS
    secret_key_tokens: tuple[str, ...] = DEFAULT_SECRET_KEY_TOKENS
    secret_patterns: tuple[str, ...] = DEFAULT_SECRET_PATTERNS
    reject_prompt_bodies: bool = True

    def __post_init__(self) -> None:
        if not self.personal_fields:
            raise ValueError("At least one personal field is required for log redaction.")
        if not self.secret_key_tokens:
            raise ValueError("At least one secret key token is required for log redaction.")
        if any(not normalise_field_name(field) for field in self.personal_fields):
            raise ValueError("Personal log field names must be non-empty.")
        if any(not normalise_field_name(token) for token in self.secret_key_tokens):
            raise ValueError("Secret log key tokens must be non-empty.")
        for pattern in self.secret_patterns:
            re.compile(pattern)


class RedactionProcessor:
    """Structlog processor that redacts sensitive values recursively."""

    def __init__(self, policy: RedactionPolicy | None = None) -> None:
        self.policy = policy or RedactionPolicy()
        self._personal_fields = frozenset(
            normalise_field_name(field) for field in self.policy.personal_fields
        )
        self._secret_key_tokens = tuple(
            normalise_field_name(token) for token in self.policy.secret_key_tokens
        )
        self._secret_patterns = tuple(
            re.compile(pattern) for pattern in self.policy.secret_patterns
        )

    def __call__(
        self,
        logger: object,
        method_name: str,
        event_dict: MutableMapping[str, Any],
    ) -> MutableMapping[str, Any]:
        del method_name
        # Reject before mutating the event, which makes this behaviour obvious
        # to callers and guarantees that no partially formatted event is sent.
        if self.policy.reject_prompt_bodies:
            self._reject_prompt_bodies(event_dict, seen=set())
        return self._redact_mapping(event_dict, seen=set())

    def _reject_prompt_bodies(
        self,
        event_dict: Mapping[str, Any],
        *,
        seen: set[int],
    ) -> None:
        identity = id(event_dict)
        if identity in seen:
            return
        seen.add(identity)
        for key, value in event_dict.items():
            normalised = normalise_field_name(key)
            if _is_prompt_field(normalised):
                raise PromptLoggingError(
                    f"Log field '{key}' contains prohibited prompt or body content; "
                    "log a version or content hash instead."
                )
            if normalised == "event" and isinstance(value, str) and _PROMPT_EVENT_RE.search(value):
                raise PromptLoggingError(
                    "Log event appears to contain prohibited prompt or body content; "
                    "log a version or content hash instead."
                )
            if isinstance(value, Mapping):
                self._reject_prompt_bodies(value, seen=seen)
            elif isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
                for item in value:
                    if isinstance(item, Mapping):
                        self._reject_prompt_bodies(item, seen=seen)
        seen.remove(identity)

    def _redact_mapping(
        self,
        mapping: Mapping[str, Any],
        *,
        seen: set[int],
    ) -> dict[str, Any]:
        identity = id(mapping)
        if identity in seen:
            return {"value": REDACTED_PLACEHOLDER}
        seen.add(identity)
        result: dict[str, Any] = {}
        for key, value in mapping.items():
            normalised = normalise_field_name(key)
            if normalised == "exc_info":
                # ``format_exc_info`` needs the original tuple to render a
                # traceback. The second redaction pass sanitizes its output.
                result[key] = value
                continue
            if self._is_secret_key(normalised) or is_personal_field(
                normalised, self._personal_fields
            ):
                result[key] = REDACTED_PLACEHOLDER
            else:
                result[key] = self._redact_value(value, seen=seen)
        seen.remove(identity)
        return result

    def _redact_value(self, value: Any, *, seen: set[int]) -> Any:
        if _is_secret_wrapper(value):
            return REDACTED_PLACEHOLDER
        if isinstance(value, str):
            return self._redact_string(value)
        if isinstance(value, bytes | bytearray | memoryview):
            return REDACTED_PLACEHOLDER
        if isinstance(value, Mapping):
            return self._redact_mapping(value, seen=seen)
        if isinstance(value, list):
            identity = id(value)
            if identity in seen:
                return REDACTED_PLACEHOLDER
            seen.add(identity)
            result = [self._redact_value(item, seen=seen) for item in value]
            seen.remove(identity)
            return result
        if isinstance(value, tuple):
            return tuple(self._redact_value(item, seen=seen) for item in value)
        if isinstance(value, set | frozenset):
            return [self._redact_value(item, seen=seen) for item in value]
        if isinstance(value, BaseException):
            return type(value).__name__
        if isinstance(value, UUID | Decimal | date | datetime):
            return value
        if value is None or isinstance(value, bool | int | float):
            return value
        # Arbitrary object reprs frequently contain customer data. Callers can
        # log a stable reference or an explicitly structured safe mapping.
        return REDACTED_PLACEHOLDER

    def _redact_string(self, value: str) -> str:
        result = value
        for pattern in self._secret_patterns:
            result = pattern.sub(REDACTED_PLACEHOLDER, result)
        return result

    def _is_secret_key(self, field_name: str) -> bool:
        return field_name not in _SAFE_METADATA_FIELDS and any(
            token in field_name for token in self._secret_key_tokens
        )


def _is_prompt_field(field_name: str) -> bool:
    return field_name in _PROMPT_FIELD_NAMES or field_name.endswith(
        ("_prompt", "_prompt_body", "_document_body", "_clause_body")
    )


def _is_secret_wrapper(value: object) -> bool:
    getter = getattr(value, "get_secret_value", None)
    return callable(getter)


__all__ = [
    "DEFAULT_PERSONAL_FIELDS",
    "DEFAULT_SECRET_KEY_TOKENS",
    "DEFAULT_SECRET_PATTERNS",
    "PromptLoggingError",
    "REDACTED_PLACEHOLDER",
    "RedactionPolicy",
    "RedactionProcessor",
    "is_personal_field",
    "normalise_field_name",
]
