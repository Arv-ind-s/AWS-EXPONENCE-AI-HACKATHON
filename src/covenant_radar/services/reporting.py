"""Regulatory reporting orchestration: authorize, assemble and audit
(`spec §R-31`, `T-132`).

`reporting/crilc.py` is pure and persistence-neutral; this module is the
one place that turns a scoped principal's request into the stored facts
that function needs, and turns its deterministic result into a retained,
audited generation record. `C-60` is the retention mechanism — there is no
separate `report_generation` table, so "the generation → audited with the
parameters and the row count" (`spec §R-31`'s "every case") *is* how a
generation is retained, the same way every other write-adjacent fact in
this application is retained.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from covenant_radar.audit.events import AuditEventType
from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import get_request_id, new_request_id
from covenant_radar.core.errors import AuthorizationError, ValidationError
from covenant_radar.db.models.facility import Facility, FacilityConduct
from covenant_radar.db.repositories.borrower import BorrowerRepository
from covenant_radar.db.repositories.facility import FacilityRepository
from covenant_radar.db.scoping import Scope, ownership_path_for, resolve_scope
from covenant_radar.db.session import is_database_session
from covenant_radar.reporting.crilc import (
    CRILC_AGGREGATE_EXPOSURE_THRESHOLD,
    CrilcBorrowerFacts,
    CrilcFacilityFacts,
    CrilcReport,
    CrilcReportType,
    build_crilc_report,
)
from covenant_radar.reporting.layouts.crilc import load_crilc_layout
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal, authorize

_CRILC_REPORT_GENERATED_EVENT: str = AuditEventType.CRILC_REPORT_GENERATED.value
_CRILC_SUBJECT_TYPE = "crilc_report_generation"


class ReportAuditWriter(Protocol):
    """The append-only `C-60` boundary used by report generation."""

    def record(
        self,
        event_type: str,
        subject: object,
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        """Append one event in the caller's transaction."""


@dataclass(frozen=True, slots=True)
class CrilcGenerationResult:
    """One generation's deterministic report plus its retained provenance."""

    report: CrilcReport
    content_bytes: bytes
    content_hash: str
    layout_version: int
    generated_at: datetime
    audit_event: object


