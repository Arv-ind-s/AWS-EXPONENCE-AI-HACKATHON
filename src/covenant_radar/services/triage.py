"""Persistence adapter for the portfolio triage what-changed view."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Final, Protocol
from uuid import UUID

from sqlalchemy import Select, select, tuple_
from sqlalchemy.orm import Session

from covenant_radar.audit.events import AuditEventType
from covenant_radar.core.context import get_request_id, new_request_id
from covenant_radar.core.errors import Conflict, NotFound
from covenant_radar.db.models.forecast import (
    Forecast,
    ForecastDriver,
    ForecastRun,
)
from covenant_radar.db.models.forecast import (
    TriageEntry as TriageEntryModel,
)
from covenant_radar.db.repositories.triage import (
    QueueCursor,
    TriageRepository,
)
from covenant_radar.db.scoping import Scope
from covenant_radar.domain.triage.changes import (
    ChangeThresholds,
    WhatChanged,
)
from covenant_radar.domain.triage.changes import (
    compare_runs as compare_domain_runs,
)
from covenant_radar.domain.triage.views import QueueFilters, QueuePage

_COMPLETE: Final[str] = "complete"
_REQUEST_ID_MAX_LENGTH: Final[int] = 40


class AuditWriter(Protocol):
    """The append-only audit boundary supplied by the caller."""

    def record(
        self,
        event_type: str,
        subject: object,
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        """Append one event in the caller's current transaction."""


@dataclass(frozen=True, slots=True)
class TriageRunComparison:
    """Comparison output with the run identities needed for auditability."""

    current_run_id: UUID
    previous_run_id: UUID | None
    changes: Mapping[UUID, WhatChanged]
    current: tuple[WhatChanged, ...]
    disappeared: tuple[WhatChanged, ...]
    first_run: bool

    def __post_init__(self) -> None:
        if not isinstance(self.current_run_id, UUID):
            raise TypeError("current_run_id must be a UUID.")
        if self.previous_run_id is not None and not isinstance(self.previous_run_id, UUID):
            raise TypeError("previous_run_id must be a UUID or None.")
        object.__setattr__(self, "changes", MappingProxyType(dict(self.changes)))
        if not isinstance(self.current, tuple) or not isinstance(self.disappeared, tuple):
            raise TypeError("current and disappeared must be tuples.")

    @property
    def newly_unmonitored(self) -> tuple[WhatChanged, ...]:
        return self.disappeared

    def __getitem__(self, borrower_id: UUID) -> WhatChanged:
        return self.changes[borrower_id]


