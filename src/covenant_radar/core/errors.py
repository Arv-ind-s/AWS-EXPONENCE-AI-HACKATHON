"""The single exception hierarchy, mapped once to HTTP status and UI state.

Every error the application raises deliberately — as opposed to a bug
surfacing as an unexpected exception — is one of the classes below. A route
handler or a UI presenter switches on `code` (or the class itself); neither
ever invents a status code or a message of its own.
"""

from __future__ import annotations

from typing import ClassVar


class DomainError(Exception):
    """Base of every error the application raises on purpose.

    Carries a stable `code` used to map the error to an HTTP status and a UI
    state, a human-readable `message`, and an optional `field` naming the
    dotted path of the value at fault (for example ``"documents.local_path"``
    or ``"facility.effective_from"``) so a caller can attach the error to the
    right input without parsing the message text.
    """

    code: ClassVar[str] = "domain_error"

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.field = field

    def __str__(self) -> str:
        return self.message

    def __repr__(self) -> str:
        field_part = f", field={self.field!r}" if self.field is not None else ""
        return f"{type(self).__name__}({self.message!r}{field_part})"


class ValidationError(DomainError):
    """The input is well-formed but fails a business rule or a shape check."""

    code: ClassVar[str] = "validation_error"


class AuthorizationError(DomainError):
    """The principal is known but lacks the permission the action requires."""

    code: ClassVar[str] = "authorization_error"


class NotFound(DomainError):
    """No record matches the identifier within the caller's scope."""

    code: ClassVar[str] = "not_found"


class Conflict(DomainError):
    """The operation collides with existing state — a duplicate, a stale
    version, or a constraint the database itself enforces."""

    code: ClassVar[str] = "conflict"


class ExternalServiceError(DomainError):
    """A dependency outside the application's own data did not respond as
    required — a database, a model provider, an SMTP relay, a connector."""

    code: ClassVar[str] = "external_service_error"


ERROR_CLASSES: tuple[type[DomainError], ...] = (
    DomainError,
    ValidationError,
    AuthorizationError,
    NotFound,
    Conflict,
    ExternalServiceError,
)
