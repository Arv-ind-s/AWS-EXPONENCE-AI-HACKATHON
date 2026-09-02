"""The run ledger: `JobRun` reads and concurrency-safe writes
(`plan.md §5.9`, `spec §R-28`).

Starting a run is serialised per job name — a PostgreSQL transaction-scoped
advisory lock, or SQLite's `BEGIN IMMEDIATE` write lock — so the
check-for-a-running-row-then-insert sequence is atomic across processes:
two schedulers racing to start the same job can never both win, and the
loser gets back the running attempt it lost to, to report why it is idle.

Every transition out of `running` is a conditional `UPDATE ... WHERE
state = 'running'`, so a late-finishing attempt can never clobber a
terminal state something else already wrote — the read-then-write race an
ordinary `session.add`/`commit` would allow between, say, a timeout firing
and the handler finishing a moment later, or a graceful shutdown abandoning
a run just as it completes.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime
from typing import Final
from uuid import UUID

from sqlalchemy import Select, select, text, update
from sqlalchemy.orm import Session

from covenant_radar.core.context import new_job_run_id
from covenant_radar.core.errors import Conflict
from covenant_radar.db.models.operations import JobRun
from covenant_radar.scheduler.jobs import JobDefinition

RUNNING: Final = "running"
SUCCEEDED: Final = "succeeded"
FAILED: Final = "failed"
INTERRUPTED: Final = "interrupted"
ABANDONED: Final = "abandoned"

TERMINAL_STATES: Final[frozenset[str]] = frozenset({SUCCEEDED, FAILED, INTERRUPTED, ABANDONED})


class JobAlreadyRunningError(Conflict):
    """Raised when a job is triggered while an attempt is already running."""

    def __init__(self, job_name: str, running: JobRun) -> None:
        self.job_name = job_name
        self.running_run_id = running.run_id
        self.started_at = running.started_at
        self.trigger = running.trigger
        super().__init__(
            f"Job {job_name!r} is already running as run {running.run_id!r} "
            f"(attempt {running.attempt}, triggered by {running.trigger!r}, "
            f"started {running.started_at.isoformat()}); refusing to start a second instance."
        )


class JobLedger:
    """Reads and concurrency-safe writes against the `job_run` table."""

    def __init__(self, session: Session) -> None:
        if not isinstance(session, Session):
            raise TypeError("JobLedger requires a SQLAlchemy Session.")
        self.session = session

    def running_run(self, job_name: str) -> JobRun | None:
        """The current `running` attempt for `job_name`, if any."""
        statement: Select[tuple[JobRun]] = (
            select(JobRun)
            .where(JobRun.job_name == job_name, JobRun.state == RUNNING)
            .order_by(JobRun.started_at.desc())
            .limit(1)
        )
        return self.session.execute(statement).scalars().one_or_none()

    def running_runs(self) -> tuple[JobRun, ...]:
        """Every row still `running` — after a hard restart, every one of
        these is orphaned and needs `spec §R-28`'s reconciliation."""
        statement: Select[tuple[JobRun]] = select(JobRun).where(JobRun.state == RUNNING)
        return tuple(self.session.execute(statement).scalars().all())

    def start_or_refuse(
        self,
        definition: JobDefinition,
        *,
        trigger: str,
        started_at: datetime,
        request_id: str,
        run_id: str | None = None,
        attempt: int = 1,
        created_by_id: UUID | None = None,
    ) -> JobRun:
        """Atomically check-and-insert the next `running` attempt of
        `definition`, raising `JobAlreadyRunningError` naming the running
        instance if one already exists.

        Must be the first statement issued on `self.session` — the SQLite
        branch of the serialising lock only has an effect before this
        session's transaction has otherwise begun.
        """
        if attempt < 1:
            raise ValueError("attempt must be at least 1.")
        self._acquire_start_lock(definition.name)
        existing = self.running_run(definition.name)
        if existing is not None:
            raise JobAlreadyRunningError(definition.name, existing)

        run = JobRun(
            job_name=definition.name,
            run_id=run_id or new_job_run_id(),
            trigger=trigger,
            started_at=started_at,
            finished_at=None,
            state=RUNNING,
            attempt=attempt,
            error=None,
            metrics=None,
            created_at=started_at,
            updated_at=started_at,
            created_by_id=created_by_id,
            updated_by_id=created_by_id,
            request_id=request_id,
        )
        self.session.add(run)
        self.session.flush()
        return run

    def succeed(
        self, run_id: UUID, *, finished_at: datetime, metrics: Mapping[str, object] | None = None
    ) -> JobRun | None:
        """Transition `run_id` to `succeeded`. `None` if it was not
        `running` (someone else already finalised it)."""
        return self._transition(
            run_id, to_state=SUCCEEDED, finished_at=finished_at, error=None, metrics=metrics
        )

    def fail(
        self,
        run_id: UUID,
        *,
        finished_at: datetime,
        error: str,
        metrics: Mapping[str, object] | None = None,
    ) -> JobRun | None:
        """Transition `run_id` to `failed`, recording `error`."""
        return self._transition(
            run_id, to_state=FAILED, finished_at=finished_at, error=error, metrics=metrics
        )

    def interrupt(self, run_id: UUID, *, finished_at: datetime) -> JobRun | None:
        """Transition `run_id` to `interrupted` — a hard-restart orphan,
        resolved by `spec §R-28`'s reconciliation, never left ambiguous."""
        return self._transition(
            run_id,
            to_state=INTERRUPTED,
            finished_at=finished_at,
            error="Interrupted by a scheduler restart.",
            metrics=None,
        )

    def abandon(self, run_id: UUID, *, finished_at: datetime) -> JobRun | None:
        """Transition `run_id` to `abandoned` — still running past a
        graceful shutdown's grace period."""
        return self._transition(
            run_id,
            to_state=ABANDONED,
            finished_at=finished_at,
            error="Abandoned at graceful shutdown: exceeded the shutdown grace period.",
            metrics=None,
        )

    def get(self, run_id: UUID) -> JobRun | None:
        """The run by primary key, whatever its state."""
        return self.session.get(JobRun, run_id)

    def _transition(
        self,
        run_id: UUID,
        *,
        to_state: str,
        finished_at: datetime,
        error: str | None,
        metrics: Mapping[str, object] | None,
    ) -> JobRun | None:
        statement = (
            update(JobRun)
            .where(JobRun.id == run_id, JobRun.state == RUNNING)
            .values(
                state=to_state,
                finished_at=finished_at,
                error=error,
                metrics=dict(metrics) if metrics is not None else None,
                updated_at=finished_at,
            )
        )
        result = self.session.execute(statement)
        if result.rowcount != 1:
            return None
        self.session.flush()
        # `populate_existing` forces a re-fetch even if `run_id` is already
        # present (and now stale) in this session's identity map, since the
        # UPDATE above bypassed the ORM and its unit-of-work bookkeeping.
        return self.session.get(JobRun, run_id, populate_existing=True)

    def _acquire_start_lock(self, job_name: str) -> None:
        dialect = self.session.get_bind().dialect.name
        if dialect == "postgresql":
            self.session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _advisory_lock_key(job_name)},
            )
            return
        connection = self.session.connection()
        if not connection.in_transaction():
            connection.exec_driver_sql("BEGIN IMMEDIATE")


def _advisory_lock_key(job_name: str) -> int:
    """A stable signed-64-bit key for `pg_advisory_xact_lock`, one per job name."""
    digest = hashlib.sha256(job_name.encode("utf-8")).digest()[:8]
    return int.from_bytes(digest, byteorder="big", signed=True)


__all__ = [
    "ABANDONED",
    "FAILED",
    "INTERRUPTED",
    "JobAlreadyRunningError",
    "JobLedger",
    "RUNNING",
    "SUCCEEDED",
    "TERMINAL_STATES",
]
