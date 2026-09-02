"""Integration coverage for `T-149`: startup self-checks, the live
scheduler's start/shutdown wired to the process lifespan, and database
pool resilience (`plan.md §5`, `spec §N-06.b`, `C-70`).

Uses a private, file-based SQLite database, the same pattern
`tests/integration/test_scheduler.py` (`T-120`) and
`tests/integration/test_batch_resilience.py` (`T-122`) already established
for self-contained scheduler/ledger coverage on a laptop with no live
PostgreSQL instance. The migration checks reuse `covenant_radar.cli`'s own
`run_migrate_upgrade`, the same real Alembic history every other migration
test runs against, rather than a hand-rolled substitute.
"""

from __future__ import annotations

import io
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from covenant_radar import cli
from covenant_radar.core.clock import FixedClock
from covenant_radar.core.errors import ExternalServiceError
from covenant_radar.db.base import Base
from covenant_radar.db.models.operations import JobRun
from covenant_radar.db.session import (
    CircuitBreakerConfig,
    CircuitOpenError,
    CircuitState,
    DatabaseCircuitBreaker,
    check_database_connection,
)
from covenant_radar.lifecycle import (
    ApplicationLifecycle,
    SelfCheck,
    SelfCheckResult,
    StartupCheckError,
    clock_skew_self_check,
    migrations_at_head_self_check,
    perform_startup,
    run_startup_self_checks,
)
from covenant_radar.scheduler.jobs import (
    InterruptionPolicy,
    JobDefinition,
    JobPolicy,
    JobRegistry,
    JobRunContext,
    RetryPolicy,
)
from covenant_radar.scheduler.ledger import JobLedger
from covenant_radar.scheduler.runner import JobRunner, Scheduler

pytestmark = pytest.mark.integration

_WAIT_SECONDS = 5


def _policy(
    *,
    max_attempts: int = 1,
    backoff_seconds: float = 0.0,
    interruption: InterruptionPolicy = InterruptionPolicy.RESTART,
    timeout_seconds: float = 5.0,
) -> JobPolicy:
    return JobPolicy(
        retry=RetryPolicy(max_attempts=max_attempts, backoff_seconds=backoff_seconds),
        interruption=interruption,
        timeout_seconds=timeout_seconds,
    )


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    database_path = tmp_path / "lifecycle.db"
    test_engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    Base.metadata.create_all(test_engine)
    try:
        yield test_engine
    finally:
        Base.metadata.drop_all(test_engine)
        test_engine.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _database_url(engine: Engine) -> str:
    return engine.url.render_as_string(hide_password=False)


def test_pending_migrations_refuse_start(tmp_path: Path) -> None:
    """`spec §N-06.b`: migrations behind head refuse to start, naming the
    pending revisions — running against an older schema corrupts quietly."""
    database_url = f"sqlite:///{tmp_path / 'migrations.db'}"
    exit_code = cli.run_migrate_upgrade(
        "0002_statements", database_url=database_url, stream=io.StringIO()
    )
    assert exit_code == 0, "test setup: the database must reach the intermediate revision"

    check = migrations_at_head_self_check(database_url)

    with pytest.raises(StartupCheckError) as excinfo:
        run_startup_self_checks([check])

    assert excinfo.value.check_name == "migrations"
    assert "0003_saved_queue_views" in excinfo.value.detail
    assert "0004_notification_read_state" in excinfo.value.detail
    assert "0002_statements" in excinfo.value.detail, "the detail names where the database is, too"

    # Upgrading the rest of the way clears the same check.
    exit_code = cli.run_migrate_upgrade("head", database_url=database_url, stream=io.StringIO())
    assert exit_code == 0
    reports = run_startup_self_checks([migrations_at_head_self_check(database_url)])
    assert reports[0].ok is True


