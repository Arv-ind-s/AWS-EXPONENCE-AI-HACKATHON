"""Process lifecycle: startup self-checks, the live scheduler's start and
graceful shutdown, and the maintenance response a request gets while the
database circuit is open (`spec §N-06.b`, `C-70`).

The process starts only when it can work: `run_startup_self_checks` runs a
named, ordered sequence of checks — configuration, migrations at head, the
database, the document store, the scheduler, the process clock — and
`StartupCheckError` names the first one that fails, so `radarctl serve`
never starts degraded and silent. It stops without losing work:
`ApplicationLifecycle.shutdown` hands off to the already-graceful
`scheduler.runner.Scheduler.shutdown`, which finishes or cleanly abandons
whatever job step was in flight. And it survives a database blip without a
restart: `ApplicationLifecycle.startup` calls `Scheduler.start`, which
resolves every run a hard restart left `running` before accepting a new
tick, and `MaintenanceModeMiddleware` turns a database outage into an
immediate 503 for every request instead of each one queuing behind its own
connection timeout.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, TextIO

from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from covenant_radar.config.settings import Settings
from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.errors import ExternalServiceError
from covenant_radar.db.session import (
    CircuitState,
    DatabaseCircuitBreaker,
    check_database_connection,
)
from covenant_radar.scheduler.jobs import JobRegistry
from covenant_radar.scheduler.runner import Scheduler, ShutdownReport

_DEFAULT_MIGRATIONS_SCRIPT_LOCATION: Final[Path] = (
    Path(__file__).resolve().parent / "db" / "migrations"
)
_DEFAULT_CLOCK_SKEW_TOLERANCE_SECONDS: Final[float] = 300.0
_STARTUP_CHECK_EXIT_CODE: Final[int] = 4
_MAINTENANCE_EXEMPT_PREFIXES: Final[tuple[str, ...]] = (
    "/health",
    "/ready",
    "/version",
    "/metrics",
    "/static",
)


# --------------------------------------------------------------------------
# Startup self-checks
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SelfCheckResult:
    """What one startup probe found, before it is attributed to a name."""

    ok: bool
    detail: str


@dataclass(frozen=True, slots=True)
class SelfCheck:
    """One named startup dependency and the probe that establishes it.

    Unlike `observability.health.NamedCheck`, a failing `SelfCheck` must
    stop the process rather than degrade a `/ready` response: there is no
    such thing as `not_configured` here — every one of these is required
    for the process to do useful work at all.
    """

    name: str
    probe: Callable[[], SelfCheckResult]


@dataclass(frozen=True, slots=True)
class SelfCheckReport:
    """One named check's resolved state, for the startup transcript."""

    name: str
    ok: bool
    detail: str


class StartupCheckError(RuntimeError):
    """Raised by `run_startup_self_checks` naming the first check that
    failed, so the process exits non-zero rather than starting degraded."""

    def __init__(self, check_name: str, detail: str) -> None:
        self.check_name = check_name
        self.detail = detail
        super().__init__(f"Startup self-check {check_name!r} failed: {detail}")


def run_startup_self_checks(checks: Sequence[SelfCheck]) -> tuple[SelfCheckReport, ...]:
    """Run every check in order, raising `StartupCheckError` naming the
    first one that fails or that raises, rather than continuing past it —
    a self-check failure is a reason to refuse to start, not a data point
    to collect alongside others."""
    reports: list[SelfCheckReport] = []
    for check in checks:
        try:
            result = check.probe()
        except Exception as error:  # noqa: BLE001 - a broken probe must still name itself
            raise StartupCheckError(check.name, f"{type(error).__name__}: {error}") from error
        if not isinstance(result, SelfCheckResult):
            raise TypeError(f"Startup self-check {check.name!r} must return a SelfCheckResult.")
        reports.append(SelfCheckReport(check.name, result.ok, result.detail))
        if not result.ok:
            raise StartupCheckError(check.name, result.detail)
    return tuple(reports)


def perform_startup(checks: Sequence[SelfCheck], *, stream: TextIO = sys.stdout) -> int:
    """Run every startup self-check, returning `0` on success or a stable
    non-zero exit code after printing which check failed and why —
    `radarctl serve`'s entry point into this module (`C-70`)."""
    try:
        run_startup_self_checks(checks)
    except StartupCheckError as error:
        stream.write(f"{error}\n")
        return _STARTUP_CHECK_EXIT_CODE
    return 0


def configuration_self_check(settings: Settings) -> SelfCheck:
    """Confirm validated configuration is actually what will be used.

    `config.settings.load_settings` already refuses an invalid value at
    import time, naming the key and line (`C-70`); this check exists so
    "configuration" appears in the same startup transcript as every other
    dependency rather than being invisible because it never fails here.
    """

    def probe() -> SelfCheckResult:
        if not isinstance(settings, Settings):
            return SelfCheckResult(False, "No validated Settings instance is available.")
        return SelfCheckResult(True, "Configuration validated.")

    return SelfCheck("configuration", probe)


