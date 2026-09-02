"""Scoped persistence for risk-view overrides.

``override_record`` is intentionally polymorphic and therefore has no
portfolio foreign key of its own.  A repository for it must not fall back to
an unscoped ``subject_id`` lookup: the subject is resolved through its owning
portfolio before an override is returned.  This module keeps that rule in one
adapter so both the service and the web surface use the same boundary.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

from sqlalchemy import Select, and_, exists, or_, select
from sqlalchemy.orm import Session

from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.covenant import Covenant, CovenantTest, CovenantVersion
from covenant_radar.db.models.document import Document
from covenant_radar.db.models.facility import Facility
from covenant_radar.db.models.forecast import Forecast, ForecastRun, Simulation, TriageEntry
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.models.workflow import Case, Memo, OverrideRecord
from covenant_radar.db.scoping import Scope
from covenant_radar.db.session import is_database_session

_SUBJECT_TYPE_MAX_LENGTH: Final[int] = 50

# These are the polymorphic subject families for which this repository can
# prove portfolio ownership.  The service currently exposes the first three
# through the why-panel; the remaining entries preserve safe reads for other
# existing override producers and future workflow screens.
SUPPORTED_OVERRIDE_SUBJECT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "borrower",
        "case",
        "covenant",
        "covenant_test",
        "covenant_version",
        "document",
        "forecast",
        "forecast_run",
        "memo",
        "simulation",
        "triage_entry",
    }
)


@dataclass(frozen=True, slots=True)
class OverrideSubjectMetadata:
    """Decision metadata available from the persisted subject."""

    threshold_snapshot_id: UUID | None = None
    model_version: str | None = None


class OverrideRepository:
    """Append-only override rows with mandatory portfolio scoping."""

    def __init__(self, session: Session) -> None:
        if not is_database_session(session):
            raise TypeError("OverrideRepository requires a SQLAlchemy Session.")
        self.session = session

    def add(self, record: OverrideRecord) -> OverrideRecord:
        """Stage and flush one validated override record.

        The enclosing service owns the transaction and may roll the savepoint
        back if the audit write fails.  This method deliberately has no
        update or delete counterpart.
        """

        if not isinstance(record, OverrideRecord):
            raise TypeError("OverrideRepository.add requires an OverrideRecord.")
        self.session.add(record)
        self.session.flush()
        return record

    save = add

    def get(self, override_id: UUID, *, scope: Scope) -> OverrideRecord | None:
        """Return an override only when its polymorphic subject is visible."""

        _uuid(override_id, "override_id")
        statement: Select[tuple[OverrideRecord]] = select(OverrideRecord).where(
            OverrideRecord.id == override_id,
            self._visible_override(scope),
        )
        return self.session.execute(statement).scalars().one_or_none()

    def list(
        self,
        *,
        scope: Scope,
        subject_type: str | None = None,
    ) -> Sequence[OverrideRecord]:
        """List all visible overrides in deterministic append order."""

        validated_type = _subject_type(subject_type) if subject_type is not None else None
        statement: Select[tuple[OverrideRecord]] = select(OverrideRecord).where(
            self._visible_override(scope)
        )
        if validated_type is not None:
            statement = statement.where(OverrideRecord.subject_type == validated_type)
        statement = statement.order_by(OverrideRecord.created_at, OverrideRecord.id)
        return tuple(self.session.execute(statement).scalars().all())

    def for_subject(
        self,
        subject_type: str,
        subject_id: UUID,
        *,
        scope: Scope,
    ) -> tuple[OverrideRecord, ...]:
        """Return a visible subject's complete override sequence."""

        validated_type = _subject_type(subject_type)
        _uuid(subject_id, "subject_id")
        statement: Select[tuple[OverrideRecord]] = select(OverrideRecord).where(
            OverrideRecord.subject_type == validated_type,
            OverrideRecord.subject_id == subject_id,
            self._subject_visible(validated_type, subject_id, scope),
        )
        statement = statement.order_by(OverrideRecord.created_at, OverrideRecord.id)
        return tuple(self.session.execute(statement).scalars().all())

    list_for_subject = for_subject

    def latest_for_subject(
        self,
        subject_type: str,
        subject_id: UUID,
        *,
        scope: Scope,
    ) -> OverrideRecord | None:
        """Return the newest visible override without changing history."""

        validated_type = _subject_type(subject_type)
        _uuid(subject_id, "subject_id")
        statement: Select[tuple[OverrideRecord]] = (
            select(OverrideRecord)
            .where(
                OverrideRecord.subject_type == validated_type,
                OverrideRecord.subject_id == subject_id,
                self._subject_visible(validated_type, subject_id, scope),
            )
            .order_by(OverrideRecord.created_at.desc(), OverrideRecord.id.desc())
            .limit(1)
        )
        return self.session.execute(statement).scalars().one_or_none()

    latest = latest_for_subject

    def subject_visible(self, subject_type: str, subject_id: UUID, *, scope: Scope) -> bool:
        """Return whether a subject exists inside the caller's portfolio scope."""

        validated_type = _subject_type(subject_type)
        _uuid(subject_id, "subject_id")
        statement = select(self._subject_visible(validated_type, subject_id, scope))
        return bool(self.session.scalar(statement))

    is_subject_visible = subject_visible

    def subject_metadata(
        self,
        subject_type: str,
        subject_id: UUID,
        *,
        scope: Scope,
    ) -> OverrideSubjectMetadata:
        """Read version metadata after the subject has passed its scope check."""

        validated_type = _subject_type(subject_type)
        _uuid(subject_id, "subject_id")
        if not self.subject_visible(validated_type, subject_id, scope=scope):
            return OverrideSubjectMetadata()
        if validated_type != "forecast":
            return OverrideSubjectMetadata()
        row = self.session.execute(
            select(ForecastRun.threshold_snapshot_id, ForecastRun.model_version)
            .join(Forecast, Forecast.run_id == ForecastRun.id)
            .where(Forecast.id == subject_id)
        ).one_or_none()
        if row is None:
            return OverrideSubjectMetadata()
        return OverrideSubjectMetadata(
            threshold_snapshot_id=row[0],
            model_version=row[1],
        )

    def _visible_override(self, scope: Scope) -> Any:
        if not isinstance(scope, Scope):
            raise TypeError("Override repository reads require a Scope.")
        clauses = [
            and_(
                OverrideRecord.subject_type == subject_type,
                self._subject_visible(subject_type, OverrideRecord.subject_id, scope),
            )
            for subject_type in sorted(SUPPORTED_OVERRIDE_SUBJECT_TYPES)
        ]
        return or_(*clauses)

    @staticmethod
    def _subject_visible(subject_type: str, subject_id: Any, scope: Scope) -> Any:
        if not isinstance(scope, Scope):
            raise TypeError("Override repository reads require a Scope.")
        if subject_type == "borrower":
            statement = (
                select(Borrower.id)
                .join(Portfolio, Borrower.portfolio_id == Portfolio.id)
                .where(Borrower.id == subject_id, scope.predicate(Portfolio.path))
            )
        elif subject_type == "covenant_test":
            statement = (
                select(CovenantTest.id)
                .join(CovenantVersion, CovenantTest.covenant_version_id == CovenantVersion.id)
                .join(Covenant, CovenantVersion.covenant_id == Covenant.id)
                .join(Facility, Covenant.facility_id == Facility.id)
                .join(Borrower, Facility.borrower_id == Borrower.id)
                .join(Portfolio, Borrower.portfolio_id == Portfolio.id)
                .where(CovenantTest.id == subject_id, scope.predicate(Portfolio.path))
            )
        elif subject_type == "forecast":
            statement = (
                select(Forecast.id)
                .join(CovenantVersion, Forecast.covenant_version_id == CovenantVersion.id)
                .join(Covenant, CovenantVersion.covenant_id == Covenant.id)
                .join(Facility, Covenant.facility_id == Facility.id)
                .join(Borrower, Facility.borrower_id == Borrower.id)
                .join(Portfolio, Borrower.portfolio_id == Portfolio.id)
                .where(Forecast.id == subject_id, scope.predicate(Portfolio.path))
            )
        elif subject_type == "covenant_version":
            statement = (
                select(CovenantVersion.id)
                .join(Covenant, CovenantVersion.covenant_id == Covenant.id)
                .join(Facility, Covenant.facility_id == Facility.id)
                .join(Borrower, Facility.borrower_id == Borrower.id)
                .join(Portfolio, Borrower.portfolio_id == Portfolio.id)
                .where(CovenantVersion.id == subject_id, scope.predicate(Portfolio.path))
            )
        elif subject_type == "covenant":
            statement = (
                select(Covenant.id)
                .join(Facility, Covenant.facility_id == Facility.id)
                .join(Borrower, Facility.borrower_id == Borrower.id)
                .join(Portfolio, Borrower.portfolio_id == Portfolio.id)
                .where(Covenant.id == subject_id, scope.predicate(Portfolio.path))
            )
        elif subject_type == "document":
            statement = (
                select(Document.id)
                .join(Borrower, Document.borrower_id == Borrower.id)
                .join(Portfolio, Borrower.portfolio_id == Portfolio.id)
                .where(Document.id == subject_id, scope.predicate(Portfolio.path))
            )
        elif subject_type == "case":
            statement = (
                select(Case.id)
                .join(Borrower, Case.borrower_id == Borrower.id)
                .join(Portfolio, Borrower.portfolio_id == Portfolio.id)
                .where(Case.id == subject_id, scope.predicate(Portfolio.path))
            )
        elif subject_type == "memo":
            statement = (
                select(Memo.id)
                .join(Borrower, Memo.borrower_id == Borrower.id)
                .join(Portfolio, Borrower.portfolio_id == Portfolio.id)
                .where(Memo.id == subject_id, scope.predicate(Portfolio.path))
            )
        elif subject_type == "triage_entry":
            statement = (
                select(TriageEntry.id)
                .join(Borrower, TriageEntry.borrower_id == Borrower.id)
                .join(Portfolio, Borrower.portfolio_id == Portfolio.id)
                .where(TriageEntry.id == subject_id, scope.predicate(Portfolio.path))
            )
        elif subject_type == "simulation":
            statement = (
                select(Simulation.id)
                .join(Forecast, Simulation.forecast_id == Forecast.id)
                .join(CovenantVersion, Forecast.covenant_version_id == CovenantVersion.id)
                .join(Covenant, CovenantVersion.covenant_id == Covenant.id)
                .join(Facility, Covenant.facility_id == Facility.id)
                .join(Borrower, Facility.borrower_id == Borrower.id)
                .join(Portfolio, Borrower.portfolio_id == Portfolio.id)
                .where(Simulation.id == subject_id, scope.predicate(Portfolio.path))
            )
        elif subject_type == "forecast_run":
            statement = (
                select(ForecastRun.id)
                .join(Forecast, Forecast.run_id == ForecastRun.id)
                .join(CovenantVersion, Forecast.covenant_version_id == CovenantVersion.id)
                .join(Covenant, CovenantVersion.covenant_id == Covenant.id)
                .join(Facility, Covenant.facility_id == Facility.id)
                .join(Borrower, Facility.borrower_id == Borrower.id)
                .join(Portfolio, Borrower.portfolio_id == Portfolio.id)
                .where(ForecastRun.id == subject_id, scope.predicate(Portfolio.path))
            )
        else:
            raise ValueError(
                f"Unsupported override subject type {subject_type!r}; valid types are: "
                f"{', '.join(sorted(SUPPORTED_OVERRIDE_SUBJECT_TYPES))}."
            )
        return exists(statement)


def _subject_type(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Override subject_type must be non-empty text.")
    candidate = value.strip()
    if len(candidate) > _SUBJECT_TYPE_MAX_LENGTH:
        raise ValueError(
            f"Override subject_type must be at most {_SUBJECT_TYPE_MAX_LENGTH} characters."
        )
    if candidate not in SUPPORTED_OVERRIDE_SUBJECT_TYPES:
        raise ValueError(
            f"Unsupported override subject type {candidate!r}; valid types are: "
            f"{', '.join(sorted(SUPPORTED_OVERRIDE_SUBJECT_TYPES))}."
        )
    return candidate


def _uuid(value: object, field_name: str) -> UUID:
    if not isinstance(value, UUID):
        raise TypeError(f"{field_name} must be a UUID.")
    return value


__all__ = [
    "OverrideRepository",
    "OverrideSubjectMetadata",
    "SUPPORTED_OVERRIDE_SUBJECT_TYPES",
]
