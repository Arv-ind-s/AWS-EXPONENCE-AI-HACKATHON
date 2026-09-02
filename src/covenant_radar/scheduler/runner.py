"""The job runner: executes one attempt, applies retry and timeout policy,
resolves restart interruption, and shuts down without leaving a run
ambiguous (`plan.md §5.9`, `spec §R-28`).

`JobRunner` is the synchronous execution core — `run_now` starts, runs and
finalises exactly one logical trigger (recursing for a declared retry) and
is what both `radarctl job run` and a fired schedule tick call. `Scheduler`
is the thin live-scheduling loop on top of it: APScheduler with a
SQLAlchemy job store (`spec §13`'s ADR) decides *when* a job is due across
a restart; `JobRunner` decides, and durably records, what happened when it
ran.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final
from uuid import UUID

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import bind_job_run_id, bind_request_id, new_request_id
from covenant_radar.core.errors import Conflict
from covenant_radar.db.models.operations import JobRun
from covenant_radar.db.session import SessionFactory
from covenant_radar.scheduler.jobs import (
    InterruptionPolicy,
    JobDefinition,
    JobRegistry,
    JobRunContext,
)
from covenant_radar.scheduler.ledger import FAILED, JobLedger

_APSCHEDULER_TABLE_NAME: Final = "apscheduler_job"


class SchedulerShuttingDownError(Conflict):
    """Raised when a trigger is attempted after `JobRunner.shutdown` began."""

    def __init__(self, job_name: str) -> None:
        self.job_name = job_name
        super().__init__(
            f"Job {job_name!r} was not started: the runner is shutting down "
            "and refuses new triggers."
        )


@dataclass(frozen=True, slots=True)
class ShutdownReport:
    """Which in-flight runs a graceful shutdown finished versus abandoned."""

    finished: tuple[str, ...]
    abandoned: tuple[str, ...]


@dataclass(slots=True)
class _InFlight:
    """One attempt currently executing on a worker thread."""

    job_name: str
    run_id: str
    attempt: int
    done: threading.Event = field(default_factory=threading.Event)


class JobRunner:
    """Starts, executes, retries and finalises one job attempt at a time
    per job name, backed by the database-resident `job_run` ledger."""

    def __init__(
        self,
        registry: JobRegistry,
        session_factory: SessionFactory,
        *,
        clock: Clock | None = None,
        sleep: Callable[[float], None] = time.sleep,
        request_id_factory: Callable[[], str] = new_request_id,
        max_workers: int = 8,
    ) -> None:
        self.registry = registry
        self.session_factory = session_factory
        self.clock = clock or SystemClock()
        self.sleep = sleep
        self._request_id_factory = request_id_factory
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="job-runner"
        )
        self._process_locks: dict[str, threading.Lock] = {}
        self._process_locks_guard = threading.Lock()
        self._inflight: dict[UUID, _InFlight] = {}
        self._inflight_guard = threading.Lock()
        self._shutting_down = False

    def run_now(
        self,
        job_name: str,
        *,
        trigger: str,
        run_id: str | None = None,
        attempt: int = 1,
        as_of: str | None = None,
        borrower_id: str | None = None,
        actor_id: UUID | None = None,
    ) -> JobRun:
        """Start, run and finalise one attempt of `job_name`, recursing for
        a declared retry on failure. Blocks until that attempt (and any of
        its retries) finishes; raises `JobAlreadyRunningError` if the job
        is already running and `SchedulerShuttingDownError` mid-shutdown."""
        if self._shutting_down:
            raise SchedulerShuttingDownError(job_name)
        definition = self.registry.get(job_name)
        policy = definition.policy
        if policy is None:  # pragma: no cover - the registry refuses this at registration
            raise RuntimeError(f"job {job_name!r} has no policy despite passing registration.")

        with self._process_lock(job_name):
            run_pk, resolved_run_id, request_id = self._start(
                definition,
                trigger=trigger,
                run_id=run_id,
                attempt=attempt,
                actor_id=actor_id,
            )

        outcome = self._execute(
            definition,
            run_pk,
            resolved_run_id,
            attempt,
            trigger,
            request_id,
            as_of=as_of,
            borrower_id=borrower_id,
        )

        if outcome.state == FAILED and attempt < policy.retry.max_attempts:
            if policy.retry.backoff_seconds:
                self.sleep(policy.retry.backoff_seconds)
            return self.run_now(
                job_name,
                trigger="retry",
                run_id=resolved_run_id,
                attempt=attempt + 1,
                as_of=as_of,
                borrower_id=borrower_id,
                actor_id=actor_id,
            )
        return outcome

    def reconcile(self) -> tuple[JobRun, ...]:
        """Resolve every run a hard restart left `running`, per its job's
        declared interruption policy. Call before accepting any new
        schedule tick, so a restart never leaves a run ambiguous."""
        now = self._now()
        session = self.session_factory()
        to_restart: list[tuple[JobDefinition, JobRun]] = []
        try:
            ledger = JobLedger(session)
            for orphaned in ledger.running_runs():
                interrupted = ledger.interrupt(orphaned.id, finished_at=now)
                if interrupted is None:
                    continue
                if orphaned.job_name in self.registry:
                    to_restart.append((self.registry.get(orphaned.job_name), interrupted))
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

        resumed: list[JobRun] = []
        for definition, interrupted_run in to_restart:
            if definition.policy is None:  # pragma: no cover - registry forbids this
                continue
            if definition.policy.interruption is InterruptionPolicy.RESUME:
                resumed.append(
                    self.run_now(
                        definition.name,
                        trigger="restart-resume",
                        run_id=interrupted_run.run_id,
                        attempt=interrupted_run.attempt + 1,
                    )
                )
            else:
                resumed.append(self.run_now(definition.name, trigger="restart-fresh"))
        return tuple(resumed)

    def shutdown(self, *, grace_period_seconds: float = 30.0) -> ShutdownReport:
        """Stop accepting new triggers; wait up to `grace_period_seconds`
        for in-flight runs to finish, then cleanly abandon any still
        running rather than leaving them `running` forever."""
        self._shutting_down = True
        with self._inflight_guard:
            snapshot = dict(self._inflight)

        deadline = time.monotonic() + grace_period_seconds
        finished: list[str] = []
        abandoned: list[str] = []
        for run_pk, info in snapshot.items():
            # `Event.wait(0)` is a non-blocking poll rather than a no-op, so
            # a run that already finished while this loop was waiting on an
            # earlier one is still correctly reported as finished even once
            # the shared deadline has passed.
            remaining = max(deadline - time.monotonic(), 0.0)
            if info.done.wait(remaining):
                finished.append(info.run_id)
                continue
            self._finalize_abandoned(run_pk)
            abandoned.append(info.run_id)

        self._executor.shutdown(wait=False, cancel_futures=True)
        return ShutdownReport(finished=tuple(finished), abandoned=tuple(abandoned))

    def submit(self, job_name: str, *, trigger: str, **kwargs: object) -> Future[JobRun]:
        """Run `job_name` on a worker thread instead of the caller's own,
        for the live scheduler's fired ticks. Returns a `Future`."""
        return self._executor.submit(self.run_now, job_name, trigger=trigger, **kwargs)  # type: ignore[arg-type]

    def _start(
        self,
        definition: JobDefinition,
        *,
        trigger: str,
        run_id: str | None,
        attempt: int,
        actor_id: UUID | None,
    ) -> tuple[UUID, str, str]:
        session = self.session_factory()
        try:
            ledger = JobLedger(session)
            request_id = self._request_id_factory()
            run = ledger.start_or_refuse(
                definition,
                trigger=trigger,
                started_at=self._now(),
                request_id=request_id,
                run_id=run_id,
                attempt=attempt,
                created_by_id=actor_id,
            )
            run_pk, resolved_run_id = run.id, run.run_id
            session.commit()
            return run_pk, resolved_run_id, request_id
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _execute(
        self,
        definition: JobDefinition,
        run_pk: UUID,
        run_id: str,
        attempt: int,
        trigger: str,
        request_id: str,
        *,
        as_of: str | None,
        borrower_id: str | None,
    ) -> JobRun:
        policy = definition.policy
        assert policy is not None  # the registry refuses an unpolicied job
        context = JobRunContext(
            run_id=run_id,
            attempt=attempt,
            trigger=trigger,
            request_id=request_id,
            as_of=as_of,
            borrower_id=borrower_id,
        )
        info = _InFlight(job_name=definition.name, run_id=run_id, attempt=attempt)
        outcome_box: dict[str, JobRun] = {}

        def _work() -> None:
            try:
                with bind_job_run_id(run_id), bind_request_id(request_id):
                    metrics = definition.handler(context)
                outcome_box["run"] = self._finalize_success(run_pk, metrics)
            except BaseException as error:  # noqa: BLE001 - reported through the ledger, not swallowed
                outcome_box["run"] = self._finalize_failure(run_pk, error)
            finally:
                info.done.set()
                with self._inflight_guard:
                    self._inflight.pop(run_pk, None)

        with self._inflight_guard:
            self._inflight[run_pk] = info

        worker = threading.Thread(target=_work, name=f"job-{definition.name}-{run_id}", daemon=True)
        worker.start()

        if info.done.wait(policy.timeout_seconds):
            return outcome_box["run"]
        return self._finalize_timeout(definition, run_pk, policy.timeout_seconds)

    def _finalize_success(self, run_pk: UUID, metrics: object) -> JobRun:
        payload = dict(metrics) if isinstance(metrics, dict) else None
        return self._apply_terminal(
            run_pk, lambda ledger, now: ledger.succeed(run_pk, finished_at=now, metrics=payload)
        )

    def _finalize_failure(self, run_pk: UUID, error: BaseException) -> JobRun:
        message = f"{type(error).__name__}: {error}"
        return self._apply_terminal(
            run_pk, lambda ledger, now: ledger.fail(run_pk, finished_at=now, error=message)
        )

    def _finalize_timeout(
        self, definition: JobDefinition, run_pk: UUID, timeout_seconds: float
    ) -> JobRun:
        message = (
            f"Job {definition.name!r} did not finish within its {timeout_seconds}-second timeout."
        )
        return self._apply_terminal(
            run_pk, lambda ledger, now: ledger.fail(run_pk, finished_at=now, error=message)
        )

    def _finalize_abandoned(self, run_pk: UUID) -> JobRun:
        return self._apply_terminal(
            run_pk, lambda ledger, now: ledger.abandon(run_pk, finished_at=now)
        )

    def _apply_terminal(
        self, run_pk: UUID, transition: Callable[[JobLedger, datetime], JobRun | None]
    ) -> JobRun:
        session = self.session_factory()
        try:
            ledger = JobLedger(session)
            now = self._now()
            run = transition(ledger, now)
            if run is None:
                # Someone else (a timeout, a shutdown, the handler itself)
                # already finalised this attempt first; report that state
                # rather than raising, since none of these callers can do
                # anything about having lost the race.
                run = ledger.get(run_pk)
            session.commit()
            if run is None:  # pragma: no cover - the row always exists once started
                raise RuntimeError(f"job_run {run_pk} vanished mid-execution.")
            return run
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _process_lock(self, job_name: str) -> threading.Lock:
        with self._process_locks_guard:
            lock = self._process_locks.get(job_name)
            if lock is None:
                lock = threading.Lock()
                self._process_locks[job_name] = lock
            return lock

    def _now(self) -> datetime:
        return self.clock.now()


