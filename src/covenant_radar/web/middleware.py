"""ASGI middleware for request identity, principal resolution and observability."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

import structlog
from opentelemetry import trace
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from covenant_radar.api.deps import resolve_principal
from covenant_radar.core.context import bind_request_id, new_request_id

_LOGGER = structlog.get_logger(__name__)
_REQUEST_HEADER = b"x-request-id"


class RequestContextMiddleware:
    """Bind one request id, principal and trace span around every HTTP request.

    This is a pure ASGI middleware so streaming responses are preserved and the
    request log is emitted exactly once after the response has completed.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        tracer_name: str = "covenant_radar.web",
        exception_responder: Callable[[Request, Exception], Awaitable[Response]] | None = None,
    ) -> None:
        self.app = app
        self.tracer = trace.get_tracer(tracer_name)
        self.exception_responder = exception_responder

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        request_id = new_request_id()
        state = _scope_state(scope)
        state["request_id"] = request_id
        state["principal"] = None
        state["error_logged"] = False
        status_code = 500
        response_started = False
        failed = False
        started = time.perf_counter()

        async def send_with_identity(message: Message) -> None:
            nonlocal response_started, status_code
            if message["type"] == "http.response.start":
                response_started = True
                status_code = int(message.get("status", 500))
                headers = list(message.get("headers", []))
                if not any(name.lower() == _REQUEST_HEADER for name, _ in headers):
                    headers.append((_REQUEST_HEADER, request_id.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        request = Request(scope, receive=receive)
        path = str(scope.get("path", "/"))
        method = str(scope.get("method", "GET"))
        principal_kind: str | None = None
        principal_id: str | None = None
        with (
            bind_request_id(request_id),
            self.tracer.start_as_current_span(
                "http.request",
                attributes={
                    "http.request.method": method,
                    "url.path": path,
                    "covenant.request_id": request_id,
                },
            ) as span,
        ):
            try:
                principal = await resolve_principal(request)
                state["principal"] = principal
                if principal is not None:
                    principal_kind = getattr(getattr(principal, "kind", None), "value", None)
                    principal_id = str(getattr(principal, "id", "")) or None
                span.set_attribute("covenant.principal.kind", principal_kind or "anonymous")
                await self.app(scope, receive, send_with_identity)
            except Exception as error:
                failed = True
                span.record_exception(error)
                span.set_status(trace.Status(trace.StatusCode.ERROR, type(error).__name__))
                if self.exception_responder is not None and not response_started:
                    try:
                        response = await self.exception_responder(request, error)
                        await response(scope, receive, send_with_identity)
                        return
                    except Exception as responder_error:
                        error = responder_error
                # FastAPI's exception middleware normally handles endpoint
                # exceptions before they reach this block.  This branch is for
                # failures in middleware or an ASGI adapter outside FastAPI.
                _LOGGER.error(
                    "request_failed",
                    error_class=type(error).__name__,
                    method=method,
                    path=path,
                    support_reference=request_id,
                    exc_info=(type(error), error, error.__traceback__),
                )
                state["error_logged"] = True
                raise
            finally:
                if not failed and not state.get("error_logged", False):
                    _LOGGER.info(
                        "request_completed",
                        method=method,
                        path=path,
                        status_code=status_code if response_started else 500,
                        duration_ms=round((time.perf_counter() - started) * 1000, 3),
                        principal_kind=principal_kind or "anonymous",
                        principal_id=principal_id,
                    )


def _scope_state(scope: Scope) -> MutableMapping[str, Any]:
    state = scope.setdefault("state", {})
    if not isinstance(state, MutableMapping):
        raise TypeError("ASGI request state must be a mutable mapping.")
    return state


# Explicit aliases keep the middleware contract discoverable for callers that
# refer to the concerns separately while retaining one atomic request lifecycle.
RequestIDMiddleware = RequestContextMiddleware
RequestIdentityMiddleware = RequestContextMiddleware


__all__ = [
    "RequestContextMiddleware",
    "RequestIDMiddleware",
    "RequestIdentityMiddleware",
]
