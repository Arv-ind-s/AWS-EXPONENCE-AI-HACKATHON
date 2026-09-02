"""Persistence adapter for reproducible forecast runs.

Forecast rows are immutable facts once written.  The only mutable record in
this module is the run envelope, whose state moves from ``running`` to
``incomplete`` or ``complete`` as a batch progresses.  Forecast and path
reads that can reach a caller are delegated to the normal scope-enforcing
repository base; the small unscoped helpers are intentionally private and
are used only by the scoring transaction while it owns the run.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import Select, or_, select
from sqlalchemy.orm import Session

from covenant_radar.core.errors import Conflict, ValidationError
from covenant_radar.core.ids import new_id
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.forecast import (
    Forecast,
    ForecastPath,
    ForecastRun,
    TriageEntry,
)
from covenant_radar.db.repositories.base import RepositoryAuditWriter, RepositoryBase
from covenant_radar.db.scoping import Scope, ownership_path_for
from covenant_radar.db.session import is_database_session

RUNNING: str = "running"
INCOMPLETE: str = "incomplete"
COMPLETE: str = "complete"
RUN_STATES: frozenset[str] = frozenset({RUNNING, INCOMPLETE, COMPLETE})


class ForecastConflict(Conflict):
    """A write-once forecast identity was recomputed with different data."""


class ForecastRepository:
    """Store forecast runs and their immutable forecast/path facts."""

    def __init__(
        self,
        session: Session,
        *,
        audit: RepositoryAuditWriter | None = None,
    ) -> None:
        if not is_database_session(session):
            raise TypeError("ForecastRepository requires a SQLAlchemy Session.")
        self.session = session
        self._forecasts = RepositoryBase(
            session,
            Forecast,
            ownership=ownership_path_for(Forecast),
            audit=audit,
        )
        self._paths = RepositoryBase(
            session,
            ForecastPath,
            ownership=ownership_path_for(ForecastPath),
            audit=audit,
        )

    def create_run(
        self,
        *,
        as_of_date: date,
        threshold_snapshot_id: UUID,
        model_version: str,
        covenant_count: int,
        started_at: datetime,
        job_run_id: UUID | None = None,
        actor_id: UUID | None = None,
        request_id: str,
        run_id: UUID | None = None,
    ) -> ForecastRun:
        """Create and flush one new run envelope.

        The caller's transaction remains responsible for commit.  Flushing
        here makes the run id and its uniqueness/foreign-key errors available
        before the first forecast is written.
        """

        _validate_run_inputs(
            as_of_date=as_of_date,
            threshold_snapshot_id=threshold_snapshot_id,
            model_version=model_version,
            covenant_count=covenant_count,
            started_at=started_at,
            job_run_id=job_run_id,
            actor_id=actor_id,
            request_id=request_id,
            run_id=run_id,
        )
        now = started_at
        row = ForecastRun(
            id=run_id or new_id(),
            as_of_date=as_of_date,
            job_run_id=job_run_id,
            threshold_snapshot_id=threshold_snapshot_id,
            model_version=model_version,
            started_at=started_at,
            finished_at=None,
            covenant_count=covenant_count,
            state=RUNNING,
            created_at=now,
            updated_at=now,
            created_by_id=actor_id,
            updated_by_id=actor_id,
            request_id=request_id,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def get_run(self, run_id: UUID, *, for_update: bool = False) -> ForecastRun | None:
        """Load a run for the owning scoring transaction."""

        _uuid(run_id, "run_id")
        statement: Select[tuple[ForecastRun]] = select(ForecastRun).where(ForecastRun.id == run_id)
        if for_update:
            statement = statement.with_for_update()
        return self.session.execute(statement).scalars().one_or_none()

    def begin_resume(self, run: ForecastRun, *, updated_at: datetime) -> ForecastRun:
        """Re-open an incomplete run without changing its snapshot/version."""

        if not isinstance(run, ForecastRun):
            raise TypeError("begin_resume requires a ForecastRun.")
        _aware_datetime(updated_at, "updated_at")
        if run.state == COMPLETE:
            return run
        if run.state not in {RUNNING, INCOMPLETE}:
            raise ValidationError(f"Unknown forecast run state {run.state!r}.", field="state")
        run.state = RUNNING
        run.finished_at = None
        run.updated_at = updated_at
        self.session.flush()
        return run

    def save_forecast(self, row: Forecast) -> Forecast:
        """Insert one forecast, or verify an identical existing one.

        There is deliberately no update path.  A rerun of the same run is
        idempotent only when every persisted decision is byte-for-byte
        equivalent after canonical normalisation.
        """

        if not isinstance(row, Forecast):
            raise TypeError("save_forecast requires a Forecast row.")
        _validate_forecast_identity(row)
        existing = self._forecast_by_identity(
            row.run_id,
            row.covenant_version_id,
            row.horizon_days,
        )
        if existing is not None:
            if _forecast_payload(existing) != _forecast_payload(row):
                raise ForecastConflict(
                    "The same forecast run/covenant/horizon was recomputed with different output."
                )
            return existing
        self.session.add(row)
        self.session.flush()
        return row

    def save_path(self, row: ForecastPath) -> ForecastPath:
        """Insert one daily path point, or verify an identical existing one."""

        if not isinstance(row, ForecastPath):
            raise TypeError("save_path requires a ForecastPath row.")
        _validate_path_identity(row)
        existing = self._path_by_identity(
            row.run_id,
            row.covenant_version_id,
            row.day_offset,
        )
        if existing is not None:
            if _path_payload(existing) != _path_payload(row):
                raise ForecastConflict(
                    "The same forecast run/covenant/day was recomputed with different output."
                )
            return existing
        self.session.add(row)
        self.session.flush()
        return row

    def attempted_covenant_ids(self, run_id: UUID) -> frozenset[UUID]:
        """Return the distinct covenant versions already attempted in a run."""

        _uuid(run_id, "run_id")
        statement = select(Forecast.covenant_version_id).where(Forecast.run_id == run_id).distinct()
        return frozenset(self.session.execute(statement).scalars().all())

    def horizons_for_run(self, run_id: UUID) -> frozenset[int]:
        """Return horizons already committed to a run for resume validation."""

        _uuid(run_id, "run_id")
        statement = select(Forecast.horizon_days).where(Forecast.run_id == run_id).distinct()
        return frozenset(self.session.execute(statement).scalars().all())

    def forecasts_for_run(self, run_id: UUID, *, scope: Scope) -> Sequence[Forecast]:
        """Return only in-scope forecast records for a run."""

        _uuid(run_id, "run_id")
        statement: Select[Any] = self._forecasts._scoped_select(scope).where(
            Forecast.run_id == run_id
        )
        statement = statement.order_by(
            Forecast.covenant_version_id,
            Forecast.horizon_days,
        )
        return tuple(cast(Sequence[Forecast], self.session.execute(statement).scalars().all()))

    def paths_for_run(self, run_id: UUID, *, scope: Scope) -> Sequence[ForecastPath]:
        """Return only in-scope daily path records for a run."""

        _uuid(run_id, "run_id")
        statement: Select[Any] = self._paths._scoped_select(scope).where(
            ForecastPath.run_id == run_id
        )
        statement = statement.order_by(
            ForecastPath.covenant_version_id,
            ForecastPath.day_offset,
        )
        return tuple(cast(Sequence[ForecastPath], self.session.execute(statement).scalars().all()))

    def latest_complete_run(
        self,
        *,
        as_of_date: date,
        scope: Scope,
    ) -> ForecastRun | None:
        """Return the newest complete run visible in ``scope``."""

        if not isinstance(as_of_date, date):
            raise TypeError("as_of_date must be a calendar date.")
        ownership = ownership_path_for(Forecast)
        visible_forecasts: Select[Any] = select(Forecast.run_id)
        visible_forecasts = ownership.apply(visible_forecasts).where(
            scope.predicate(ownership.path_column)
        )
        statement = (
            select(ForecastRun)
            .where(
                ForecastRun.as_of_date == as_of_date,
                ForecastRun.state == COMPLETE,
                ForecastRun.id.in_(visible_forecasts),
            )
            .order_by(ForecastRun.finished_at.desc(), ForecastRun.id.desc())
            .limit(1)
        )
        return self.session.execute(statement).scalars().one_or_none()

    def latest_complete_run_for_borrower(
        self,
        borrower_id: UUID,
        *,
        scope: Scope,
    ) -> ForecastRun | None:
        """Return the newest complete run that contains this borrower.

        A portfolio can contain both full-book runs and off-cycle
        single-borrower rechecks.  Selecting the globally newest run makes a
        valid forecast disappear from every borrower not included in the
        recheck.  The borrower workspace and memo collector therefore use
        this read: a run is eligible only when it contains either a forecast
        or the explicit no-forecast triage row for ``borrower_id``, and both
        visibility probes carry the caller's portfolio scope.
        """

        _uuid(borrower_id, "borrower_id")
        if not isinstance(scope, Scope):
            raise TypeError("scope must be a Scope.")

        forecast_ownership = ownership_path_for(Forecast)
        visible_forecast_runs: Select[Any] = select(Forecast.run_id)
        visible_forecast_runs = forecast_ownership.apply(visible_forecast_runs).where(
            Borrower.id == borrower_id,
            scope.predicate(forecast_ownership.path_column),
        )

        triage_ownership = ownership_path_for(TriageEntry)
        visible_triage_runs: Select[Any] = select(TriageEntry.run_id)
        visible_triage_runs = triage_ownership.apply(visible_triage_runs).where(
            TriageEntry.borrower_id == borrower_id,
            scope.predicate(triage_ownership.path_column),
        )

        statement = (
            select(ForecastRun)
            .where(
                ForecastRun.state == COMPLETE,
                or_(
                    ForecastRun.id.in_(visible_forecast_runs),
                    ForecastRun.id.in_(visible_triage_runs),
                ),
            )
            .order_by(
                ForecastRun.as_of_date.desc(),
                ForecastRun.finished_at.desc().nullslast(),
                ForecastRun.id.desc(),
            )
            .limit(1)
        )
        return self.session.execute(statement).scalars().one_or_none()

    def mark_incomplete(self, run: ForecastRun, *, finished_at: datetime) -> ForecastRun:
        """Persist an interrupted state that remains eligible for resume."""

        _run_state(run, {RUNNING, INCOMPLETE})
        _aware_datetime(finished_at, "finished_at")
        run.state = INCOMPLETE
        run.finished_at = finished_at
        run.updated_at = finished_at
        self.session.flush()
        return run

    def mark_complete(
        self,
        run: ForecastRun,
        *,
        finished_at: datetime,
        attempted_count: int,
    ) -> ForecastRun:
        """Complete only when the run's expected covenant set is present."""

        _run_state(run, {RUNNING, INCOMPLETE, COMPLETE})
        _aware_datetime(finished_at, "finished_at")
        if run.covenant_count is None:
            raise ValidationError("A forecast run must carry covenant_count before completion.")
        if (
            isinstance(attempted_count, bool)
            or not isinstance(attempted_count, int)
            or attempted_count != run.covenant_count
        ):
            raise Conflict(
                "A forecast run cannot be complete until every covenant has been attempted."
            )
        run.state = COMPLETE
        run.finished_at = finished_at
        run.updated_at = finished_at
        self.session.flush()
        return run

    def _forecast_by_identity(
        self,
        run_id: UUID,
        covenant_version_id: UUID,
        horizon_days: int,
    ) -> Forecast | None:
        statement = select(Forecast).where(
            Forecast.run_id == run_id,
            Forecast.covenant_version_id == covenant_version_id,
            Forecast.horizon_days == horizon_days,
        )
        return self.session.execute(statement).scalars().one_or_none()

    def _path_by_identity(
        self,
        run_id: UUID,
        covenant_version_id: UUID,
        day_offset: int,
    ) -> ForecastPath | None:
        statement = select(ForecastPath).where(
            ForecastPath.run_id == run_id,
            ForecastPath.covenant_version_id == covenant_version_id,
            ForecastPath.day_offset == day_offset,
        )
        return self.session.execute(statement).scalars().one_or_none()


