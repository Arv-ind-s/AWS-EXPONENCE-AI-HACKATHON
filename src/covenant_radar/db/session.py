"""Engine and session factories, and the `UnitOfWork` that opens exactly
one transaction per use case.

Repositories never open a transaction themselves: a service opens the
`UnitOfWork`, does its work through repositories bound to its session, and
either commits once or lets an exception roll everything back
(`plan.md §3.3`). Opening a second `UnitOfWork` while one is already open
means a use case tried to span two transactions — `plan.md §3.3` treats
that as two use cases, not one — so it raises rather than nesting silently.
"""

from __future__ import annotations

import inspect
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Self, TypeVar

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, scoped_session, sessionmaker

from covenant_radar.config.settings import DatabaseSettings
from covenant_radar.core.errors import ExternalServiceError

SessionFactory = Callable[[], Session]
RequestSession = scoped_session[Session]

_T = TypeVar("_T")


def is_database_session(value: object) -> bool:
    """Return whether ``value`` is a real or request-scoped SQLAlchemy session.

    Browser routers are composed once at application startup.  A
    :class:`~sqlalchemy.orm.scoping.scoped_session` lets those long-lived
    service objects resolve the actual SQLAlchemy session for the current
    request, rather than accidentally sharing one mutable transaction across
    users.  Keeping this check in the database boundary avoids treating an
    arbitrary duck-typed object as a session.
    """
    return isinstance(value, Session | scoped_session)


def create_database_engine(settings: DatabaseSettings) -> Engine:
    """Build the pooled, pre-ping engine for `settings.url`.

    SQLite and PostgreSQL take different pool arguments: SQLite's default
    pool for a file database (and its dedicated pool for `:memory:`) does
    not accept `pool_size`/`max_overflow`, so the dialect decides which
    arguments apply rather than the caller having to know.
    """
    url = make_url(settings.url)
    engine_kwargs: dict[str, object] = {"pool_pre_ping": True}

    is_sqlite = url.get_backend_name() == "sqlite"
    database = url.database
    if is_sqlite:
        if database and database != ":memory:":
            Path(database).parent.mkdir(parents=True, exist_ok=True)
        engine_kwargs["connect_args"] = {"timeout": settings.connect_timeout_seconds}
    else:
        engine_kwargs["pool_size"] = settings.pool_size
        engine_kwargs["max_overflow"] = settings.max_overflow
        engine_kwargs["connect_args"] = {"connect_timeout": settings.connect_timeout_seconds}

    engine = create_engine(url, **engine_kwargs)
    if is_sqlite and database and database != ":memory:":
        _configure_sqlite_concurrency(engine, settings)
    return engine


