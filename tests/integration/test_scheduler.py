"""Integration coverage for `T-120`: the job registry, database-backed run
ledger, per-job concurrency lock, restart resumption and graceful shutdown
(`plan.md §5.9`, `spec §R-28`).

Uses a private, file-based SQLite database (real background threads need
more than one connection) rather than the `COVENANT_RADAR_DATABASE_URL`
PostgreSQL fixture, matching the pattern other tasks already established
for self-contained scheduler/ledger coverage on a laptop with no live
PostgreSQL instance.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from covenant_radar.db.base import Base
from covenant_radar.db.models.operations import JobRun
from covenant_radar.scheduler.jobs import (
    InterruptionPolicy,
    JobDefinition,
    JobPolicy,
    JobRegistrationError,
    JobRegistry,
    JobRunContext,
    RetryPolicy,
)
from covenant_radar.scheduler.ledger import JobAlreadyRunningError, JobLedger
from covenant_radar.scheduler.runner import JobRunner

pytestmark = pytest.mark.integration

_WAIT_SECONDS = 5


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    database_path = tmp_path / "scheduler.db"
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


def _job_runs(session_factory: sessionmaker[Session], job_name: str) -> list[JobRun]:
    with session_factory() as session:
        statement = select(JobRun).where(JobRun.job_name == job_name).order_by(JobRun.attempt)
        return list(session.execute(statement).scalars().all())


def test_job_without_policy_refused_at_registration() -> None:
    registry = JobRegistry()
    definition = JobDefinition(name="unpolicied", handler=lambda ctx: None, policy=None)

    with pytest.raises(JobRegistrationError, match="policy"):
        registry.register(definition)

    assert "unpolicied" not in registry


def test_failure_recorded_with_error_and_retried(
    session_factory: sessionmaker[Session],
) -> None:
    attempts_seen: list[int] = []

    def handler(context: JobRunContext) -> dict[str, object]:
        attempts_seen.append(context.attempt)
        if context.attempt == 1:
            raise RuntimeError("simulated ingestion failure")
        return {"rows_processed": 3}

    registry = JobRegistry()
    registry.register(
        JobDefinition(
            name="flaky-ingest",
            handler=handler,
            policy=_policy(max_attempts=2, backoff_seconds=0),
        )
    )
    runner = JobRunner(registry, session_factory)

    final_run = runner.run_now("flaky-ingest", trigger="manual")

    assert attempts_seen == [1, 2]
    assert final_run.state == "succeeded"
    assert final_run.attempt == 2
    assert final_run.metrics == {"rows_processed": 3}

    rows = _job_runs(session_factory, "flaky-ingest")
    assert [row.state for row in rows] == ["failed", "succeeded"]
    assert rows[0].attempt == 1
    assert rows[0].error is not None
    assert "simulated ingestion failure" in rows[0].error
    assert rows[1].run_id == rows[0].run_id, "a retry continues the same logical run"


def test_manual_trigger_refused_while_running(
    session_factory: sessionmaker[Session],
) -> None:
    started = threading.Event()
    release = threading.Event()

    def handler(context: JobRunContext) -> dict[str, object]:
        started.set()
        release.wait(_WAIT_SECONDS)
        return {"ok": True}

    registry = JobRegistry()
    registry.register(JobDefinition(name="nightly", handler=handler, policy=_policy()))
    runner = JobRunner(registry, session_factory)

    scheduled_thread = threading.Thread(
        target=runner.run_now, args=("nightly",), kwargs={"trigger": "scheduled"}
    )
    scheduled_thread.start()
    assert started.wait(_WAIT_SECONDS), "the scheduled attempt never started"

    with pytest.raises(JobAlreadyRunningError) as excinfo:
        runner.run_now("nightly", trigger="manual")
    assert "nightly" in str(excinfo.value)
    assert "already running" in str(excinfo.value)

    release.set()
    scheduled_thread.join(_WAIT_SECONDS)
    assert not scheduled_thread.is_alive()

    rows = _job_runs(session_factory, "nightly")
    assert len(rows) == 1, "the refused manual trigger must not have written a second row"
    assert rows[0].state == "succeeded"
    assert rows[0].trigger == "scheduled"


def test_second_runner_idles_with_reason(session_factory: sessionmaker[Session]) -> None:
    started = threading.Event()
    release = threading.Event()

    def handler(context: JobRunContext) -> dict[str, object]:
        started.set()
        release.wait(_WAIT_SECONDS)
        return {"ok": True}

    def make_registry() -> JobRegistry:
        registry = JobRegistry()
        registry.register(JobDefinition(name="nightly", handler=handler, policy=_policy()))
        return registry

    # Two independent runners sharing one database, simulating two
    # scheduler processes started against it.
    scheduler_a = JobRunner(make_registry(), session_factory)
    scheduler_b = JobRunner(make_registry(), session_factory)

    thread_a = threading.Thread(
        target=scheduler_a.run_now, args=("nightly",), kwargs={"trigger": "scheduled"}
    )
    thread_a.start()
    assert started.wait(_WAIT_SECONDS), "scheduler A never started its attempt"

    with pytest.raises(JobAlreadyRunningError) as excinfo:
        scheduler_b.run_now("nightly", trigger="scheduled")
    reason = str(excinfo.value)
    assert "nightly" in reason
    assert "already running" in reason
    assert "started" in reason

    release.set()
    thread_a.join(_WAIT_SECONDS)
    assert not thread_a.is_alive()

    rows = _job_runs(session_factory, "nightly")
    assert len(rows) == 1, "the idled second scheduler must not have written a run of its own"
    assert rows[0].state == "succeeded"


def test_restart_resumes_or_restarts_per_policy(
    session_factory: sessionmaker[Session],
) -> None:
    executed: list[tuple[str, str, int]] = []

    def make_handler(job_name: str):
        def handler(context: JobRunContext) -> dict[str, object]:
            executed.append((job_name, context.run_id, context.attempt))
            return {"ok": True}

        return handler

    registry = JobRegistry()
    registry.register(
        JobDefinition(
            name="resume-job",
            handler=make_handler("resume-job"),
            policy=_policy(interruption=InterruptionPolicy.RESUME),
        )
    )
    registry.register(
        JobDefinition(
            name="restart-job",
            handler=make_handler("restart-job"),
            policy=_policy(interruption=InterruptionPolicy.RESTART),
        )
    )

    # Simulate a hard-killed process: two `running` rows left behind, with
    # nothing ever having called `run_now` to finish or fail them.
    crash_time = datetime(2026, 8, 30, 1, 0, tzinfo=UTC)
    with session_factory() as session:
        ledger = JobLedger(session)
        orphaned_resume = ledger.start_or_refuse(
            registry.get("resume-job"),
            trigger="scheduled",
            started_at=crash_time,
            request_id="rq-orphan-resume",
        )
        orphaned_restart = ledger.start_or_refuse(
            registry.get("restart-job"),
            trigger="scheduled",
            started_at=crash_time,
            request_id="rq-orphan-restart",
        )
        session.commit()
        orphaned_resume_id, orphaned_resume_run_id = orphaned_resume.id, orphaned_resume.run_id
        orphaned_restart_id, orphaned_restart_run_id = (
            orphaned_restart.id,
            orphaned_restart.run_id,
        )

    # A fresh process, with the same job code registered, starts a new
    # runner against the same database and reconciles before accepting any
    # new schedule tick.
    runner = JobRunner(registry, session_factory)
    resumed_runs = runner.reconcile()

    by_name = {run.job_name: run for run in resumed_runs}
    assert set(by_name) == {"resume-job", "restart-job"}

    assert by_name["resume-job"].state == "succeeded"
    assert by_name["resume-job"].run_id == orphaned_resume_run_id
    assert by_name["resume-job"].attempt == 2

    assert by_name["restart-job"].state == "succeeded"
    assert by_name["restart-job"].run_id != orphaned_restart_run_id
    assert by_name["restart-job"].attempt == 1

    with session_factory() as session:
        original_resume = session.get(JobRun, orphaned_resume_id)
        original_restart = session.get(JobRun, orphaned_restart_id)
    assert original_resume is not None and original_resume.state == "interrupted"
    assert original_restart is not None and original_restart.state == "interrupted"

    assert ("resume-job", orphaned_resume_run_id, 2) in executed
    assert any(
        job_name == "restart-job" and run_id != orphaned_restart_run_id and attempt == 1
        for job_name, run_id, attempt in executed
    )


def test_graceful_shutdown_finishes_or_abandons_cleanly(
    session_factory: sessionmaker[Session],
) -> None:
    run_ids: dict[str, str] = {}
    slow_started = threading.Event()
    slow_release = threading.Event()
    quick_started = threading.Event()
    quick_release = threading.Event()

    def slow_handler(context: JobRunContext) -> dict[str, object]:
        run_ids["slow"] = context.run_id
        slow_started.set()
        slow_release.wait(2 * _WAIT_SECONDS)
        return {"ok": True}

    def quick_handler(context: JobRunContext) -> dict[str, object]:
        run_ids["quick"] = context.run_id
        quick_started.set()
        quick_release.wait(2 * _WAIT_SECONDS)
        return {"ok": True}

    registry = JobRegistry()
    registry.register(
        JobDefinition(name="slow-step", handler=slow_handler, policy=_policy(timeout_seconds=30))
    )
    registry.register(
        JobDefinition(name="quick-step", handler=quick_handler, policy=_policy(timeout_seconds=30))
    )
    runner = JobRunner(registry, session_factory)

    slow_thread = threading.Thread(
        target=runner.run_now, args=("slow-step",), kwargs={"trigger": "scheduled"}
    )
    slow_thread.start()
    assert slow_started.wait(_WAIT_SECONDS), "the slow step never started"

    quick_future = runner.submit("quick-step", trigger="scheduled")
    assert quick_started.wait(_WAIT_SECONDS), "the quick step never started"

    def _release_quick_shortly() -> None:
        time.sleep(0.1)
        quick_release.set()

    threading.Thread(target=_release_quick_shortly, daemon=True).start()

    # `slow-step` is never released before the grace period elapses, so
    # `shutdown` must abandon it; `quick-step` finishes on its own while
    # `shutdown` is still busy waiting on `slow-step`, so the same call
    # also demonstrates the "finishes" branch.
    report = runner.shutdown(grace_period_seconds=1.0)

    quick_future.result(_WAIT_SECONDS)
    slow_release.set()
    slow_thread.join(_WAIT_SECONDS)
    assert not slow_thread.is_alive()

    assert run_ids["quick"] in report.finished
    assert run_ids["slow"] in report.abandoned

    with session_factory() as session:
        quick_row = session.execute(
            select(JobRun).where(JobRun.run_id == run_ids["quick"])
        ).scalar_one()
        slow_row = session.execute(
            select(JobRun).where(JobRun.run_id == run_ids["slow"])
        ).scalar_one()
    assert quick_row.state == "succeeded"
    assert slow_row.state == "abandoned"

    # The slow handler's own late completion (released only after
    # `shutdown` returned) must never overwrite the terminal state
    # `shutdown` already recorded.
    with session_factory() as session:
        slow_row_after_release = session.execute(
            select(JobRun).where(JobRun.run_id == run_ids["slow"])
        ).scalar_one()
    assert slow_row_after_release.state == "abandoned"

    with pytest.raises(Exception):  # noqa: B017, PT011 - the runner refuses new triggers post-shutdown
        runner.run_now("quick-step", trigger="manual")