SqlAlchemyForecastRepository = ForecastRepository


def _validate_run_inputs(
    *,
    as_of_date: date,
    threshold_snapshot_id: UUID,
    model_version: str,
    covenant_count: int,
    started_at: datetime,
    job_run_id: UUID | None,
    actor_id: UUID | None,
    request_id: str,
    run_id: UUID | None,
) -> None:
    if isinstance(as_of_date, datetime) or not isinstance(as_of_date, date):
        raise TypeError("as_of_date must be a calendar date.")
    _uuid(threshold_snapshot_id, "threshold_snapshot_id")
    _bounded_text(model_version, "model_version", 50)
    if (
        isinstance(covenant_count, bool)
        or not isinstance(covenant_count, int)
        or covenant_count < 0
    ):
        raise ValueError("covenant_count must be a non-negative integer.")
    _aware_datetime(started_at, "started_at")
    if job_run_id is not None:
        _uuid(job_run_id, "job_run_id")
    if actor_id is not None:
        _uuid(actor_id, "actor_id")
    _bounded_text(request_id, "request_id", 40)
    if run_id is not None:
        _uuid(run_id, "run_id")


def _validate_forecast_identity(row: Forecast) -> None:
    _uuid(row.run_id, "run_id")
    _uuid(row.covenant_version_id, "covenant_version_id")
    if (
        isinstance(row.horizon_days, bool)
        or not isinstance(row.horizon_days, int)
        or row.horizon_days < 0
    ):
        raise ValueError("horizon_days must be a non-negative integer.")


