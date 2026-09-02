"""Persistence adapter for immutable forecast-driver explanations.

Forecast drivers are derived from an immutable forecast.  The adapter keeps
the write path idempotent for resumable scoring runs while exposing only
portfolio-scoped reads to callers.  There is deliberately no update or
delete method: changing a forecast creates a new forecast row and therefore
new driver facts.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from decimal import Decimal
from typing import cast
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from covenant_radar.core.errors import Conflict
from covenant_radar.db.models.forecast import ForecastDriver
from covenant_radar.db.repositories.base import RepositoryAuditWriter, RepositoryBase
from covenant_radar.db.scoping import Scope, ownership_path_for

_DRIVER_NAME_MAX_LENGTH = 100
_OTHER_NAME = "other"


class DriverRepository(RepositoryBase[ForecastDriver]):
    """Scoped repository for the drivers belonging to one forecast."""

    def __init__(self, session: Session, *, audit: RepositoryAuditWriter | None = None) -> None:
        super().__init__(
            session,
            ForecastDriver,
            ownership=ownership_path_for(ForecastDriver),
            audit=audit,
        )

    def for_forecast(self, forecast_id: UUID, *, scope: Scope) -> Sequence[ForecastDriver]:
        """Return a forecast's drivers in deterministic display order."""

        _uuid(forecast_id, "forecast_id")
        statement: Select[tuple[ForecastDriver]] = cast(
            Select[tuple[ForecastDriver]], self._scoped_select(scope)
        )
        statement = statement.where(ForecastDriver.forecast_id == forecast_id).order_by(
            ForecastDriver.name,
            ForecastDriver.id,
        )
        return tuple(self.session.execute(statement).scalars().all())

    list_for_forecast = for_forecast

    def save(self, row: ForecastDriver) -> ForecastDriver:
        """Insert a driver or verify the identical immutable fact exists.

        A scoring retry may reconstruct the row with a new generated id and
        timestamps.  Those persistence metadata fields do not change the
        fact, so only the forecast identity and driver payload participate in
        the idempotence check.
        """

        _validate_row(row)
        existing_rows = tuple(
            self.session.execute(
                select(ForecastDriver)
                .where(
                    ForecastDriver.forecast_id == row.forecast_id,
                    ForecastDriver.name == row.name,
                )
                .order_by(ForecastDriver.id)
            )
            .scalars()
            .all()
        )
        if len(existing_rows) > 1:
            raise Conflict(f"Forecast {row.forecast_id} has duplicate driver name {row.name!r}.")
        if existing_rows:
            existing = existing_rows[0]
            if _payload(existing) != _payload(row):
                raise Conflict(
                    f"Forecast driver {row.name!r} was recomputed with different output."
                )
            return existing

        self.session.add(row)
        self.session.flush()
        return row

    def save_many(self, rows: Iterable[ForecastDriver]) -> tuple[ForecastDriver, ...]:
        """Persist one complete driver set without committing the transaction."""

        values = tuple(rows)
        seen: set[tuple[UUID, str]] = set()
        for row in values:
            _validate_row(row)
            identity = (row.forecast_id, row.name)
            if identity in seen:
                raise ValueError(
                    f"Forecast driver names must be unique per forecast: {row.name!r}."
                )
            seen.add(identity)
        return tuple(self.save(row) for row in values)


ForecastDriverRepository = DriverRepository


def _validate_row(row: ForecastDriver) -> None:
    if not isinstance(row, ForecastDriver):
        raise TypeError("DriverRepository requires a ForecastDriver row.")
    _uuid(row.forecast_id, "forecast_id")
    if not isinstance(row.name, str) or not row.name.strip():
        raise ValueError("Forecast driver name must be non-empty text.")
    if len(row.name) > _DRIVER_NAME_MAX_LENGTH:
        raise ValueError(
            f"Forecast driver name must be at most {_DRIVER_NAME_MAX_LENGTH} characters."
        )
    if not isinstance(row.share, Decimal) or not row.share.is_finite():
        raise ValueError("Forecast driver share must be a finite Decimal.")
    _uuid_or_none(row.evidence_id, "evidence_id")
    if not isinstance(row.is_other, bool):
        raise TypeError("Forecast driver is_other must be a boolean.")
    if row.is_other != (row.name == _OTHER_NAME):
        raise ValueError("Only the 'other' forecast driver may be flagged as other.")
    _uuid_or_none(row.created_by_id, "created_by_id")
    _uuid_or_none(row.updated_by_id, "updated_by_id")
    _aware_datetime(row.created_at, "created_at")
    _aware_datetime(row.updated_at, "updated_at")
    if not isinstance(row.request_id, str) or not 1 <= len(row.request_id) <= 40:
        raise ValueError("Forecast driver request_id must be between 1 and 40 characters.")


def _payload(row: ForecastDriver) -> tuple[object, ...]:
    return row.forecast_id, row.name, row.share, row.evidence_id, row.is_other


def _uuid(value: object, field: str) -> UUID:
    if not isinstance(value, UUID):
        raise TypeError(f"{field} must be a UUID.")
    return value


def _uuid_or_none(value: object, field: str) -> None:
    if value is not None and not isinstance(value, UUID):
        raise TypeError(f"{field} must be a UUID or None.")


def _aware_datetime(value: object, field: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware.")


__all__ = ["DriverRepository", "ForecastDriverRepository"]