def _configure_sqlite_concurrency(engine: Engine, settings: DatabaseSettings) -> None:
    """Make a file-backed SQLite database survive concurrent writers.

    The scheduler writes the job ledger from its own threads while requests
    read and write on theirs.  Under the default rollback journal every reader
    blocks the writer and `database is locked` surfaces immediately, which is
    what made a nightly run and an operator opening `/admin/jobs` at the same
    moment fail each other.

    WAL lets readers run against a snapshot while one writer commits, and
    `busy_timeout` makes a contending writer wait for the lock instead of
    failing on first contention.  `synchronous=NORMAL` is the documented,
    crash-safe companion setting for WAL.
    """

    from sqlalchemy import event

    busy_timeout_ms = max(1, int(settings.connect_timeout_seconds * 1000))

    @event.listens_for(engine, "connect")
    def _apply_pragmas(dbapi_connection: object, _record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()


class CircuitState(StrEnum):
    """The three states a `DatabaseCircuitBreaker` can be in."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(ExternalServiceError):
    """Raised in place of attempting a database call while the circuit is
    open, so a caller fails fast instead of queuing behind its own
    connection timeout."""


@dataclass(frozen=True, slots=True)
class CircuitBreakerConfig:
    """How many consecutive failures open the circuit, and how long it
    stays open before a single probe is allowed through."""

    failure_threshold: int = 5
    recovery_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1.")
        if self.recovery_timeout_seconds <= 0:
            raise ValueError("recovery_timeout_seconds must be positive.")


class DatabaseCircuitBreaker:
    """Fails fast once the database has shown it is down (`spec §N-06.b`).

    Every consecutive failure is counted; once `config.failure_threshold`
    is reached the circuit opens and every subsequent call is refused
    immediately with `CircuitOpenError`, without even attempting a
    connection, until `config.recovery_timeout_seconds` has elapsed. The
    next call after that window is let through as a single probe
    (`HALF_OPEN`): success closes the circuit and resets the failure
    count; failure reopens it immediately, restarting the recovery clock,
    rather than requiring the full threshold to be hit again.

    Thread-safe: `Scheduler`/`JobRunner` attempts and web request threads
    share one breaker per engine.
    """

    def __init__(
        self,
        *,
        config: CircuitBreakerConfig | None = None,
        time_source: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config or CircuitBreakerConfig()
        self._time_source = time_source
        self._lock = threading.Lock()
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> CircuitState:
        with self._lock:
            return self._resolve_state()

    def call(self, fn: Callable[[], _T]) -> _T:
        """Run `fn`, refusing to call it at all while the circuit is open."""
        with self._lock:
            current = self._resolve_state()
            if current is CircuitState.OPEN:
                raise CircuitOpenError(
                    "Database circuit is open; refusing to attempt a connection."
                )
            # Commit a resolved HALF_OPEN transition so a concurrent caller
            # sees it too, rather than every thread independently deciding
            # the recovery window has elapsed.
            self._state = current

        try:
            result = fn()
        except Exception:
            self._on_failure()
            raise
        else:
            self._on_success()
            return result

    def _resolve_state(self) -> CircuitState:
        """Must be called with `self._lock` held."""
        if self._state is CircuitState.OPEN and self._opened_at is not None:
            elapsed = self._time_source() - self._opened_at
            if elapsed >= self._config.recovery_timeout_seconds:
                return CircuitState.HALF_OPEN
        return self._state

    def _on_success(self) -> None:
        with self._lock:
            self._state = CircuitState.CLOSED
            self._consecutive_failures = 0
            self._opened_at = None

    def _on_failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if (
                self._state is CircuitState.HALF_OPEN
                or self._consecutive_failures >= self._config.failure_threshold
            ):
                self._state = CircuitState.OPEN
                self._opened_at = self._time_source()


def check_database_connection(
    engine: Engine,
    *,
    circuit_breaker: DatabaseCircuitBreaker | None = None,
    retry_attempts: int = 1,
    retry_backoff_seconds: float = 0.05,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Confirm `engine` can open a connection, retrying a transient
    `OperationalError` before giving up, and reporting the outcome to
    `circuit_breaker` when one is supplied.

    The single choke point `SqlAlchemyUnitOfWork`, the startup database
    self-check and the `/ready` probe all share, so a blip trips the same
    breaker regardless of which of them noticed it first.
    """
    if retry_attempts < 1:
        raise ValueError("retry_attempts must be at least 1.")

    def _attempt() -> None:
        last_error: OperationalError | None = None
        for attempt_number in range(1, retry_attempts + 1):
            try:
                with engine.connect() as connection:
                    connection.execute(text("SELECT 1"))
                return
            except OperationalError as error:
                last_error = error
                if attempt_number < retry_attempts:
                    sleep(retry_backoff_seconds * attempt_number)
        raise ExternalServiceError(
            f"Database unreachable: {_engine_target(engine)}"
        ) from last_error

    if circuit_breaker is not None:
        circuit_breaker.call(_attempt)
    else:
        _attempt()


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Build the session factory bound to `engine`.

    `expire_on_commit=False` so an object a service just committed stays
    readable for the rest of the request without a redundant reload;
    `autoflush=False` so a repository's read never triggers a surprise
    write mid-query.
    """
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class SqlAlchemyUnitOfWork:
    """The `UnitOfWork` port (`ports/unit_of_work.py`) backed by one
    SQLAlchemy `Session`.

    Opens on `__enter__`, eagerly establishing the database connection so
    an unreachable database fails at the transaction boundary rather than
    on the first query deep inside a service. Commits once on a clean
    exit, rolls back on any exception, and always closes its session.
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        circuit_breaker: DatabaseCircuitBreaker | None = None,
        retry_attempts: int = 1,
        retry_backoff_seconds: float = 0.05,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._session_factory = session_factory
        self._session: Session | None = None
        self._entered_at: str | None = None
        self._circuit_breaker = circuit_breaker
        self._retry_attempts = retry_attempts
        self._retry_backoff_seconds = retry_backoff_seconds
        self._sleep = sleep

    @property
    def session(self) -> Session:
        """The open session. Raises outside an entered `with` block."""
        if self._session is None:
            raise RuntimeError("UnitOfWork is not open; use it as `with unit_of_work: ...`.")
        return self._session

    def __enter__(self) -> Self:
        if self._session is not None:
            attempted_at = _call_site()
            raise RuntimeError(
                f"UnitOfWork is already open, entered at {self._entered_at}; "
                f"a second entry attempted at {attempted_at} is a nested unit "
                "of work. That is a programming error, not a business "
                "outcome — split the use case into two instead of opening "
                "two transactions."
            )

        entered_at = _call_site()
        session = self._session_factory()
        try:
            self._connect(session)
        except Exception:
            session.close()
            raise

        self._session = session
        self._entered_at = entered_at
        return self

    def _connect(self, session: Session) -> None:
        """Eagerly establish the connection, retrying a transient failure
        and reporting the outcome to `circuit_breaker` when one is
        configured — the same resilience `check_database_connection`
        gives the startup self-check and `/ready`, applied to the session
        this unit of work is about to hand out."""

        def _attempt() -> None:
            last_error: OperationalError | None = None
            for attempt_number in range(1, self._retry_attempts + 1):
                try:
                    session.connection()
                    return
                except OperationalError as error:
                    last_error = error
                    if attempt_number < self._retry_attempts:
                        self._sleep(self._retry_backoff_seconds * attempt_number)
            raise ExternalServiceError(
                f"Database unreachable: {_connection_target(session)}"
            ) from last_error

        if self._circuit_breaker is not None:
            self._circuit_breaker.call(_attempt)
        else:
            _attempt()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        session = self.session
        try:
            if exc_type is None:
                session.commit()
            else:
                session.rollback()
        finally:
            session.close()
            self._session = None
            self._entered_at = None

    def commit(self) -> None:
        self.session.commit()

    def rollback(self) -> None:
        self.session.rollback()


def _call_site() -> str:
    """The file and line of whoever called `__enter__` — two frames up,
    since this helper is always invoked from inside `__enter__` itself."""
    caller = inspect.stack()[2]
    return f"{caller.filename}:{caller.lineno}"


def _connection_target(session: Session) -> str:
    """The host a session is bound to, never the credentials in its URL."""
    bind = session.get_bind()
    url = bind.url if isinstance(bind, Engine) else bind.engine.url
    return _redacted_target(url)


def _engine_target(engine: Engine) -> str:
    """The host an engine is bound to, never the credentials in its URL."""
    return _redacted_target(engine.url)


def _redacted_target(url: URL) -> str:
    if url.host:
        return f"{url.host}:{url.port}" if url.port else url.host
    return url.render_as_string(hide_password=True)


__all__ = [
    "CircuitBreakerConfig",
    "CircuitOpenError",
    "CircuitState",
    "DatabaseCircuitBreaker",
    "RequestSession",
    "SessionFactory",
    "SqlAlchemyUnitOfWork",
    "check_database_connection",
    "create_database_engine",
    "create_session_factory",
    "is_database_session",
]