def test_database_blip_opens_and_closes_circuit() -> None:
    """`spec §N-06.b`: a database blip opens the circuit after enough
    consecutive failures, every call fails fast (never even attempting a
    connection) while it is open, and a single successful probe after the
    recovery window closes it again — without a restart."""
    clock = _ManualClock()
    breaker = DatabaseCircuitBreaker(
        config=CircuitBreakerConfig(failure_threshold=2, recovery_timeout_seconds=10.0),
        time_source=clock,
    )

    def failing() -> None:
        raise OperationalError("SELECT 1", {}, Exception("database blip"))

    assert breaker.state is CircuitState.CLOSED

    with pytest.raises(OperationalError):
        breaker.call(failing)
    assert breaker.state is CircuitState.CLOSED, "one failure alone must not trip the breaker"

    with pytest.raises(OperationalError):
        breaker.call(failing)
    assert breaker.state is CircuitState.OPEN, "the second consecutive failure trips it"

    with pytest.raises(CircuitOpenError):
        breaker.call(lambda: pytest.fail("must never be called while the circuit is open"))

    clock.advance(9.9)
    assert breaker.state is CircuitState.OPEN, "the recovery window has not elapsed yet"

    clock.advance(0.2)
    assert breaker.state is CircuitState.HALF_OPEN, "past the window, a single probe is allowed"

    probe_calls: list[str] = []

    def succeeding() -> str:
        probe_calls.append("probed")
        return "ok"

    assert breaker.call(succeeding) == "ok"
    assert probe_calls == ["probed"]
    assert breaker.state is CircuitState.CLOSED, "the successful probe closes it, without a restart"

    # `check_database_connection` — the choke point the startup self-check
    # and `SqlAlchemyUnitOfWork` share — reports through the same breaker.
    flaky_engine = _FlakyEngine(failures=1)
    with pytest.raises(ExternalServiceError):
        check_database_connection(flaky_engine, circuit_breaker=breaker, retry_attempts=1)  # type: ignore[arg-type]
    assert breaker.state is CircuitState.CLOSED, "a single failure below threshold stays closed"