class TriageService:
    """Compare and persist immutable triage-run summaries.

    The service never commits.  It participates in the caller's transaction,
    allowing the triage write and its what-changed update to become visible as
    one unit.  Existing summaries are verified rather than overwritten so a
    rerun cannot silently rewrite a completed run.
    """

    def __init__(
        self,
        session: Session,
        thresholds: ChangeThresholds | Mapping[str, object] | object | None = None,
        *,
        audit: AuditWriter,
        change_thresholds: ChangeThresholds | Mapping[str, object] | object | None = None,
        threshold_store: ChangeThresholds | Mapping[str, object] | object | None = None,
        cursor_secret: bytes | str | None = None,
        request_id: str | None = None,
    ) -> None:
        if not isinstance(session, Session):
            raise TypeError("TriageService requires a SQLAlchemy Session.")
        if audit is None or not callable(getattr(audit, "record", None)):
            raise TypeError("TriageService requires an append-only audit writer.")
        supplied = tuple(
            value for value in (thresholds, change_thresholds, threshold_store) if value is not None
        )
        if len(supplied) > 1:
            raise TypeError(
                "Provide exactly one of thresholds, change_thresholds or threshold_store."
            )
        configured = supplied[0] if supplied else None
        self.session = session
        self.audit = audit
        self.request_id = request_id or get_request_id() or new_request_id()
        if (
            not isinstance(self.request_id, str)
            or not 1 <= len(self.request_id) <= _REQUEST_ID_MAX_LENGTH
        ):
            raise ValueError(
                f"Triage request_id must be between 1 and {_REQUEST_ID_MAX_LENGTH} characters."
            )
        self.change_thresholds = ChangeThresholds.from_value(configured)
        self.queue_repository = TriageRepository(session, cursor_secret=cursor_secret)

    def query(
        self,
        scope: Scope,
        filters: QueueFilters | Mapping[str, object] | None = None,
        *,
        page_size: int = 50,
        limit: int | None = None,
        cursor: str | QueueCursor | None = None,
        band: str | None = None,
        portfolio: UUID | str | None = None,
        portfolio_id: UUID | str | None = None,
        industry: str | None = None,
        industry_code: str | None = None,
        assignee: UUID | str | None = None,
        assignee_id: UUID | str | None = None,
        sma_band: str | None = None,
        case_state: str | None = None,
        case_status: str | None = None,
    ) -> QueuePage:
        """Return the scoped queue read model through the service boundary."""
        return self.queue_repository.query(
            scope,
            filters,
            page_size=page_size,
            limit=limit,
            cursor=cursor,
            band=band,
            portfolio=portfolio,
            portfolio_id=portfolio_id,
            industry=industry,
            industry_code=industry_code,
            assignee=assignee,
            assignee_id=assignee_id,
            sma_band=sma_band,
            case_state=case_state,
            case_status=case_status,
        )

    queue = query
    query_queue = query

    def compare(
        self,
        current_run_id: UUID,
        *,
        previous_run_id: UUID | None = None,
    ) -> TriageRunComparison:
        """Compare a complete run with the immediately preceding complete run."""

        _uuid(current_run_id, "current_run_id")
        if previous_run_id is not None:
            _uuid(previous_run_id, "previous_run_id")
        current_run = self._complete_run(current_run_id, role="current")
        previous_run = (
            self._complete_run(previous_run_id, role="previous")
            if previous_run_id is not None
            else self._latest_prior_complete_run(current_run)
        )
        if previous_run is not None:
            _ensure_prior_run(current_run, previous_run)

        current_rows = self._entries_for_run(current_run.id)
        previous_rows = self._entries_for_run(previous_run.id) if previous_run is not None else ()
        drivers = self._drivers_for_rows(current_rows, previous_rows)
        current_facts = tuple(_row_facts(row, drivers) for row in current_rows)
        previous_facts = tuple(_row_facts(row, drivers) for row in previous_rows)
        comparison = compare_domain_runs(
            current_facts,
            None if previous_run is None else previous_facts,
            self.change_thresholds,
        )
        return TriageRunComparison(
            current_run_id=current_run.id,
            previous_run_id=previous_run.id if previous_run is not None else None,
            changes=comparison.changes,
            current=comparison.current,
            disappeared=comparison.disappeared,
            first_run=comparison.first_run,
        )

    def persist_what_changed(
        self,
        current_run_id: UUID,
        *,
        previous_run_id: UUID | None = None,
    ) -> TriageRunComparison:
        """Compute and attach summaries to the current run's triage rows."""

        comparison = self.compare(current_run_id, previous_run_id=previous_run_id)
        rows = self._entries_for_run(current_run_id)
        expected = {row.borrower_id: comparison.changes[row.borrower_id].summary for row in rows}
        conflicts = sorted(
            (
                row.borrower_id,
                row.what_changed,
                expected[row.borrower_id],
            )
            for row in rows
            if row.what_changed is not None and row.what_changed != expected[row.borrower_id]
        )
        if conflicts:
            borrower_id, stored, calculated = conflicts[0]
            raise Conflict(
                f"Triage entry {borrower_id} already has a different what-changed summary; "
                f"stored {stored!r}, calculated {calculated!r}."
            )
        updated_borrower_ids: list[UUID] = []
        for row in rows:
            if row.what_changed is None:
                row.what_changed = expected[row.borrower_id]
                updated_borrower_ids.append(row.borrower_id)
        self.session.flush()
        for borrower_id in updated_borrower_ids:
            self.audit.record(
                AuditEventType.TRIAGE_WHAT_CHANGED_RECORDED.value,
                ("borrower", borrower_id),
                {
                    "run_id": str(current_run_id),
                    "summary": expected[borrower_id],
                },
                actor=None,
                request_id=self.request_id,
            )
        self.audit.record(
            AuditEventType.TRIAGE_COMPARISON_PERSISTED.value,
            ("forecast_run", current_run_id),
            {
                "previous_run_id": (
                    str(comparison.previous_run_id)
                    if comparison.previous_run_id is not None
                    else None
                ),
                "first_run": comparison.first_run,
                "entries_total": len(rows),
                "entries_updated": len(updated_borrower_ids),
            },
            actor=None,
            request_id=self.request_id,
        )
        return comparison

    # Explicit aliases make the write operation discoverable at call sites
    # without creating a second implementation with different semantics.
    compare_and_persist = persist_what_changed
    update_what_changed = persist_what_changed

    def _complete_run(self, run_id: UUID, *, role: str) -> ForecastRun:
        run = self.session.get(ForecastRun, run_id)
        if run is None:
            raise NotFound(f"{role.capitalize()} forecast run {run_id} was not found.")
        if run.state != _COMPLETE:
            raise Conflict(
                f"{role.capitalize()} forecast run {run_id} is {run.state}; "
                "only a completed run can be compared."
            )
        return run

    def _latest_prior_complete_run(self, current: ForecastRun) -> ForecastRun | None:
        statement: Select[tuple[ForecastRun]] = select(ForecastRun).where(
            ForecastRun.state == _COMPLETE,
            ForecastRun.id != current.id,
        )
        if current.finished_at is not None:
            statement = statement.where(ForecastRun.finished_at < current.finished_at)
        else:
            statement = statement.where(ForecastRun.as_of_date < current.as_of_date)
        statement = statement.order_by(
            ForecastRun.finished_at.desc(),
            ForecastRun.as_of_date.desc(),
            ForecastRun.id.desc(),
        ).limit(1)
        return self.session.execute(statement).scalars().one_or_none()

    def _entries_for_run(self, run_id: UUID) -> tuple[TriageEntryModel, ...]:
        statement = (
            select(TriageEntryModel)
            .where(TriageEntryModel.run_id == run_id)
            .order_by(TriageEntryModel.rank.asc(), TriageEntryModel.borrower_id.asc())
        )
        return tuple(self.session.execute(statement).scalars().all())

    def _drivers_for_rows(
        self,
        current_rows: Iterable[TriageEntryModel],
        previous_rows: Iterable[TriageEntryModel],
    ) -> Mapping[tuple[UUID, UUID, int], Mapping[str, Decimal]]:
        keys = {
            (row.run_id, row.worst_covenant_version_id, row.worst_horizon)
            for row in (*tuple(current_rows), *tuple(previous_rows))
            if row.worst_covenant_version_id is not None and row.worst_horizon is not None
        }
        if not keys:
            return MappingProxyType({})
        statement = (
            select(
                Forecast.run_id,
                Forecast.covenant_version_id,
                Forecast.horizon_days,
                ForecastDriver.name,
                ForecastDriver.share,
            )
            .join(ForecastDriver, ForecastDriver.forecast_id == Forecast.id)
            .where(
                tuple_(
                    Forecast.run_id,
                    Forecast.covenant_version_id,
                    Forecast.horizon_days,
                ).in_(tuple(sorted(keys, key=_driver_key)))
            )
        )
        grouped: dict[tuple[UUID, UUID, int], dict[str, Decimal]] = {}
        for run_id, covenant_id, horizon, name, share in self.session.execute(statement).all():
            grouped.setdefault((run_id, covenant_id, horizon), {})[name] = share
        return MappingProxyType({key: MappingProxyType(value) for key, value in grouped.items()})