def migrations_at_head_self_check(
    database_url: str,
    *,
    script_location: Path | str | None = None,
) -> SelfCheck:
    """Refuse to start against a database behind the migration head,
    naming the pending revisions — running against an older schema
    corrupts quietly rather than loudly."""
    location = Path(script_location) if script_location is not None else (
        _DEFAULT_MIGRATIONS_SCRIPT_LOCATION
    )

    def probe() -> SelfCheckResult:
        script = ScriptDirectory(str(location))
        heads = set(script.get_heads())
        engine = create_engine(database_url)
        try:
            current = _current_revision(engine)
        except OperationalError as error:
            return SelfCheckResult(
                False, f"Database unreachable while checking migrations: {error}"
            )
        finally:
            engine.dispose()

        current_set = {current} if current is not None else set()
        if current_set == heads:
            return SelfCheckResult(True, f"Database is at head revision {current!r}.")

        pending = _pending_revisions(script, current)
        if pending:
            return SelfCheckResult(
                False,
                f"Database is at revision {current!r}; pending migrations: "
                f"{', '.join(pending)}.",
            )
        return SelfCheckResult(
            False,
            f"Database revision {current!r} is not among this migration history's "
            f"heads {sorted(heads)!r}.",
        )

    return SelfCheck("migrations", probe)


def _current_revision(engine: Engine) -> str | None:
    from alembic.runtime.migration import MigrationContext

    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def _pending_revisions(script: ScriptDirectory, current: str | None) -> tuple[str, ...]:
    try:
        revisions = list(script.walk_revisions(base=current or "base", head="heads"))
    except Exception:  # noqa: BLE001 - an unknown current revision is reported, not crashed on
        return ()
    ordered = [
        revision.revision for revision in reversed(revisions) if revision.revision != current
    ]
    return tuple(ordered)


def database_self_check(
    engine: Engine,
    *,
    circuit_breaker: DatabaseCircuitBreaker | None = None,
) -> SelfCheck:
    """Confirm the database is reachable before accepting any traffic."""

    def probe() -> SelfCheckResult:
        try:
            check_database_connection(engine, circuit_breaker=circuit_breaker)
        except ExternalServiceError as error:
            return SelfCheckResult(False, str(error))
        return SelfCheckResult(True, "Database reachable.")

    return SelfCheck("database", probe)


def document_store_self_check(store: object) -> SelfCheck:
    """Confirm the configured document store is actually reachable and
    writable, mirroring `observability.health`'s `/ready` probe but
    treated as fatal here rather than merely reported."""

    def probe() -> SelfCheckResult:
        if store is None:
            return SelfCheckResult(False, "No document store is configured.")
        root = getattr(store, "root", None)
        if root is None:
            # A backend with no on-disk root (e.g. object storage) cannot be
            # probed generically; its own health surfaces on first real use.
            return SelfCheckResult(True, "Document store configured.")
        try:
            path = Path(root)
            reachable = path.is_dir() and os.access(path, os.W_OK)
        except OSError as error:
            return SelfCheckResult(False, f"Document store unreachable: {error}")
        if not reachable:
            return SelfCheckResult(
                False, f"Document store root is not a writable directory: {root}"
            )
        return SelfCheckResult(True, "Document store reachable.")

    return SelfCheck("document_store", probe)


def scheduler_self_check(registry: JobRegistry) -> SelfCheck:
    """Confirm the scheduler has at least the jobs a production deployment
    always registers — an empty registry means composition never wired the
    nightly pipeline up, not that there is simply nothing to run yet."""

    def probe() -> SelfCheckResult:
        names = registry.names()
        if not names:
            return SelfCheckResult(False, "No jobs are registered with the scheduler.")
        return SelfCheckResult(
            True, f"{len(names)} job(s) registered: {', '.join(sorted(names))}."
        )

    return SelfCheck("scheduler", probe)


def clock_skew_self_check(
    engine: Engine,
    *,
    clock: Clock | None = None,
    max_skew_seconds: float = _DEFAULT_CLOCK_SKEW_TOLERANCE_SECONDS,
) -> SelfCheck:
    """Refuse to start when this process's clock disagrees with the
    database's by more than `max_skew_seconds` — an undetected skewed
    clock silently corrupts every effective-dated read and audit
    timestamp this application writes."""
    active_clock = clock or SystemClock()

    def probe() -> SelfCheckResult:
        try:
            with engine.connect() as connection:
                raw = connection.execute(text("SELECT CURRENT_TIMESTAMP")).scalar()
        except SQLAlchemyError as error:
            return SelfCheckResult(False, f"Could not read the database clock: {error}")

        database_time = _coerce_utc(raw)
        if database_time is None:
            return SelfCheckResult(False, f"Database clock reading was not understood: {raw!r}.")

        skew = abs((active_clock.now() - database_time).total_seconds())
        if skew > max_skew_seconds:
            return SelfCheckResult(
                False,
                f"Process clock differs from the database clock by {skew:.1f}s, "
                f"exceeding the {max_skew_seconds:.0f}s tolerance.",
            )
        return SelfCheckResult(True, f"Clock skew {skew:.1f}s is within tolerance.")

    return SelfCheck("clock_skew", probe)


