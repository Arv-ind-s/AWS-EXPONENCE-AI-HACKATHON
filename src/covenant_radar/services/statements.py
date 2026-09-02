"""Application service for atomic statement import, restatement and
quarantine resolution (`T-025`, `T-026`).

Mirrors `services/ingestion.py`'s `SignalIngestionService` shape: the
service owns the database transaction's write set. A mapping is resolved
and the source file is read and column-checked *before* any row is
written; every accepted row becomes a `FinancialPeriod` plus its
`StatementLineValue`s and one `FieldProvenance`; every rejected row is sent
to quarantine as a persisted `QuarantineRow`, never merely logged. The
whole batch is idempotent on the SHA-256 of the uploaded bytes: re-running
the identical import returns the first run's report and writes nothing.

`T-026` adds three more write paths on the same service: `restate_period`
supersedes one existing `FinancialPeriod` with a new version rather than
editing it, and only *flags* the covenant tests that read the superseded
period — it never recomputes one itself, exactly as `spec §R-03.b`
requires. `correct_quarantine_row` and `reject_quarantine_row` resolve one
`QuarantineRow` each, and `trace_line_value` answers "where did this number
come from" for any stored `StatementLineValue`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from covenant_radar.audit.events import AuditEventType
from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import get_request_id, new_request_id
from covenant_radar.core.errors import AuthorizationError, Conflict, NotFound, ValidationError
from covenant_radar.core.ids import new_id
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.covenant import CovenantTest
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.models.statements import (
    FieldProvenance,
    FinancialPeriod,
    ImportBatch,
    ImportMapping,
    QuarantineRow,
    StatementLineValue,
)
from covenant_radar.db.scoping import Scope, resolve_scope
from covenant_radar.db.session import is_database_session
from covenant_radar.domain.statements.chart import DEFAULT_IDENTITY_TOLERANCE, Chart, default_chart
from covenant_radar.ingestion.statements.mapping import ImportMappingSpec, parse_mapping_spec
from covenant_radar.ingestion.statements.provenance import (
    ProvenanceTrace,
    correction_row_reference,
    correction_transform_note,
    restatement_transform_note,
)
from covenant_radar.ingestion.statements.readers import Cell, read_rows
from covenant_radar.ingestion.statements.restate import (
    DependentTest,
    RestatementResult,
    resolve_restatement_row,
    validate_reason,
)
from covenant_radar.ingestion.statements.validate import (
    PreparedStatementRow,
    QuarantinedStatementRow,
    TotalsDiscrepancy,
    check_columns,
    prepare,
    reconcile_totals,
)
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal, PrincipalKind, authorize

_REQUEST_ID_MAX_LENGTH = 40
_ROW_REFERENCE_MAX_LENGTH = 50
_UNIT_OUT = "crore"


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
class QuarantineSummary:
    """A JSON-safe, persisted-report view of one quarantined row."""

    row_number: int
    rule_failed: str
    message: str

    def as_dict(self) -> dict[str, object]:
        return {
            "row_number": self.row_number,
            "rule_failed": self.rule_failed,
            "message": self.message,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> QuarantineSummary:
        return cls(
            row_number=_as_int(data["row_number"]),
            rule_failed=str(data["rule_failed"]),
            message=str(data["message"]),
        )


@dataclass(frozen=True, slots=True)
class DiscrepancySummary:
    """A JSON-safe, persisted-report view of one totals-row discrepancy."""

    line_code: str
    expected: str
    actual: str
    difference: str

    def as_dict(self) -> dict[str, object]:
        return {
            "line_code": self.line_code,
            "expected": self.expected,
            "actual": self.actual,
            "difference": self.difference,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> DiscrepancySummary:
        return cls(
            line_code=str(data["line_code"]),
            expected=str(data["expected"]),
            actual=str(data["actual"]),
            difference=str(data["difference"]),
        )


@dataclass(frozen=True, slots=True)
class StatementImportReport:
    """The reconciled, JSON-round-trippable result of one import run.

    Persisted verbatim into `ImportBatch.report`: an idempotent replay
    reconstructs this object from that JSON alone, with no second query.
    """

    batch_id: UUID
    mapping_name: str
    mapping_version: int
    source_type: str
    content_hash: str
    received: int
    accepted: int
    quarantined: int
    totals_rows: int
    quarantine: tuple[QuarantineSummary, ...]
    discrepancies: tuple[DiscrepancySummary, ...]
    reconciled: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "batch_id": str(self.batch_id),
            "mapping_name": self.mapping_name,
            "mapping_version": self.mapping_version,
            "source_type": self.source_type,
            "content_hash": self.content_hash,
            "received": self.received,
            "accepted": self.accepted,
            "quarantined": self.quarantined,
            "totals_rows": self.totals_rows,
            "quarantine": [item.as_dict() for item in self.quarantine],
            "discrepancies": [item.as_dict() for item in self.discrepancies],
            "reconciled": self.reconciled,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> StatementImportReport:
        return cls(
            batch_id=UUID(str(data["batch_id"])),
            mapping_name=str(data["mapping_name"]),
            mapping_version=_as_int(data["mapping_version"]),
            source_type=str(data["source_type"]),
            content_hash=str(data["content_hash"]),
            received=_as_int(data["received"]),
            accepted=_as_int(data["accepted"]),
            quarantined=_as_int(data["quarantined"]),
            totals_rows=_as_int(data["totals_rows"]),
            quarantine=tuple(
                QuarantineSummary.from_dict(item) for item in _as_mapping_list(data["quarantine"])
            ),
            discrepancies=tuple(
                DiscrepancySummary.from_dict(item)
                for item in _as_mapping_list(data["discrepancies"])
            ),
            reconciled=bool(data["reconciled"]),
        )


class StatementImportService:
    """Validate, map, quarantine, and persist one statement import batch."""

    def __init__(
        self,
        session: Session,
        *,
        audit: AuditWriter,
        clock: Clock | None = None,
        request_id: str | None = None,
        chart: Chart | None = None,
        tolerance: Decimal = DEFAULT_IDENTITY_TOLERANCE,
    ) -> None:
        if not is_database_session(session):
            raise TypeError("StatementImportService requires a SQLAlchemy Session.")
        if audit is None or not callable(getattr(audit, "record", None)):
            raise TypeError("StatementImportService requires an append-only audit writer.")
        if clock is not None and not callable(getattr(clock, "now", None)):
            raise TypeError("StatementImportService clock must expose now().")
        if not isinstance(tolerance, Decimal):
            raise TypeError("StatementImportService tolerance must be a Decimal.")
        self.session = session
        self.audit = audit
        self.clock = clock or SystemClock()
        self.chart = chart or default_chart()
        self.tolerance = tolerance
        self.request_id = request_id or get_request_id() or new_request_id()
        if (
            not isinstance(self.request_id, str)
            or not 1 <= len(self.request_id) <= _REQUEST_ID_MAX_LENGTH
        ):
            raise ValueError(
                "Statement import request_id must be between 1 and "
                f"{_REQUEST_ID_MAX_LENGTH} characters."
            )

    def import_statements(
        self,
        principal: Principal,
        *,
        source_type: str,
        content: bytes,
        mapping_name: str,
        mapping_version: int | None = None,
        source_reference: str | None = None,
        scope: Scope | None = None,
        request_id: str | None = None,
    ) -> StatementImportReport:
        """Ingest one complete source file.

        Does not commit: the caller's unit of work is the outer transaction
        boundary, exactly as `SignalIngestionService.ingest` documents.
        """
        resolved_scope = self._write_context(principal, scope)
        resolved_request_id = self._request_id(request_id)
        now = self._now()
        actor_id = self._actor(principal)

        if not isinstance(content, bytes | bytearray):
            raise TypeError("Statement import content must be bytes.")
        content_bytes = bytes(content)
        content_hash = hashlib.sha256(content_bytes).hexdigest()

        with self.session.begin_nested():
            existing = self.session.scalar(
                select(ImportBatch).where(ImportBatch.content_hash == content_hash)
            )
            if existing is not None:
                return StatementImportReport.from_dict(existing.report)

            mapping = self._resolve_mapping(mapping_name, mapping_version)
            spec = parse_mapping_spec(mapping.spec, chart=self.chart)

            row_batch = read_rows(source_type, content_bytes)
            check_columns(frozenset(row_batch.columns), spec)

            batch = ImportBatch(
                id=new_id(),
                source_type=source_type,
                source_reference=source_reference,
                mapping_id=mapping.id,
                content_hash=content_hash,
                started_at=now,
                finished_at=None,
                row_count=len(row_batch.rows),
                accepted_count=0,
                quarantined_count=0,
                state="completed",
                report={},
                created_at=now,
                updated_at=now,
                created_by_id=actor_id,
                updated_by_id=actor_id,
                request_id=resolved_request_id,
            )
            self.session.add(batch)
            self.session.flush()

            prepared_batch = prepare(row_batch.rows, spec, self.chart, tolerance=self.tolerance)
            quarantined: list[QuarantinedStatementRow] = list(prepared_batch.quarantined)

            borrower_ids = self._resolve_borrowers(
                {row.resolved.borrower_key for row in prepared_batch.prepared}, resolved_scope
            )

            accepted: list[tuple[UUID, PreparedStatementRow]] = []
            seen_periods: set[tuple[UUID, str]] = set()
            for row in prepared_batch.prepared:
                borrower_id = borrower_ids.get(row.resolved.borrower_key)
                if borrower_id is None:
                    quarantined.append(
                        QuarantinedStatementRow(
                            row_number=row.row_number,
                            raw=row.raw,
                            rule_failed="unknown_borrower",
                            message=(
                                f"Borrower key {row.resolved.borrower_key!r} does not match any "
                                "borrower in the caller's portfolio scope."
                            ),
                        )
                    )
                    continue
                period_key = (borrower_id, row.resolved.fy_label)
                if period_key in seen_periods:
                    quarantined.append(
                        QuarantinedStatementRow(
                            row_number=row.row_number,
                            raw=row.raw,
                            rule_failed="duplicate_row_in_batch",
                            message=(
                                f"Another row in this batch already supplies "
                                f"{row.resolved.fy_label!r} for this borrower."
                            ),
                        )
                    )
                    continue
                if self._financial_period_exists(borrower_id, row.resolved.fy_label):
                    quarantined.append(
                        QuarantinedStatementRow(
                            row_number=row.row_number,
                            raw=row.raw,
                            rule_failed="financial_period_already_exists",
                            message=(
                                f"A financial period already exists for this borrower at "
                                f"{row.resolved.fy_label!r}; restatement is not part of "
                                "this import."
                            ),
                        )
                    )
                    continue
                seen_periods.add(period_key)
                accepted.append((borrower_id, row))

            for borrower_id, row in accepted:
                self._persist_accepted_row(
                    borrower_id,
                    row,
                    batch=batch,
                    spec=spec,
                    source_type=source_type,
                    source_reference=source_reference,
                    mapping_version=mapping.version,
                    now=now,
                    actor_id=actor_id,
                    request_id=resolved_request_id,
                )

            discrepancies = reconcile_totals(
                tuple(row for _, row in accepted), prepared_batch.totals, tolerance=self.tolerance
            )

            for record in quarantined:
                self.session.add(
                    QuarantineRow(
                        id=new_id(),
                        batch_id=batch.id,
                        row_number=record.row_number,
                        raw=_json_safe(record.raw),
                        rule_failed=record.rule_failed,
                        message=record.message,
                        resolved_at=None,
                        resolved_by_id=None,
                        resolution=None,
                        created_at=now,
                        updated_at=now,
                        created_by_id=actor_id,
                        updated_by_id=actor_id,
                        request_id=resolved_request_id,
                    )
                )
                self.audit.record(
                    AuditEventType.STATEMENT_ROW_QUARANTINED.value,
                    ("import_batch", batch.id),
                    {
                        "row_number": record.row_number,
                        "rule_failed": record.rule_failed,
                        "message": record.message,
                    },
                    actor=actor_id,
                    request_id=resolved_request_id,
                )

            report = StatementImportReport(
                batch_id=batch.id,
                mapping_name=mapping.name,
                mapping_version=mapping.version,
                source_type=source_type,
                content_hash=content_hash,
                received=prepared_batch.received,
                accepted=len(accepted),
                quarantined=len(quarantined),
                totals_rows=len(prepared_batch.totals),
                quarantine=tuple(
                    QuarantineSummary(
                        row_number=record.row_number,
                        rule_failed=record.rule_failed,
                        message=record.message,
                    )
                    for record in quarantined
                ),
                discrepancies=tuple(_discrepancy_summary(item) for item in discrepancies),
                reconciled=not discrepancies,
            )

            batch.accepted_count = report.accepted
            batch.quarantined_count = report.quarantined
            batch.finished_at = now
            batch.report = report.as_dict()

            self.audit.record(
                AuditEventType.STATEMENT_IMPORT_COMPLETED.value,
                ("import_batch", batch.id),
                report.as_dict(),
                actor=actor_id,
                request_id=resolved_request_id,
            )

            return report

    def restate_period(
        self,
        principal: Principal,
        *,
        source_type: str,
        content: bytes,
        mapping_name: str,
        mapping_version: int | None = None,
        source_reference: str | None = None,
        reason: str,
        scope: Scope | None = None,
        request_id: str | None = None,
    ) -> RestatementResult:
        """Restate one existing financial period from one corrected row.

        The corrected row is validated through the same mapping and chart
        pipeline as a plain import (`import_statements`), but it must name
        exactly the borrower and financial period an existing, live
        `FinancialPeriod` already covers — restatement corrects a period
        that exists; it does not create one, and a missing target is
        refused rather than silently treated as a fresh import.

        A new `FinancialPeriod` is created at `version + 1` and the old row
        is superseded, never edited (`plan.md §5.3`). Every `CovenantTest`
        that read the superseded period is looked up and returned as
        flagged — this method never recomputes one itself, so a
        restatement with dependent tests can never silently leave a stale
        verdict looking current.
        """
        resolved_scope = self._write_context(
            principal, scope, permission=Permission.CORRECT_SOURCE_DATA
        )
        if principal.kind is not PrincipalKind.USER:
            raise AuthorizationError("Restatement requires an authenticated user principal.")
        resolved_request_id = self._request_id(request_id)
        now = self._now()
        actor_id = self._actor(principal)
        clean_reason = validate_reason(reason)

        if not isinstance(content, bytes | bytearray):
            raise TypeError("Restatement content must be bytes.")
        content_bytes = bytes(content)

        with self.session.begin_nested():
            mapping = self._resolve_mapping(mapping_name, mapping_version)
            spec = parse_mapping_spec(mapping.spec, chart=self.chart)

            row_batch = read_rows(source_type, content_bytes)
            check_columns(frozenset(row_batch.columns), spec)
            if len(row_batch.rows) != 1:
                raise ValidationError(
                    "A restatement corrects exactly one row.", field="content"
                )
            resolved_row = resolve_restatement_row(
                row_batch.rows[0], spec, self.chart, tolerance=self.tolerance
            )

            borrower_ids = self._resolve_borrowers(
                {resolved_row.resolved.borrower_key}, resolved_scope
            )
            borrower_id = borrower_ids.get(resolved_row.resolved.borrower_key)
            if borrower_id is None:
                raise NotFound(
                    f"Borrower key {resolved_row.resolved.borrower_key!r} does not match "
                    "any borrower in the caller's portfolio scope."
                )
            fy_label = resolved_row.resolved.fy_label
            old_period = self._live_financial_period(borrower_id, fy_label)
            if old_period is None:
                raise NotFound(
                    f"No existing financial period for {fy_label!r} to restate; "
                    "import it first."
                )

            content_hash = hashlib.sha256(
                content_bytes
                + b"|restate|"
                + str(old_period.id).encode("ascii")
                + b"|"
                + resolved_request_id.encode("utf-8")
            ).hexdigest()
            batch = ImportBatch(
                id=new_id(),
                source_type=source_type,
                source_reference=source_reference,
                mapping_id=mapping.id,
                content_hash=content_hash,
                started_at=now,
                finished_at=now,
                row_count=1,
                accepted_count=1,
                quarantined_count=0,
                state="completed",
                report={
                    "restatement": True,
                    "reason": clean_reason,
                    "previous_period_id": str(old_period.id),
                },
                created_at=now,
                updated_at=now,
                created_by_id=actor_id,
                updated_by_id=actor_id,
                request_id=resolved_request_id,
            )
            self.session.add(batch)
            self.session.flush()

            prepared = PreparedStatementRow(
                row_number=1,
                resolved=resolved_row.resolved,
                normalisation=resolved_row.normalisation,
                raw=row_batch.rows[0],
            )
            new_period = self._persist_accepted_row(
                borrower_id,
                prepared,
                batch=batch,
                spec=spec,
                source_type=source_type,
                source_reference=source_reference,
                mapping_version=mapping.version,
                now=now,
                actor_id=actor_id,
                request_id=resolved_request_id,
                version=old_period.version + 1,
                transform_note=restatement_transform_note(
                    spec_summary=_transform_note(spec), reason=clean_reason
                ),
            )
            old_period.superseded_by_id = new_period.id
            old_period.updated_at = now
            old_period.updated_by_id = actor_id
            old_period.request_id = resolved_request_id
            self.session.flush()

            flagged = self._dependent_tests(old_period.id)

            self.audit.record(
                AuditEventType.STATEMENT_PERIOD_RESTATED.value,
                ("financial_period", new_period.id),
                {
                    "borrower_id": str(borrower_id),
                    "fy_label": fy_label,
                    "previous_period_id": str(old_period.id),
                    "previous_version": old_period.version,
                    "new_period_id": str(new_period.id),
                    "new_version": new_period.version,
                    "reason": clean_reason,
                    "flagged_covenant_test_ids": [
                        str(item.covenant_test_id) for item in flagged
                    ],
                },
                actor=actor_id,
                request_id=resolved_request_id,
            )

            return RestatementResult(
                borrower_id=borrower_id,
                fy_label=fy_label,
                previous_period_id=old_period.id,
                previous_version=old_period.version,
                new_period_id=new_period.id,
                new_version=new_period.version,
                reason=clean_reason,
                flagged_tests=flagged,
            )

    def correct_quarantine_row(
        self,
        principal: Principal,
        quarantine_row_id: UUID,
        *,
        corrected_raw: Mapping[str, Cell],
        reason: str,
        scope: Scope | None = None,
        request_id: str | None = None,
    ) -> FinancialPeriod:
        """Correct and re-submit one quarantined row.

        The row is re-validated through the mapping and chart of the batch
        it was originally quarantined in — a corrected row that still
        fails is refused for the same reason an import row would be, and
        the quarantine entry stays open. A row whose borrower and period
        already have a live `FinancialPeriod` is refused in favour of
        `restate_period`: correction loads a period that does not yet
        exist, it does not re-open one that does.
        """
        resolved_scope = self._write_context(
            principal, scope, permission=Permission.RESOLVE_QUARANTINE
        )
        if principal.kind is not PrincipalKind.USER:
            raise AuthorizationError(
                "Quarantine correction requires an authenticated user principal."
            )
        resolved_request_id = self._request_id(request_id)
        now = self._now()
        clean_reason = validate_reason(reason)

        with self.session.begin_nested():
            quarantine_row = self.session.get(QuarantineRow, quarantine_row_id)
            if quarantine_row is None:
                raise NotFound(f"Quarantine row {quarantine_row_id} was not found.")
            if quarantine_row.resolved_at is not None:
                raise Conflict("This quarantine row has already been resolved.")
            batch = self.session.get(ImportBatch, quarantine_row.batch_id)
            if batch is None:
                raise NotFound(f"Import batch {quarantine_row.batch_id} was not found.")
            mapping = self.session.get(ImportMapping, batch.mapping_id)
            if mapping is None:
                raise NotFound(f"Import mapping {batch.mapping_id} was not found.")
            spec = parse_mapping_spec(mapping.spec, chart=self.chart)

            resolved_row = resolve_restatement_row(
                corrected_raw, spec, self.chart, tolerance=self.tolerance
            )
            borrower_ids = self._resolve_borrowers(
                {resolved_row.resolved.borrower_key}, resolved_scope
            )
            borrower_id = borrower_ids.get(resolved_row.resolved.borrower_key)
            if borrower_id is None:
                raise NotFound(
                    f"Borrower key {resolved_row.resolved.borrower_key!r} does not match "
                    "any borrower in the caller's portfolio scope."
                )
            fy_label = resolved_row.resolved.fy_label
            if self._live_financial_period(borrower_id, fy_label) is not None:
                raise Conflict(
                    f"A financial period already exists for {fy_label!r}; use "
                    "restate_period instead of a quarantine correction."
                )

            prepared = PreparedStatementRow(
                row_number=quarantine_row.row_number,
                resolved=resolved_row.resolved,
                normalisation=resolved_row.normalisation,
                raw=corrected_raw,
            )
            new_period = self._persist_accepted_row(
                borrower_id,
                prepared,
                batch=batch,
                spec=spec,
                source_type=batch.source_type,
                source_reference=batch.source_reference,
                mapping_version=mapping.version,
                now=now,
                actor_id=principal.id,
                request_id=resolved_request_id,
                row_reference=correction_row_reference(quarantine_row.row_number),
                transform_note=correction_transform_note(
                    spec_summary=_transform_note(spec),
                    quarantine_row_id=quarantine_row.id,
                    original_rule_failed=quarantine_row.rule_failed,
                    reason=clean_reason,
                ),
            )
            quarantine_row.resolved_at = now
            quarantine_row.resolved_by_id = principal.id
            quarantine_row.resolution = f"corrected: {clean_reason}"
            quarantine_row.updated_at = now
            quarantine_row.updated_by_id = principal.id
            quarantine_row.request_id = resolved_request_id
            self.session.flush()

            self.audit.record(
                AuditEventType.STATEMENT_QUARANTINE_ROW_CORRECTED.value,
                ("quarantine_row", quarantine_row.id),
                {
                    "quarantine_row_id": str(quarantine_row.id),
                    "batch_id": str(batch.id),
                    "row_number": quarantine_row.row_number,
                    "original_rule_failed": quarantine_row.rule_failed,
                    "new_period_id": str(new_period.id),
                    "borrower_id": str(borrower_id),
                    "fy_label": fy_label,
                    "reason": clean_reason,
                },
                actor=principal.id,
                request_id=resolved_request_id,
            )
            return new_period

    def reject_quarantine_row(
        self,
        principal: Principal,
        quarantine_row_id: UUID,
        *,
        reason: str,
        scope: Scope | None = None,
        request_id: str | None = None,
    ) -> QuarantineRow:
        """Close one quarantined row without loading it.

        The row is retained, never deleted: `resolved_at`, `resolved_by_id`
        and `resolution` record the outcome, so it stays visible to a
        later provenance query or audit review for as long as the
        surrounding retention policy keeps `quarantine_row` at all.
        """
        self._write_context(principal, scope, permission=Permission.RESOLVE_QUARANTINE)
        if principal.kind is not PrincipalKind.USER:
            raise AuthorizationError(
                "Quarantine rejection requires an authenticated user principal."
            )
        resolved_request_id = self._request_id(request_id)
        now = self._now()
        clean_reason = validate_reason(reason)

        with self.session.begin_nested():
            quarantine_row = self.session.get(QuarantineRow, quarantine_row_id)
            if quarantine_row is None:
                raise NotFound(f"Quarantine row {quarantine_row_id} was not found.")
            if quarantine_row.resolved_at is not None:
                raise Conflict("This quarantine row has already been resolved.")
            quarantine_row.resolved_at = now
            quarantine_row.resolved_by_id = principal.id
            quarantine_row.resolution = f"rejected: {clean_reason}"
            quarantine_row.updated_at = now
            quarantine_row.updated_by_id = principal.id
            quarantine_row.request_id = resolved_request_id
            self.session.flush()

            self.audit.record(
                AuditEventType.STATEMENT_QUARANTINE_ROW_REJECTED.value,
                ("quarantine_row", quarantine_row.id),
                {
                    "quarantine_row_id": str(quarantine_row.id),
                    "batch_id": str(quarantine_row.batch_id),
                    "row_number": quarantine_row.row_number,
                    "rule_failed": quarantine_row.rule_failed,
                    "reason": clean_reason,
                },
                actor=principal.id,
                request_id=resolved_request_id,
            )
        return quarantine_row

    def list_open_quarantine_rows(
        self,
        principal: Principal,
        *,
        limit: int = 200,
    ) -> tuple[QuarantineRow, ...]:
        """List unresolved quarantine rows for the review screen.

        `quarantine_row` carries no portfolio column of its own — an
        unresolved row may not yet have resolved to any borrower at all
        (an `unknown_borrower` rejection, say) — so this is gated on the
        `RESOLVE_QUARANTINE` permission alone, the same operational,
        bank-wide visibility `plan.md §5.3`'s quarantine review already
        implies; it is not portfolio-scoped the way a borrower read is.
        """
        if not isinstance(principal, Principal):
            raise AuthorizationError("An authenticated principal is required.")
        authorize(principal, Permission.RESOLVE_QUARANTINE)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("Quarantine row list limit must be between 1 and 200.")
        rows = self.session.scalars(
            select(QuarantineRow)
            .where(QuarantineRow.resolved_at.is_(None))
            .order_by(QuarantineRow.created_at, QuarantineRow.id)
            .limit(limit)
        )
        return tuple(rows)

    def list_import_mapping_names(self, principal: Principal) -> tuple[str, ...]:
        """List the mapping names this screen can actually import against.

        The import screen asked the operator to type this name from memory,
        so a typo surfaced as "no active import mapping named ..." rather
        than as a list of what does exist.  Gated on the same permission
        the import itself needs; the mapping name is configuration, not
        borrower data, so no portfolio scope applies.

        Active is necessary but not sufficient: `import_mapping` also holds
        rows whose spec is a provenance label rather than a parseable
        `ImportMappingSpec` (a batch needs a `mapping_id` even when nothing
        was mapped) and rows for non-statement sources.  Offering those
        would only move the failure from a typo to a menu choice, so each
        candidate's spec is parsed and only the usable ones are returned.
        """
        if not isinstance(principal, Principal):
            raise AuthorizationError("An authenticated principal is required.")
        authorize(principal, Permission.INGEST_FINANCIAL_STATEMENTS)
        mappings = self.session.scalars(
            select(ImportMapping)
            .where(ImportMapping.is_active.is_(True))
            .order_by(ImportMapping.name, ImportMapping.version.desc())
        )
        usable: list[str] = []
        for mapping in mappings:
            if mapping.name in usable:
                continue
            try:
                parse_mapping_spec(mapping.spec, chart=self.chart)
            except ValidationError:
                continue
            usable.append(mapping.name)
        return tuple(usable)

    def trace_line_value(
        self,
        principal: Principal,
        statement_line_value_id: UUID,
        *,
        scope: Scope | None = None,
    ) -> ProvenanceTrace:
        """Resolve one stored `StatementLineValue` to its source, row and
        mapping version (`T-026`'s "a provenance query for any stored
        value"). Read-only: no audit event, the same as every other
        scoped read elsewhere in the application."""
        if not isinstance(principal, Principal):
            raise AuthorizationError("An authenticated principal is required.")
        authorize(principal, Permission.VIEW_BORROWER)
        resolved_scope = scope if scope is not None else resolve_scope(principal, self.session)
        if not isinstance(resolved_scope, Scope) or resolved_scope.principal_id != principal.id:
            raise AuthorizationError(
                "The resolved portfolio scope does not belong to the authenticated principal."
            )

        row = self.session.execute(
            select(StatementLineValue, FieldProvenance, ImportBatch, ImportMapping)
            .join(FieldProvenance, FieldProvenance.id == StatementLineValue.provenance_id)
            .join(ImportBatch, ImportBatch.id == FieldProvenance.batch_id)
            .join(ImportMapping, ImportMapping.id == ImportBatch.mapping_id)
            .join(FinancialPeriod, FinancialPeriod.id == StatementLineValue.period_id)
            .join(Borrower, Borrower.id == FinancialPeriod.borrower_id)
            .join(Portfolio, Portfolio.id == Borrower.portfolio_id)
            .where(
                StatementLineValue.id == statement_line_value_id,
                resolved_scope.predicate(Portfolio.path),
            )
        ).first()
        if row is None:
            raise NotFound(
                f"Statement line value {statement_line_value_id} was not found within "
                "the current scope."
            )
        _, provenance, batch, mapping = row
        return ProvenanceTrace(
            source_type=provenance.source_type,
            source_reference=provenance.source_reference,
            row_reference=provenance.row_reference,
            mapping_name=mapping.name,
            mapping_version=provenance.mapping_version,
            batch_id=batch.id,
            ingested_at=provenance.ingested_at,
            transform_note=provenance.transform_note,
        )

    def _dependent_tests(self, period_id: UUID) -> tuple[DependentTest, ...]:
        """Every `CovenantTest` whose inputs came from `period_id`, in
        stable order. `CovenantTest.period_id` is a bare identifier, not a
        foreign key (`db/models/covenant.py`'s own docstring: `financial_
        period` postdates that table), so this is a plain equality filter,
        not a join — and it is a read, never a write: flagging a test here
        never touches the row itself."""
        rows = self.session.execute(
            select(
                CovenantTest.id,
                CovenantTest.covenant_version_id,
                CovenantTest.as_of_date,
                CovenantTest.verdict,
            )
            .where(CovenantTest.period_id == period_id)
            .order_by(CovenantTest.as_of_date, CovenantTest.id)
        ).all()
        return tuple(
            DependentTest(
                covenant_test_id=row[0],
                covenant_version_id=row[1],
                as_of_date=row[2],
                verdict=row[3],
            )
            for row in rows
        )

    def _persist_accepted_row(
        self,
        borrower_id: UUID,
        row: PreparedStatementRow,
        *,
        batch: ImportBatch,
        spec: ImportMappingSpec,
        source_type: str,
        source_reference: str | None,
        mapping_version: int,
        now: datetime,
        actor_id: UUID | None,
        request_id: str,
        version: int = 1,
        row_reference: str | None = None,
        transform_note: str | None = None,
    ) -> FinancialPeriod:
        """Persist one validated row as a `FinancialPeriod` plus its
        `StatementLineValue`s and one `FieldProvenance`, and return the new
        period.

        `version`, `row_reference` and `transform_note` default to the
        plain-import shape (`version=1`, a `row_N` reference, the mapping's
        own unit/currency/sign note); `restate_period` and
        `correct_quarantine_row` override them to record a restatement
        ordinal and a provenance note naming what changed and why, without
        duplicating this method's ~40 lines of row/provenance/line-value
        persistence.
        """
        period = FinancialPeriod(
            id=new_id(),
            borrower_id=borrower_id,
            fy_label=row.resolved.fy_label,
            period_type=row.resolved.period_type,
            period_start=row.resolved.period_start,
            period_end=row.resolved.period_end,
            is_complete=row.normalisation.is_complete,
            is_audited=row.resolved.is_audited,
            version=version,
            superseded_by_id=None,
            source_batch_id=batch.id,
            created_at=now,
            updated_at=now,
            created_by_id=actor_id,
            updated_by_id=actor_id,
            request_id=request_id,
        )
        self.session.add(period)
        self.session.flush()

        provenance = FieldProvenance(
            id=new_id(),
            source_type=source_type,
            source_reference=source_reference,
            row_reference=(row_reference or f"row_{row.row_number}")[:_ROW_REFERENCE_MAX_LENGTH],
            mapping_version=mapping_version,
            ingested_at=now,
            batch_id=batch.id,
            transform_note=transform_note or _transform_note(spec),
            created_at=now,
            updated_at=now,
            created_by_id=actor_id,
            updated_by_id=actor_id,
            request_id=request_id,
        )
        self.session.add(provenance)
        self.session.flush()

        for line_code, value in row.normalisation.lines.items():
            self.session.add(
                StatementLineValue(
                    id=new_id(),
                    period_id=period.id,
                    line_code=line_code,
                    value=value,
                    unit=_UNIT_OUT,
                    currency=spec.currency,
                    provenance_id=provenance.id,
                    created_at=now,
                    updated_at=now,
                    created_by_id=actor_id,
                    updated_by_id=actor_id,
                    request_id=request_id,
                )
            )
        return period

    def _resolve_mapping(self, name: str, version: int | None) -> ImportMapping:
        if not isinstance(name, str) or not name.strip():
            raise ValidationError("mapping_name is required.", field="mapping_name")
        if version is not None:
            mapping = self.session.scalar(
                select(ImportMapping).where(
                    ImportMapping.name == name, ImportMapping.version == version
                )
            )
            if mapping is None:
                raise ValidationError(
                    f"Import mapping {name!r} version {version} does not exist.",
                    field="mapping_version",
                )
            return mapping
        mapping = self.session.scalar(
            select(ImportMapping)
            .where(ImportMapping.name == name, ImportMapping.is_active.is_(True))
            .order_by(ImportMapping.version.desc())
            .limit(1)
        )
        if mapping is None:
            raise ValidationError(
                f"No active import mapping named {name!r} exists.", field="mapping_name"
            )
        return mapping

    def _resolve_borrowers(self, keys: set[str], scope: Scope) -> dict[str, UUID]:
        if not keys:
            return {}
        statement = (
            select(Borrower.reference, Borrower.id)
            .join(Portfolio, Portfolio.id == Borrower.portfolio_id)
            .where(
                Borrower.reference.in_(keys),
                Borrower.is_active.is_(True),
                scope.predicate(Portfolio.path),
            )
        )
        return {
            reference: borrower_id for reference, borrower_id in self.session.execute(statement)
        }

    def _financial_period_exists(self, borrower_id: UUID, fy_label: str) -> bool:
        return self._live_financial_period(borrower_id, fy_label) is not None

    def _live_financial_period(self, borrower_id: UUID, fy_label: str) -> FinancialPeriod | None:
        """The current (non-superseded) version of one borrower's period,
        if any — the row `restate_period` supersedes and the row whose
        existence blocks a plain import or a quarantine correction from
        double-loading the same period (`T-026`)."""
        return self.session.scalar(
            select(FinancialPeriod).where(
                FinancialPeriod.borrower_id == borrower_id,
                FinancialPeriod.fy_label == fy_label,
                FinancialPeriod.superseded_by_id.is_(None),
            )
        )

    def _write_context(
        self,
        principal: Principal,
        scope: Scope | None,
        *,
        permission: Permission = Permission.INGEST_DATA,
    ) -> Scope:
        if not isinstance(principal, Principal):
            raise AuthorizationError("An authenticated principal is required.")
        authorize(principal, permission)
        if scope is None:
            resolved = resolve_scope(principal, self.session)
            if not isinstance(resolved, Scope) or resolved.principal_id != principal.id:
                raise AuthorizationError(
                    "The resolved portfolio scope does not belong to the authenticated principal."
                )
            return resolved
        if not isinstance(scope, Scope) or scope.principal_id != principal.id:
            raise AuthorizationError(
                "The supplied portfolio scope does not belong to the authenticated principal."
            )
        return scope

    def _now(self) -> datetime:
        now = self.clock.now()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Statement import clock must return a timezone-aware datetime.")
        return now.astimezone(UTC)

    def _request_id(self, request_id: str | None) -> str:
        value = request_id or self.request_id
        if not isinstance(value, str) or not 1 <= len(value) <= _REQUEST_ID_MAX_LENGTH:
            raise ValueError(
                "Statement import request_id must be between 1 and "
                f"{_REQUEST_ID_MAX_LENGTH} characters."
            )
        return value

    @staticmethod
    def _actor(principal: Principal) -> UUID | None:
        return principal.id if principal.kind is PrincipalKind.USER else None


def _transform_note(spec: ImportMappingSpec) -> str:
    return f"unit={spec.unit}->{_UNIT_OUT}; currency={spec.currency}; sign={spec.sign}"


def _discrepancy_summary(item: TotalsDiscrepancy) -> DiscrepancySummary:
    return DiscrepancySummary(
        line_code=item.line_code,
        expected=format(item.expected, "f"),
        actual=format(item.actual, "f"),
        difference=format(item.difference, "f"),
    )


def _as_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"Expected an int in a persisted statement import report, got {value!r}.")
    return value


def _as_mapping_list(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise TypeError(
            f"Expected a list of objects in a persisted statement import report, got {value!r}."
        )
    return value


def _json_safe(value: object) -> object:
    """Convert domain values to values accepted by both JSONB and SQLite
    JSON, matching `services/ingestion.py`'s helper of the same name."""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
        return {str(key): _json_safe(nested) for key, nested in value.items()}
    if isinstance(value, tuple | list):
        return [_json_safe(item) for item in value]
    return value


__all__ = [
    "AuditWriter",
    "DiscrepancySummary",
    "QuarantineSummary",
    "StatementImportReport",
    "StatementImportService",
]

# T-026's own public return types are defined in `ingestion/statements/`
# (`DependentTest`, `RestatementResult`, `ProvenanceTrace`) and imported
# above rather than redefined here; this module re-exports them so a
# caller of `StatementImportService` need not reach into that package
# separately for the types its own methods return.
__all__ += ["DependentTest", "ProvenanceTrace", "RestatementResult"]