def compute_and_persist_what_changed(
    session: Session,
    current_run_id: UUID,
    thresholds: ChangeThresholds | Mapping[str, object] | object,
    *,
    audit: AuditWriter,
    previous_run_id: UUID | None = None,
) -> TriageRunComparison:
    """Functional entry point for jobs that do not need to retain a service."""

    return TriageService(session, thresholds, audit=audit).persist_what_changed(
        current_run_id,
        previous_run_id=previous_run_id,
    )


def _row_facts(
    row: TriageEntryModel,
    drivers: Mapping[tuple[UUID, UUID, int], Mapping[str, Decimal]],
) -> dict[str, object]:
    driver_shares: Mapping[str, Decimal] = {}
    if row.worst_covenant_version_id is not None and row.worst_horizon is not None:
        driver_shares = drivers.get(
            (row.run_id, row.worst_covenant_version_id, row.worst_horizon),
            {},
        )
    probability = row.probability
    if probability is not None:
        state = "available"
    elif row.worst_covenant_version_id is not None:
        state = "suppressed"
    else:
        state = "no_forecast"
    return {
        "borrower_id": row.borrower_id,
        "reference": str(row.borrower_id),
        "band": row.band or "watch",
        "probability": probability,
        "state": state,
        "suppressed": state == "suppressed",
        "drivers": driver_shares,
        "worst_covenant_version_id": row.worst_covenant_version_id,
    }


def _ensure_prior_run(current: ForecastRun, previous: ForecastRun) -> None:
    if current.id == previous.id:
        raise Conflict("A forecast run cannot be compared with itself.")
    if current.finished_at is not None and previous.finished_at is not None:
        if previous.finished_at >= current.finished_at:
            raise Conflict("The selected comparison run is not earlier than the current run.")
    elif previous.as_of_date >= current.as_of_date:
        raise Conflict("The selected comparison run is not earlier than the current run.")


def _driver_key(value: tuple[UUID, UUID, int]) -> tuple[int, int, int]:
    return value[0].int, value[1].int, value[2]


def _uuid(value: object, field_name: str) -> UUID:
    if not isinstance(value, UUID):
        raise TypeError(f"{field_name} must be a UUID.")
    return value


__all__ = [
    "AuditWriter",
    "TriageRunComparison",
    "TriageService",
    "compute_and_persist_what_changed",
]
