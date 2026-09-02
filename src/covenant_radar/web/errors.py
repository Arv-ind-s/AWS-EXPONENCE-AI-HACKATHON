"""HTTP and presentation mappings for the standing domain error hierarchy."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

import structlog
from fastapi import Request

from covenant_radar.core.context import get_request_id
from covenant_radar.core.errors import (
    AuthorizationError,
    Conflict,
    DomainError,
    ExternalServiceError,
    NotFound,
    ValidationError,
)

_LOGGER = structlog.get_logger(__name__)

_STATUS_BY_ERROR: Final[tuple[tuple[type[DomainError], int], ...]] = (
    (ValidationError, 422),
    (AuthorizationError, 403),
    (NotFound, 404),
    (Conflict, 409),
    (ExternalServiceError, 503),
    (DomainError, 500),
)
_UI_KEY_BY_STATUS: Final[Mapping[int, str]] = {
    400: "error.400.title",
    401: "error.401.title",
    403: "error.403.title",
    404: "error.404.title",
    409: "error.409.title",
    422: "error.422.title",
    500: "error.500.title",
    503: "error.503.title",
}


def status_for_error(error: DomainError) -> int:
    """Map a domain error to its single canonical HTTP status."""
    for error_type, status in _STATUS_BY_ERROR:
        if isinstance(error, error_type):
            return status
    return 500


def ui_key_for_status(status: int) -> str:
    """Return the catalogue key for a status displayed in a shell error."""
    return _UI_KEY_BY_STATUS.get(status, "error.500.title")


def ui_state_for_error(error: DomainError) -> str:
    """Map a domain error to the designed error-state family."""
    status = status_for_error(error)
    return "not_found" if status == 404 else "error"


def support_reference(request_id: str | None) -> str:
    """Return a non-secret, searchable support reference for an incident."""
    return request_id or get_request_id() or "unidentified-request"


def log_unhandled_exception(request: Request, error: Exception) -> str:
    """Write the one server-side error record and mark the request as logged."""
    reference = support_reference(getattr(request.state, "request_id", None))
    request.state.error_logged = True
    _LOGGER.error(
        "request_failed",
        error_class=type(error).__name__,
        path=request.url.path,
        method=request.method,
        support_reference=reference,
        exc_info=(type(error), error, error.__traceback__),
    )
    return reference


# Stable aliases for adapters that name the mapping by its target protocol.
http_status_for_error = status_for_error
error_status = status_for_error


__all__ = [
    "log_unhandled_exception",
    "error_status",
    "http_status_for_error",
    "status_for_error",
    "support_reference",
    "ui_key_for_status",
    "ui_state_for_error",
]