_ACTIVE_RUNNER: JobRunner | None = None


def _dispatch_scheduled_tick(job_name: str) -> None:
    """The picklable entry point `Scheduler` registers with APScheduler's
    `SQLAlchemyJobStore` for every scheduled job: a plain, importable
    module-level function, looked up by string reference rather than by
    pickling a `JobRunner` instance and its live locks and thread pool."""
    if _ACTIVE_RUNNER is None:  # pragma: no cover - defensive; Scheduler always sets this first
        raise RuntimeError(
            f"Scheduled job {job_name!r} fired with no active Scheduler in this process."
        )
    _ACTIVE_RUNNER.submit(job_name, trigger="scheduled")


class Scheduler:
    """The live scheduling loop: APScheduler with a SQLAlchemy job store
    (`spec §13`'s ADR) decides when each job is next due, durably, across a
    restart; every fired tick is handed to `JobRunner`, which decides and
    records what happened.
    """

    def __init__(self, runner: JobRunner, *, database_url: str, timezone: str = "UTC") -> None:
        self.runner = runner
        self._timezone = timezone
        self._aps = BackgroundScheduler(
            jobstores={
                "default": SQLAlchemyJobStore(url=database_url, tablename=_APSCHEDULER_TABLE_NAME)
            },
            timezone=timezone,
        )
        # `SQLAlchemyJobStore` persists each job by pickling its target, so
        # that target must be a plain, importable module-level function, not
        # a bound method on this `runner` — a `JobRunner` holds live locks
        # and a thread pool, neither of which pickles. `_dispatch_scheduled_tick`
        # is the picklable indirection; it looks the active runner up by
        # process-wide reference instead. `spec §13`'s "single host, in-process
        # scheduling" means one live `Scheduler` per process is the deployed
        # shape this indirection assumes.
        global _ACTIVE_RUNNER
        _ACTIVE_RUNNER = runner

    def schedule(self, definition: JobDefinition) -> JobDefinition:
        """Register `definition` with the runner and, if it declares a
        cron schedule, add it to the live trigger loop."""
        self.runner.registry.register(definition)
        if definition.schedule is not None:
            self._aps.add_job(
                _dispatch_scheduled_tick,
                trigger=CronTrigger.from_crontab(definition.schedule, timezone=self._timezone),
                id=definition.name,
                name=definition.name,
                replace_existing=True,
                args=[definition.name],
                coalesce=True,
                max_instances=1,
            )
        return definition

    def start(self) -> tuple[JobRun, ...]:
        """Resolve every restart-orphaned run, then start accepting ticks."""
        resumed = self.runner.reconcile()
        self._aps.start()
        return resumed

    def shutdown(self, *, grace_period_seconds: float = 30.0) -> ShutdownReport:
        """Stop firing new ticks, then gracefully shut the runner down."""
        self._aps.shutdown(wait=False)
        return self.runner.shutdown(grace_period_seconds=grace_period_seconds)


__all__ = [
    "JobRunner",
    "Scheduler",
    "SchedulerShuttingDownError",
    "ShutdownReport",
]