def _coerce_utc(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        candidate = value.strip()
        for date_format in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                # The database's own clock reading is already UTC (SQLite's
                # `CURRENT_TIMESTAMP` and this project's PostgreSQL sessions
                # both are); `%z` is absent from the format because the
                # driver never includes an offset, not because the instant
                # is ambiguous.
                return datetime.strptime(candidate, date_format).replace(tzinfo=UTC)  # noqa: DTZ007
            except ValueError:
                continue
    return None


# --------------------------------------------------------------------------
# Scheduler start/shutdown orchestration
# --------------------------------------------------------------------------


@dataclass(slots=True)
class LifecycleState:
    """The transcript `ApplicationLifecycle` accumulates across the
    process's life, for diagnostics and tests."""

    startup_reports: tuple[SelfCheckReport, ...] = ()
    resumed_runs: tuple[Any, ...] = ()
    shutdown_report: ShutdownReport | None = None


class ApplicationLifecycle:
    """Ties startup self-checks and the live job scheduler to the ASGI
    process lifespan.

    `startup` refuses to proceed past the first failing self-check
    (`StartupCheckError`), then calls `Scheduler.start`, which resolves
    every run a hard restart left `running` (`spec §N-06.b`) before
    accepting a new schedule tick. `shutdown` hands off entirely to
    `Scheduler.shutdown`, which drains in-flight job steps to completion or
    cleanly abandons them within the grace period — this class adds no
    shutdown behaviour of its own, only wires the existing one in.
    """

    def __init__(
        self,
        *,
        checks: Sequence[SelfCheck],
        scheduler: Scheduler,
        shutdown_grace_period_seconds: float = 30.0,
    ) -> None:
        self._checks = tuple(checks)
        self._scheduler = scheduler
        self._shutdown_grace_period_seconds = shutdown_grace_period_seconds
        self.state = LifecycleState()

    def startup(self) -> LifecycleState:
        self.state.startup_reports = run_startup_self_checks(self._checks)
        self.state.resumed_runs = self._scheduler.start()
        return self.state

    def shutdown(self) -> ShutdownReport:
        report = self._scheduler.shutdown(grace_period_seconds=self._shutdown_grace_period_seconds)
        self.state.shutdown_report = report
        return report


def install_lifecycle(app: Any, lifecycle: ApplicationLifecycle) -> None:
    """Register `lifecycle` on `app`'s ASGI startup and shutdown events."""

    async def _startup() -> None:
        lifecycle.startup()

    async def _shutdown() -> None:
        lifecycle.shutdown()

    app.router.on_event("startup")(_startup)
    app.router.on_event("shutdown")(_shutdown)
    app.state.lifecycle = lifecycle


# --------------------------------------------------------------------------
# The maintenance response
# --------------------------------------------------------------------------


class MaintenanceModeMiddleware(BaseHTTPMiddleware):
    """Fail every database-backed request fast with a 503 maintenance
    response while `circuit_breaker` is open, instead of letting each one
    queue up behind its own connection attempt and timeout.

    `/health`, `/ready`, `/version`, `/metrics` and `/static` are exempt:
    a load balancer must still be able to see *why* the process is not
    ready, and a liveness probe must never depend on the database at all.
    """

    def __init__(self, app: Any, *, circuit_breaker: DatabaseCircuitBreaker) -> None:
        super().__init__(app)
        self._circuit_breaker = circuit_breaker

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path.startswith(_MAINTENANCE_EXEMPT_PREFIXES):
            return await call_next(request)
        if self._circuit_breaker.state is CircuitState.OPEN:
            return JSONResponse(
                {
                    "detail": "The service is temporarily unavailable for maintenance.",
                    "request_id": getattr(request.state, "request_id", None),
                },
                status_code=503,
            )
        return await call_next(request)


__all__ = [
    "ApplicationLifecycle",
    "LifecycleState",
    "MaintenanceModeMiddleware",
    "SelfCheck",
    "SelfCheckReport",
    "SelfCheckResult",
    "StartupCheckError",
    "clock_skew_self_check",
    "configuration_self_check",
    "database_self_check",
    "document_store_self_check",
    "install_lifecycle",
    "migrations_at_head_self_check",
    "perform_startup",
    "run_startup_self_checks",
    "scheduler_self_check",
]