def test_shutdown_records_step_disposition(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    """`spec §N-06.b`: a shutdown signal during a job step lets the step
    finish or cleanly abandons it per policy, with the run ledger recording
    which — driven through `ApplicationLifecycle`, not `JobRunner` directly,
    proving the lifespan wiring itself produces the same disposition."""
    run_ids: dict[str, str] = {}
    finishing_started = threading.Event()
    finishing_release = threading.Event()
    abandoned_started = threading.Event()
    abandoned_release = threading.Event()

    def finishing_handler(context: JobRunContext) -> dict[str, object]:
        run_ids["finishing"] = context.run_id
        finishing_started.set()
        finishing_release.wait(2 * _WAIT_SECONDS)
        return {"ok": True}

    def abandoned_handler(context: JobRunContext) -> dict[str, object]:
        run_ids["abandoned"] = context.run_id
        abandoned_started.set()
        abandoned_release.wait(2 * _WAIT_SECONDS)
        return {"ok": True}

    registry = JobRegistry()
    registry.register(
        JobDefinition(
            name="finishing-step", handler=finishing_handler, policy=_policy(timeout_seconds=30)
        )
    )
    registry.register(
        JobDefinition(
            name="abandoned-step", handler=abandoned_handler, policy=_policy(timeout_seconds=30)
        )
    )
    runner = JobRunner(registry, session_factory)
    scheduler = Scheduler(runner, database_url=_database_url(engine))
    lifecycle = ApplicationLifecycle(
        checks=(), scheduler=scheduler, shutdown_grace_period_seconds=1.0
    )
    lifecycle.startup()

    abandoned_thread = threading.Thread(
        target=runner.run_now, args=("abandoned-step",), kwargs={"trigger": "scheduled"}
    )
    abandoned_thread.start()
    assert abandoned_started.wait(_WAIT_SECONDS), "the step that will be abandoned never started"

    finishing_future = runner.submit("finishing-step", trigger="scheduled")
    assert finishing_started.wait(_WAIT_SECONDS), "the step that will finish never started"

    def _release_finishing_shortly() -> None:
        finishing_release.wait(0.1)
        finishing_release.set()

    threading.Thread(target=_release_finishing_shortly, daemon=True).start()

    report = lifecycle.shutdown()

    finishing_future.result(_WAIT_SECONDS)
    abandoned_release.set()
    abandoned_thread.join(_WAIT_SECONDS)
    assert not abandoned_thread.is_alive()

    assert lifecycle.state.shutdown_report is report
    assert run_ids["finishing"] in report.finished
    assert run_ids["abandoned"] in report.abandoned

    with session_factory() as session:
        finishing_row = session.execute(
            select(JobRun).where(JobRun.run_id == run_ids["finishing"])
        ).scalar_one()
        abandoned_row = session.execute(
            select(JobRun).where(JobRun.run_id == run_ids["abandoned"])
        ).scalar_one()
    assert finishing_row.state == "succeeded", "the ledger records the finished disposition"
    assert abandoned_row.state == "abandoned", "the ledger records the abandoned disposition"


def test_hard_kill_leaves_no_partial_state(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    """`spec §N-06.b`: a hard kill during the overnight batch leaves no
    partial state, and the next start's `ApplicationLifecycle.startup`
    resolves it and lets the retry complete correctly."""
    executed: list[tuple[str, int]] = []

    def handler(context: JobRunContext) -> dict[str, object]:
        executed.append((context.run_id, context.attempt))
        return {"ok": True}

    registry = JobRegistry()
    registry.register(
        JobDefinition(
            name="nightly.batch",
            handler=handler,
            policy=_policy(interruption=InterruptionPolicy.RESUME),
        )
    )

    # Simulate a hard-killed process: a `running` row left behind with
    # nothing ever having finished or failed it.
    crash_time = datetime(2026, 8, 30, 1, 0, tzinfo=UTC)
    with session_factory() as session:
        ledger = JobLedger(session)
        orphaned = ledger.start_or_refuse(
            registry.get("nightly.batch"),
            trigger="scheduled",
            started_at=crash_time,
            request_id="rq-hard-kill-orphan",
        )
        session.commit()
        orphaned_id, orphaned_run_id = orphaned.id, orphaned.run_id

    # A fresh process starts a new runner and scheduler against the same
    # database and reconciles before accepting any new schedule tick.
    runner = JobRunner(registry, session_factory)
    scheduler = Scheduler(runner, database_url=_database_url(engine))
    lifecycle = ApplicationLifecycle(checks=(), scheduler=scheduler)

    state = lifecycle.startup()

    assert len(state.resumed_runs) == 1
    resumed = state.resumed_runs[0]
    assert resumed.state == "succeeded"
    assert resumed.run_id == orphaned_run_id
    assert resumed.attempt == 2, "RESUME continues the same logical run under a fresh attempt"
    assert (orphaned_run_id, 2) in executed

    with session_factory() as session:
        original = session.get(JobRun, orphaned_id)
    assert original is not None
    assert original.state == "interrupted", (
        "the crashed attempt is resolved to a terminal state, never left running: "
        "no partial state survives the restart"
    )

    lifecycle.shutdown()


def test_failed_self_check_exits_non_zero_naming_it(tmp_path: Path) -> None:
    """`spec §N-06.b`: a self-check failing at startup exits non-zero
    naming the check, never starting degraded and silent — proven both for
    an arbitrary failing check and for the real clock-skew check
    (`spec §26`'s "clock skew detected" mitigation)."""
    output = io.StringIO()
    failing_check = SelfCheck("widget", lambda: SelfCheckResult(False, "widget is broken"))

    exit_code = perform_startup([failing_check], stream=output)

    assert exit_code != 0
    assert "widget" in output.getvalue()
    assert "widget is broken" in output.getvalue()

    database_path = tmp_path / "clock-skew.db"
    engine = create_engine(f"sqlite:///{database_path}")
    Base.metadata.create_all(engine)
    try:
        skewed_clock = FixedClock(datetime.now(UTC) + timedelta(hours=2))
        clock_output = io.StringIO()
        clock_check = clock_skew_self_check(engine, clock=skewed_clock, max_skew_seconds=60.0)

        clock_exit_code = perform_startup([clock_check], stream=clock_output)

        assert clock_exit_code != 0
        assert clock_exit_code == exit_code, "every self-check failure returns the same stable code"
        assert "clock_skew" in clock_output.getvalue()
    finally:
        engine.dispose()


class _ManualClock:
    """A monotonic-style clock a test advances explicitly, for
    `DatabaseCircuitBreaker`'s recovery-window arithmetic."""

    def __init__(self, start: float = 0.0) -> None:
        self._value = start

    def __call__(self) -> float:
        return self._value

    def advance(self, delta: float) -> None:
        self._value += delta


class _FlakyConnection:
    def __enter__(self) -> _FlakyConnection:
        return self

    def __exit__(self, *_exc_info: object) -> bool:
        return False

    def execute(self, _statement: object) -> None:
        return None


class _FlakyEngine:
    """A minimal `Engine`-shaped double that fails to connect a fixed
    number of times before succeeding, for deterministic blip simulation
    without depending on OS-level connection timing."""

    def __init__(self, *, failures: int) -> None:
        self._remaining_failures = failures
        self.url = make_url("sqlite:///:memory:")

    def connect(self) -> _FlakyConnection:
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise OperationalError("SELECT 1", {}, Exception("database blip"))
        return _FlakyConnection()
