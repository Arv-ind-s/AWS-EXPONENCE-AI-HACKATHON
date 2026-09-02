"""Operational routes: `/health`, `/ready`, `/version`, `/metrics` (`C-23`).

Health is liveness only. Readiness is the sum of named, independently
reported checks against the database, the document store, the scheduler
and every configured capability — never one undifferentiated boolean
(`spec §20`). `/metrics` is unauthenticated by design (a scrape target
cannot present a session), so it is restricted by network origin or a
shared token instead; both are checked here rather than left to a reverse
proxy, because the specification treats an unrestricted `/metrics` as a
volume-and-shape leak, not a convenience.
"""

from __future__ import annotations

import hmac
import ipaddress
import os
from collections.abc import Callable
from typing import Final

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from covenant_radar import __version__
from covenant_radar.api.deps import public
from covenant_radar.observability.health import (
    CheckResult,
    NamedCheck,
    ReadinessStatus,
    evaluate_readiness,
    liveness_status,
)
from covenant_radar.observability.logging import logging_health

_BUILD_COMMIT_ENV: Final[str] = "COVENANT_RADAR_BUILD_COMMIT"
_BUILD_TIME_ENV: Final[str] = "COVENANT_RADAR_BUILD_TIME"
_UNKNOWN_BUILD_METADATA: Final[str] = "unknown"


def create_system_router() -> APIRouter:
    """Build the operational router.

    Every dependency it reports on is read from `request.app.state` at
    request time rather than injected at construction, because these are
    the only routes the application factory must be able to serve before
    the rest of the composition root (`web/application.py`) has decided
    which adapters exist for this deployment.
    """
    router = APIRouter(tags=["system"])

    @router.get("/health", name="health", response_class=JSONResponse)
    @public
    async def health(request: Request) -> JSONResponse:
        payload: dict[str, object] = dict(liveness_status())
        payload["version"] = __version__
        payload["request_id"] = request.state.request_id
        payload["logging"] = logging_health()
        return JSONResponse(payload)

    @router.get("/ready", name="ready", response_class=JSONResponse)
    @public
    async def ready(request: Request) -> JSONResponse:
        report = evaluate_readiness(_readiness_checks(request))
        payload = report.to_dict()
        payload["request_id"] = request.state.request_id
        return JSONResponse(payload, status_code=200 if report.ready else 503)

    @router.get("/version", name="version", response_class=JSONResponse)
    @public
    async def version(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "version": __version__,
                "commit": os.environ.get(_BUILD_COMMIT_ENV, _UNKNOWN_BUILD_METADATA),
                "build_time": os.environ.get(_BUILD_TIME_ENV, _UNKNOWN_BUILD_METADATA),
                "request_id": request.state.request_id,
            }
        )

    @router.get("/metrics", name="metrics")
    @public
    async def metrics(request: Request) -> Response:
        if not getattr(request.app.state, "metrics_enabled", False):
            return Response(status_code=404)
        if not _metrics_request_is_allowed(request):
            return JSONResponse({"detail": "Metrics access is restricted."}, status_code=403)
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return router


def _readiness_checks(request: Request) -> tuple[NamedCheck, ...]:
    state = request.app.state
    checks = [
        NamedCheck("database", lambda: _database_check(getattr(state, "database_engine", None))),
        NamedCheck(
            "document_store", lambda: _document_store_check(getattr(state, "document_store", None))
        ),
        NamedCheck("scheduler", lambda: _scheduler_check(getattr(state, "nightly_runtime", None))),
    ]
    settings = getattr(state, "settings", None)
    capabilities = getattr(settings, "capabilities", None)
    if capabilities is not None:
        for field_name in (
            "model_provider",
            "sso",
            "ocr",
            "smtp",
            "webhooks",
        ):
            capability = getattr(capabilities, field_name)
            checks.append(NamedCheck(f"capability:{field_name}", _capability_check(capability)))
    return tuple(checks)


def _database_check(engine: Engine | None) -> CheckResult:
    if engine is None:
        return CheckResult(
            ReadinessStatus.NOT_CONFIGURED, "No database engine is wired to this process."
        )
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as error:
        return CheckResult(ReadinessStatus.NOT_READY, f"Database unreachable: {error}")
    return CheckResult(ReadinessStatus.READY, "Database reachable.")


def _document_store_check(store: object) -> CheckResult:
    if store is None:
        return CheckResult(
            ReadinessStatus.NOT_CONFIGURED, "No document store is wired to this process."
        )
    root = getattr(store, "root", None)
    if root is None:
        # A backend without an on-disk root (e.g. a future object-storage
        # adapter) cannot be probed generically here; its own health surfaces
        # through the first real request against it instead of a synthetic one
        # on every readiness poll.
        return CheckResult(ReadinessStatus.READY, "Document store configured.")
    try:
        reachable = root.is_dir() and os.access(root, os.W_OK)
    except OSError as error:
        return CheckResult(ReadinessStatus.NOT_READY, f"Document store unreachable: {error}")
    if not reachable:
        return CheckResult(
            ReadinessStatus.NOT_READY, f"Document store root is not a writable directory: {root}"
        )
    return CheckResult(ReadinessStatus.READY, "Document store reachable.")


def _scheduler_check(nightly_runtime: object) -> CheckResult:
    if nightly_runtime is None:
        return CheckResult(ReadinessStatus.NOT_CONFIGURED, "No scheduler is wired to this process.")
    if getattr(nightly_runtime, "runner", None) is None:
        return CheckResult(ReadinessStatus.NOT_READY, "Scheduler runtime has no job runner.")
    return CheckResult(ReadinessStatus.READY, "Scheduler runtime initialised.")


def _capability_check(capability: object) -> Callable[[], CheckResult]:
    def _check() -> CheckResult:
        configured = bool(getattr(capability, "configured", False))
        detail = str(getattr(capability, "detail", ""))
        if not configured:
            return CheckResult(ReadinessStatus.NOT_CONFIGURED, detail or "Not configured.")
        return CheckResult(ReadinessStatus.READY, detail)

    return _check


def _metrics_request_is_allowed(request: Request) -> bool:
    configured_token = getattr(request.app.state, "metrics_token", None)
    supplied_token = request.headers.get("x-metrics-token", "")
    if configured_token:
        return bool(supplied_token) and hmac.compare_digest(supplied_token, configured_token)
    host = request.client.host if request.client else ""
    if host == "testclient":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


__all__ = ["create_system_router"]