def _validate_path_identity(row: ForecastPath) -> None:
    _uuid(row.run_id, "run_id")
    _uuid(row.covenant_version_id, "covenant_version_id")
    if (
        isinstance(row.day_offset, bool)
        or not isinstance(row.day_offset, int)
        or row.day_offset < 0
    ):
        raise ValueError("day_offset must be a non-negative integer.")


def _forecast_payload(row: Forecast) -> tuple[object, ...]:
    return (
        row.run_id,
        row.covenant_version_id,
        row.horizon_days,
        row.probability,
        row.probability_source,
        row.fallback_reason,
        row.confidence,
        row.below_confidence_floor,
        row.projected_cross_date,
        row.direction,
        _canonical(row.formula_inputs),
        row.data_as_of,
        row.staleness_days,
    )


def _path_payload(row: ForecastPath) -> tuple[object, ...]:
    return (
        row.run_id,
        row.covenant_version_id,
        row.day_offset,
        row.projected_value,
        row.headroom_pct,
    )


def _canonical(value: object) -> object:
    if isinstance(value, dict):
        return tuple(sorted((str(key), _canonical(item)) for key, item in value.items()))
    if isinstance(value, list | tuple):
        return tuple(_canonical(item) for item in value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


def _run_state(run: ForecastRun, allowed: set[str]) -> None:
    if run.state not in RUN_STATES:
        raise ValidationError(f"Unknown forecast run state {run.state!r}.", field="state")
    if run.state not in allowed:
        raise Conflict(f"Forecast run state {run.state!r} cannot be changed by this operation.")


def _uuid(value: object, field: str) -> UUID:
    if not isinstance(value, UUID):
        raise TypeError(f"{field} must be a UUID.")
    return value


def _aware_datetime(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise TypeError(f"{field} must be timezone-aware.")
    return value


def _bounded_text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum or not value.strip():
        raise ValueError(f"{field} must be non-empty text of at most {maximum} characters.")
    return value


__all__ = [
    "COMPLETE",
    "ForecastConflict",
    "ForecastRepository",
    "INCOMPLETE",
    "RUNNING",
    "RUN_STATES",
    "SqlAlchemyForecastRepository",
]
