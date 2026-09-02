"""Export the scoped warning feedback corpus.

The labelled dataset is an explicit allow-list, not a serialisation of ORM
objects.  That distinction is important: borrower legal names, CIN/PAN,
notes, and other personal or free-text values must never cross this export
boundary.  The export contains stable record references, persisted derived
forecast features, and the complete non-text disposition sequence.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, date
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Final, cast
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.engine import Row
from sqlalchemy.orm import Session

from covenant_radar.core.errors import AuthorizationError
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.covenant import Covenant, CovenantVersion
from covenant_radar.db.models.facility import Facility
from covenant_radar.db.models.forecast import Forecast
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.models.workflow import Disposition
from covenant_radar.db.scoping import Scope, resolve_scope
from covenant_radar.db.session import is_database_session
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal, authorize

_CSV_CONTENT_TYPE: Final[str] = "text/csv; charset=utf-8"
_CSV_FILENAME: Final[str] = "covenant-radar-labelled-warnings.csv"
_WARNING_SUBJECT_TYPE: Final[str] = "forecast"

# These are the only fields permitted in a labelled row.  In particular,
# ``Borrower.legal_name``, encrypted identity columns, notes and arbitrary
# JSON blobs are intentionally absent.
_FEATURE_COLUMNS: Final[tuple[str, ...]] = (
    "warning_id",
    "warning_type",
    "run_id",
    "covenant_reference",
    "facility_reference",
    "covenant_version_id",
    "horizon_days",
    "probability",
    "confidence",
    "below_confidence_floor",
    "projected_cross_date",
    "direction",
    "data_as_of",
    "staleness_days",
)
_EXPORT_COLUMNS: Final[tuple[str, ...]] = _FEATURE_COLUMNS + (
    "label",
    "outcome",
    "reason_code",
    "disposition_sequence",
)


@dataclass(frozen=True, slots=True)
class LabelledDatasetExport:
    """An immutable in-memory export and its deterministic CSV rendering."""

    rows: tuple[Mapping[str, object], ...]
    content: bytes
    filename: str = _CSV_FILENAME
    content_type: str = _CSV_CONTENT_TYPE

    def __post_init__(self) -> None:
        object.__setattr__(self, "rows", tuple(self.rows))
        if not isinstance(self.content, bytes):
            raise TypeError("LabelledDatasetExport.content must be bytes.")
        if not self.filename or not self.content_type:
            raise ValueError("A labelled dataset export requires file metadata.")

    def __iter__(self) -> Iterator[Mapping[str, object]]:
        return iter(self.rows)

    def __len__(self) -> int:
        return len(self.rows)

    def as_dicts(self) -> tuple[dict[str, object], ...]:
        """Return detached row dictionaries for callers that need JSON."""

        return tuple(dict(row) for row in self.rows)


class LabelledExportService:
    """Assemble a complete, scoped warning-labelled dataset."""

    def __init__(
        self,
        session: Session,
        *,
        scope_resolver: Callable[[Principal], Scope] | None = None,
    ) -> None:
        if not is_database_session(session):
            raise TypeError("LabelledExportService requires a SQLAlchemy Session.")
        if scope_resolver is not None and not callable(scope_resolver):
            raise TypeError("LabelledExportService scope_resolver must be callable.")
        self.session = session
        self.scope_resolver = scope_resolver or (
            lambda principal: resolve_scope(principal, session)
        )

    def export(
        self,
        principal: Principal,
        *,
        scope: Scope | None = None,
    ) -> LabelledDatasetExport:
        """Return one row per scoped forecast warning, including unlabelled rows."""

        self._require_principal(principal)
        resolved_scope = self._validated_scope(principal, scope)
        forecasts = tuple(self.session.execute(self._forecast_statement(resolved_scope)).all())
        forecast_ids = tuple(row[0].id for row in forecasts)
        dispositions = self._dispositions_by_forecast(forecast_ids)
        rows = tuple(
            MappingProxyType(
                _export_row(
                    cast(Row[tuple[Forecast, str, str]], row), dispositions.get(row[0].id, ())
                )
            )
            for row in forecasts
        )
        return LabelledDatasetExport(rows=rows, content=_csv_bytes(rows))

    export_dataset = export
    build = export

    def export_rows(
        self,
        principal: Principal,
        *,
        scope: Scope | None = None,
    ) -> tuple[Mapping[str, object], ...]:
        """Return only the rows for integrations that do not need CSV bytes."""

        return self.export(principal, scope=scope).rows

    rows = export_rows

    def csv_bytes(
        self,
        principal: Principal,
        *,
        scope: Scope | None = None,
    ) -> bytes:
        """Return UTF-8 CSV bytes with a stable header and row order."""

        return self.export(principal, scope=scope).content

    to_csv = csv_bytes

    def _forecast_statement(self, scope: Scope) -> Select[Any]:
        return (
            select(Forecast, Covenant.reference, Facility.reference)
            .join(CovenantVersion, CovenantVersion.id == Forecast.covenant_version_id)
            .join(Covenant, Covenant.id == CovenantVersion.covenant_id)
            .join(Facility, Facility.id == Covenant.facility_id)
            .join(Borrower, Borrower.id == Facility.borrower_id)
            .join(Portfolio, Portfolio.id == Borrower.portfolio_id)
            .where(scope.predicate(Portfolio.path))
            .order_by(Forecast.created_at.asc(), Forecast.id.asc())
        )

    def _dispositions_by_forecast(
        self,
        forecast_ids: tuple[UUID, ...],
    ) -> dict[UUID, tuple[Disposition, ...]]:
        if not forecast_ids:
            return {}
        records = (
            self.session.execute(
                select(Disposition)
                .where(
                    Disposition.subject_type == _WARNING_SUBJECT_TYPE,
                    Disposition.subject_id.in_(forecast_ids),
                )
                .order_by(Disposition.created_at.asc(), Disposition.id.asc())
            )
            .scalars()
            .all()
        )
        grouped: dict[UUID, list[Disposition]] = {}
        for record in records:
            grouped.setdefault(record.subject_id, []).append(record)
        return {subject_id: tuple(items) for subject_id, items in grouped.items()}

    def _validated_scope(self, principal: Principal, scope: Scope | None) -> Scope:
        resolved = self.scope_resolver(principal) if scope is None else scope
        if not isinstance(resolved, Scope) or resolved.principal_id != principal.id:
            raise AuthorizationError(
                "The supplied scope does not belong to the authenticated principal."
            )
        return resolved

    @staticmethod
    def _require_principal(principal: Principal) -> None:
        if not isinstance(principal, Principal):
            raise AuthorizationError("An authenticated principal is required.")
        authorize(principal, Permission.RECORD_DISPOSITION)


def export_labelled_dataset(
    session: Session,
    principal: Principal,
    *,
    scope: Scope | None = None,
    scope_resolver: Callable[[Principal], Scope] | None = None,
) -> LabelledDatasetExport:
    """Functional entry point for a one-off labelled dataset export."""

    return LabelledExportService(session, scope_resolver=scope_resolver).export(
        principal, scope=scope
    )


def _export_row(
    selected: Row[tuple[Forecast, str, str]],
    records: tuple[Disposition, ...],
) -> dict[str, object]:
    forecast, covenant_reference, facility_reference = selected
    latest = records[-1] if records else None
    label = latest.outcome if latest is not None else "unlabelled"
    row: dict[str, object] = {
        "warning_id": str(forecast.id),
        "warning_type": _WARNING_SUBJECT_TYPE,
        "run_id": str(forecast.run_id),
        "covenant_reference": covenant_reference,
        "facility_reference": facility_reference,
        "covenant_version_id": str(forecast.covenant_version_id),
        "horizon_days": forecast.horizon_days,
        "probability": _decimal(forecast.probability),
        "confidence": _decimal(forecast.confidence),
        "below_confidence_floor": forecast.below_confidence_floor,
        "projected_cross_date": _date(forecast.projected_cross_date),
        "direction": forecast.direction,
        "data_as_of": _date(forecast.data_as_of),
        "staleness_days": forecast.staleness_days,
        "label": label,
        "outcome": latest.outcome if latest is not None else None,
        "reason_code": latest.reason_code if latest is not None else None,
        "disposition_sequence": tuple(
            {
                "sequence": sequence,
                "disposition_id": str(record.id),
                "outcome": record.outcome,
                "reason_code": record.reason_code,
                "recorded_at": record.created_at.astimezone(UTC).isoformat(),
            }
            for sequence, record in enumerate(records, start=1)
        ),
    }
    if tuple(row) != _EXPORT_COLUMNS:
        raise AssertionError("Labelled export row contains an unapproved field.")
    return row


def _csv_bytes(rows: tuple[Mapping[str, object], ...]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=_EXPORT_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: _csv_value(row[key]) for key in _EXPORT_COLUMNS})
    return stream.getvalue().encode("utf-8")


def _csv_value(value: object) -> object:
    if isinstance(value, tuple | list):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "" if value is None else value


def _decimal(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


def _date(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


__all__ = [
    "LabelledDatasetExport",
    "LabelledExportService",
    "export_labelled_dataset",
]
