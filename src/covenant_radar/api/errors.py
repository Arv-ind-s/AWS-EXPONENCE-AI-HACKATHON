"""The single JSON error envelope for the versioned REST API.

Every error the API returns — a deliberately raised `DomainError`, a raw
`HTTPException`, or FastAPI's own request-validation failure — is reshaped
into one envelope carrying a stable `error` code, a human `message`, and an
optional `field` naming the dotted path of the value at fault, alongside the
request id every response already carries. `DomainError` already models
`code`, `message` and `field` (`core/errors.py`); this module is the one
place that puts them on the wire, the same way, for every route.

`asgi.py` wires the three handlers this module needs at the application
level: `DomainError` and `StarletteHTTPException` already have handlers
there and are updated to call `domain_error_body`/`http_error_body` below;
`request_validation_error_handler` is new and registered for
`RequestValidationError`, since no handler for it existed before this task
and FastAPI's own default has no `field` and no stable `error` code.

`request_validation_error_handler` keeps FastAPI's own `detail` array in the
body alongside the four envelope keys, so a caller that already parses the
pre-existing `{"detail": [...]}` shape is unaffected by this addition.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from covenant_radar.core.errors import DomainError

#: `loc` segments that name the request part rather than the field itself.
_LOC_PREFIXES_TO_DROP: Final[frozenset[str]] = frozenset({"body", "query", "path", "header"})


def error_envelope(
    *,
    code: str,
    message: str,
    field: str | None,
    request_id: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the standing `{error, message, field, request_id}` body.

    `extra` merges additional, handler-specific keys without disturbing the
    four envelope keys every API error carries.
    """
    if not isinstance(code, str) or not code:
        raise ValueError("error_envelope requires a non-empty code.")
    if not isinstance(message, str) or not message:
        raise ValueError("error_envelope requires a non-empty message.")
    if field is not None and not isinstance(field, str):
        raise TypeError("error_envelope field must be text or None.")
    body: dict[str, Any] = {
        "error": code,
        "message": message,
        "field": field,
        "request_id": request_id,
    }
    if extra:
        body.update(extra)
    return body


def domain_error_body(error: DomainError, *, request_id: str) -> dict[str, Any]:
    """Build the envelope for a deliberately raised `DomainError`."""
    return error_envelope(
        code=error.code,
        message=error.message,
        field=error.field,
        request_id=request_id,
    )


def http_error_body(*, status: int, detail: str, request_id: str) -> dict[str, Any]:
    """Build the envelope for a raw `HTTPException`.

    A plain `HTTPException` carries no field concept — only `DomainError`
    and FastAPI's own validation failure do — so `field` is always `None`
    here rather than guessed from the message text.
    """
    return error_envelope(code=f"http_{status}", message=detail, field=None, request_id=request_id)


async def request_validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Reshape FastAPI's own 422 into the standing envelope."""
    errors = exc.errors()
    request_id = _request_id(request)
    field = _field_path(errors[0]["loc"]) if errors else None
    message = errors[0]["msg"] if errors else "Request validation failed."
    body = error_envelope(
        code="validation_error",
        message=message,
        field=field,
        request_id=request_id,
        extra={"detail": _json_safe_errors(errors)},
    )
    return JSONResponse(body, status_code=422)


def _field_path(loc: tuple[Any, ...]) -> str | None:
    parts = [str(part) for part in loc if str(part) not in _LOC_PREFIXES_TO_DROP]
    return ".".join(parts) if parts else None


def _json_safe_errors(errors: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Coerce `loc` tuples and any non-JSON exception context to safe values."""
    safe: list[dict[str, Any]] = []
    for item in errors:
        entry = dict(item)
        entry["loc"] = list(entry.get("loc", ()))
        context = entry.get("ctx")
        if isinstance(context, dict):
            entry["ctx"] = {key: str(value) for key, value in context.items()}
        safe.append(entry)
    return safe


def _request_id(request: Request) -> str:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) and value else "unidentified-request"


__all__ = [
    "domain_error_body",
    "error_envelope",
    "http_error_body",
    "request_validation_error_handler",
]