class CrilcReportService:
    """Assemble, and audit, one CRILC-shaped export for a stated as-of date.

    The service never commits — one call is meant to run inside the
    caller's existing transaction, exactly like every other service in
    this application.
    """

    def __init__(
        self,
        session: Session,
        *,
        audit: ReportAuditWriter,
        clock: Clock | None = None,
        scope_resolver: Callable[[Principal], Scope] | None = None,
        request_id: str | None = None,
    ) -> None:
        if not is_database_session(session):
            raise TypeError("CrilcReportService requires a SQLAlchemy Session.")
        if audit is None or not callable(getattr(audit, "record", None)):
            raise TypeError("CrilcReportService requires an append-only audit writer.")
        if clock is not None and not callable(getattr(clock, "now", None)):
            raise TypeError("CrilcReportService clock must expose now().")
        if scope_resolver is not None and not callable(scope_resolver):
            raise TypeError("CrilcReportService scope_resolver must be callable.")
        self.session = session
        self.audit = audit
        self.clock = clock or SystemClock()
        self.request_id = request_id or get_request_id() or new_request_id()
        self.scope_resolver = scope_resolver or (
            lambda principal: resolve_scope(principal, session)
        )
        self.borrowers = BorrowerRepository(session)
        self.facilities = FacilityRepository(session)

    def generate(
        self,
        principal: Principal,
        *,
        report_type: CrilcReportType,
        as_of_date: date,
        layout_version: int | None = None,
        layouts_dir: Path | str | None = None,
        scope: Scope | None = None,
    ) -> CrilcGenerationResult:
        """Authorize, assemble and audit one CRILC-shaped generation.

        `layout_version` pins a specific published version — a past
        generation regenerated after a newer layout ships still asks for
        its own version explicitly rather than silently picking up the
        new one. Left `None`, the latest shipped version is used.
        """
        authorize(principal, Permission.EXPORT_EVIDENCE)
        if not isinstance(report_type, CrilcReportType):
            raise ValidationError("report_type must be a CrilcReportType.", field="report_type")
        if isinstance(as_of_date, datetime) or not isinstance(as_of_date, date):
            raise ValidationError("as_of_date must be a calendar date.", field="as_of_date")
        resolved_scope = self._validated_scope(principal, scope)

        layout = load_crilc_layout(report_type, layout_version, layouts_dir=layouts_dir)
        borrower_facts = self._borrower_facts(resolved_scope)
        conduct_by_facility = self._conduct_snapshot(resolved_scope, as_of_date, borrower_facts)
        report = build_crilc_report(
            borrower_facts,
            report_type=report_type,
            as_of_date=as_of_date,
            layout=layout,
            conduct_by_facility=conduct_by_facility,
        )
        content_bytes = report.canonical_bytes()
        content_hash = report.content_hash()
        generated_at = self._now()

        event = self.audit.record(
            _CRILC_REPORT_GENERATED_EVENT,
            (_CRILC_SUBJECT_TYPE, _generation_subject_key(report_type, as_of_date, layout.version)),
            {
                "report_type": report_type.value,
                "as_of_date": as_of_date.isoformat(),
                "layout_version": layout.version,
                "threshold": format(CRILC_AGGREGATE_EXPOSURE_THRESHOLD, "f"),
                "row_count": report.row_count,
                "reconciliation": report.reconciliation.as_dict(),
                "content_hash": content_hash,
            },
            actor=principal.id,
            request_id=self.request_id,
        )
        return CrilcGenerationResult(
            report=report,
            content_bytes=content_bytes,
            content_hash=content_hash,
            layout_version=layout.version,
            generated_at=generated_at,
            audit_event=event,
        )

    def _now(self) -> datetime:
        instant = self.clock.now()
        if instant.tzinfo is None:
            raise ValueError("CrilcReportService clock returned a naive datetime.")
        return instant

    def _validated_scope(self, principal: Principal, scope: Scope | None) -> Scope:
        resolved = self.scope_resolver(principal) if scope is None else scope
        if not isinstance(resolved, Scope) or resolved.principal_id != principal.id:
            raise AuthorizationError(
                "The supplied scope does not belong to the authenticated principal."
            )
        return resolved

    def _borrower_facts(self, scope: Scope) -> tuple[CrilcBorrowerFacts, ...]:
        borrowers = self.borrowers.list(scope=scope)
        facilities = self.facilities.list(scope=scope)
        by_borrower: dict[UUID, list[Facility]] = {}
        for facility in facilities:
            by_borrower.setdefault(facility.borrower_id, []).append(facility)

        result: list[CrilcBorrowerFacts] = []
        for borrower in borrowers:
            facility_facts = tuple(
                CrilcFacilityFacts(
                    facility_id=row.id,
                    reference=row.reference,
                    sanctioned_limit=row.sanctioned_limit,
                    currency=row.currency,
                    outstanding=row.outstanding,
                    effective_from=row.effective_from,
                    effective_to=row.effective_to,
                )
                for row in by_borrower.get(borrower.id, ())
            )
            result.append(
                CrilcBorrowerFacts(
                    borrower_id=borrower.id,
                    reference=borrower.reference,
                    legal_name=borrower.legal_name,
                    industry_code=borrower.industry_code,
                    constitution=borrower.constitution,
                    facilities=facility_facts,
                )
            )
        return tuple(result)

    def _conduct_snapshot(
        self,
        scope: Scope,
        as_of_date: date,
        borrower_facts: tuple[CrilcBorrowerFacts, ...],
    ) -> dict[object, FacilityConduct]:
        facility_ids = tuple(
            facility.facility_id
            for borrower in borrower_facts
            for facility in borrower.effective_facilities(as_of_date)
        )
        if not facility_ids:
            return {}
        ownership = ownership_path_for(FacilityConduct)
        statement = ownership.apply(select(FacilityConduct)).where(
            scope.predicate(ownership.path_column),
            FacilityConduct.facility_id.in_(facility_ids),
            FacilityConduct.as_of_date == as_of_date,
        )
        rows = (
            self.session.execute(
                statement.order_by(FacilityConduct.facility_id, FacilityConduct.id)
            )
            .scalars()
            .all()
        )
        return {row.facility_id: row for row in rows}


def _generation_subject_key(
    report_type: CrilcReportType, as_of_date: date, layout_version: int
) -> str:
    """A stable, human-readable subject key: same report identity, same key.

    Every generation for the same `(report_type, as_of_date, layout_version)`
    resolves to the same audit subject, so an inspector can find every
    generation of one report by the subject alone rather than by
    correlating unrelated random ids.
    """
    return f"{report_type.value}:{as_of_date.isoformat()}:v{layout_version}"


__all__ = [
    "CrilcGenerationResult",
    "CrilcReportService",
    "ReportAuditWriter",
]
