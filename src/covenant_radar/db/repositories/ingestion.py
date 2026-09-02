"""Durable reporting and quarantine operations for signal ingestion.

Signal ingestion and statement import share the ``import_batch`` and
``quarantine_row`` tables.  This adapter deliberately keeps the signal
report in the batch's JSON snapshot while storing each rejected row
separately for steward action.  Resolving a row therefore never rewrites the
report that describes the original run.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from covenant_radar.core.errors import Conflict, NotFound, ValidationError
from covenant_radar.core.ids import new_id
from covenant_radar.db.models.statements import ImportBatch, ImportMapping, QuarantineRow
from covenant_radar.db.session import is_database_session
from covenant_radar.ingestion.signals.framework import QuarantinedSignal
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal, authorize

_SIGNAL_MAPPING_NAME_PREFIX = "signal_ingestion_"
_SIGNAL_MAPPING_VERSION = 1
_SIGNAL_MAPPING_SPEC: dict[str, object] = {
    "kind": "canonical_signal_event",
    "version": _SIGNAL_MAPPING_VERSION,
}
_SOURCE_TYPES = frozenset({"api", "csv", "json", "xlsx"})
_SOURCE_REFERENCE_MAX_LENGTH = 500
_REQUEST_ID_MAX_LENGTH = 40
_RULE_MAX_LENGTH = 100
_RESOLUTION_MAX_LENGTH = 1000


class SignalIngestionRepository:
    """Persist signal ingestion runs and expose operational read models.

    The repository never commits.  Callers own the surrounding transaction,
    which lets signal rows, quarantine rows and the report succeed or fail as
    one unit.
    """

    def __init__(self, session: Session) -> None:
        if not is_database_session(session):
            raise TypeError("SignalIngestionRepository requires a SQLAlchemy Session.")
        self.session = session

    def begin_run(
        self,
        *,
        batch_id: UUID,
        source_type: str,
        source_reference: str | None,
        started_at: datetime,
        actor_id: UUID | None,
        request_id: str,
    ) -> ImportBatch:
        """Create or return a signal run envelope.

        A caller retrying with the same batch id receives the existing
        envelope.  An incomplete envelope is refused rather than being
        overwritten, because silently replacing a partially written run
        would destroy the evidence needed to diagnose it.
        """

        _uuid(batch_id, "batch_id")
        source_kind = _source_type(source_type)
        reference = _optional_text(
            source_reference, "source_reference", _SOURCE_REFERENCE_MAX_LENGTH
        )
        timestamp = _aware(started_at, "started_at")
        _optional_uuid(actor_id, "actor_id")
        _request_id(request_id)

        existing = self.session.get(ImportBatch, batch_id)
        if existing is not None:
            if not self._is_signal_batch(existing):
                raise Conflict(f"Batch id {batch_id} is already used by another import type.")
            if not existing.report:
                raise Conflict(f"Signal ingestion batch {batch_id} is already in progress.")
            return existing

        mapping = self._canonical_mapping(
            source_type=source_kind,
            started_at=timestamp,
            actor_id=actor_id,
            request_id=request_id,
        )
        batch = ImportBatch(
            id=batch_id,
            source_type=source_kind,
            source_reference=reference,
            mapping_id=mapping.id,
            content_hash=_run_content_hash(batch_id),
            started_at=timestamp,
            finished_at=None,
            row_count=0,
            accepted_count=0,
            quarantined_count=0,
            state="completed",
            report={},
            created_at=timestamp,
            updated_at=timestamp,
            created_by_id=actor_id,
            updated_by_id=actor_id,
            request_id=request_id,
        )
        self.session.add(batch)
        self.session.flush()
        return batch

    def add_quarantine(
        self,
        signal: QuarantinedSignal,
        *,
        actor_id: UUID | None,
        request_id: str,
    ) -> QuarantineRow:
        """Store one rejected signal row without exposing raw data in logs."""

        if not isinstance(signal, QuarantinedSignal):
            raise TypeError("add_quarantine requires a QuarantinedSignal.")
        _optional_uuid(actor_id, "actor_id")
        _request_id(request_id)
        row = QuarantineRow(
            id=new_id(),
            batch_id=signal.batch_id,
            row_number=signal.row_number,
            raw=_json_safe(signal.raw),
            rule_failed=_rule_failed(signal.reason),
            message=signal.reason,
            resolved_at=None,
            resolved_by_id=None,
            resolution=None,
            created_at=signal.occurred_at,
            updated_at=signal.occurred_at,
            created_by_id=actor_id,
            updated_by_id=actor_id,
            request_id=request_id,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def complete_run(
        self,
        batch_id: UUID,
        report: Mapping[str, object],
        *,
        finished_at: datetime,
        actor_id: UUID | None,
        request_id: str,
    ) -> ImportBatch:
        """Persist the immutable report snapshot for a completed run."""

        _uuid(batch_id, "batch_id")
        if not isinstance(report, Mapping) or not report:
            raise ValueError("A signal ingestion report must be a non-empty mapping.")
        timestamp = _aware(finished_at, "finished_at")
        _optional_uuid(actor_id, "actor_id")
        _request_id(request_id)
        batch = self.session.get(ImportBatch, batch_id)
        if batch is None or not self._is_signal_batch(batch):
            raise NotFound(f"Signal ingestion batch {batch_id} was not found.")
        payload = _json_safe(report)
        if not isinstance(payload, dict):
            raise TypeError("A signal ingestion report must serialise to a JSON object.")
        if batch.report:
            if batch.report != payload:
                raise Conflict(f"Signal ingestion batch {batch_id} already has a report.")
            return batch

        batch.row_count = _count(payload, "received")
        batch.accepted_count = _count(payload, "accepted")
        batch.quarantined_count = _count(payload, "rejected")
        batch.finished_at = timestamp
        batch.state = "completed"
        batch.report = payload
        batch.updated_at = timestamp
        batch.updated_by_id = actor_id
        batch.request_id = request_id
        self.session.flush()
        return batch

    def get_report(self, batch_id: UUID) -> Mapping[str, object] | None:
        """Return a defensive copy of one persisted signal report."""

        _uuid(batch_id, "batch_id")
        batch = self.session.get(ImportBatch, batch_id)
        if batch is None or not self._is_signal_batch(batch) or not batch.report:
            return None
        return _copy_json_mapping(batch.report)

    def resolve_quarantine(
        self,
        principal: Principal,
        quarantine_row_id: UUID,
        *,
        reason: str,
        resolved_at: datetime,
        request_id: str,
    ) -> QuarantineRow:
        """Resolve one signal quarantine row while retaining its original data."""

        if not isinstance(principal, Principal):
            raise ValidationError("An authenticated data steward is required.")
        authorize(principal, Permission.RESOLVE_QUARANTINE)
        _uuid(quarantine_row_id, "quarantine_row_id")
        clean_reason = _reason(reason)
        timestamp = _aware(resolved_at, "resolved_at")
        _request_id(request_id)

        with self.session.begin_nested():
            row = self.session.get(QuarantineRow, quarantine_row_id)
            if row is None or not self._is_signal_batch_id(row.batch_id):
                raise NotFound(f"Signal quarantine row {quarantine_row_id} was not found.")
            if row.resolved_at is not None:
                raise Conflict("This signal quarantine row has already been resolved.")
            row.resolved_at = timestamp
            row.resolved_by_id = principal.id
            row.resolution = clean_reason
            row.updated_at = timestamp
            row.updated_by_id = principal.id
            row.request_id = request_id
            self.session.flush()
        return row

    def list_open_quarantine_rows(
        self,
        principal: Principal,
        *,
        limit: int = 200,
    ) -> tuple[QuarantineRow, ...]:
        """List signal quarantine rows awaiting steward action."""

        if not isinstance(principal, Principal):
            raise ValidationError("An authenticated data steward is required.")
        authorize(principal, Permission.RESOLVE_QUARANTINE)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("Signal quarantine list limit must be between 1 and 200.")
        batch_ids = self._signal_batch_ids()
        if not batch_ids:
            return ()
        statement: Select[tuple[QuarantineRow]] = select(QuarantineRow).where(
            QuarantineRow.batch_id.in_(batch_ids),
            QuarantineRow.resolved_at.is_(None),
        ).order_by(QuarantineRow.created_at, QuarantineRow.id).limit(limit)
        return tuple(self.session.execute(statement).scalars().all())

    def metrics(self) -> dict[str, int]:
        """Return aggregate signal-ingestion counts for metrics exporters."""

        batches = self._signal_batches()
        totals = {
            "runs": len(batches),
            "received": 0,
            "accepted": 0,
            "inserted": 0,
            "duplicates": 0,
            "rejected": 0,
            "open_quarantine": 0,
            "resolved_quarantine": 0,
        }
        batch_ids: set[UUID] = set()
        for batch in batches:
            batch_ids.add(batch.id)
            report = batch.report
            for key in ("received", "accepted", "inserted", "duplicates", "rejected"):
                totals[key] += _count(report, key)
        if batch_ids:
            rows = self.session.scalars(
                select(QuarantineRow).where(QuarantineRow.batch_id.in_(batch_ids))
            )
            for row in rows:
                key = "resolved_quarantine" if row.resolved_at is not None else "open_quarantine"
                totals[key] += 1
        return totals

    counts = metrics
    metric_counts = metrics

    def _canonical_mapping(
        self,
        *,
        source_type: str,
        started_at: datetime,
        actor_id: UUID | None,
        request_id: str,
    ) -> ImportMapping:
        mapping_name = _mapping_name(source_type)
        mapping = self.session.scalar(
            select(ImportMapping).where(
                ImportMapping.name == mapping_name,
                ImportMapping.version == _SIGNAL_MAPPING_VERSION,
            )
        )
        if mapping is not None:
            if mapping.source_type != source_type or mapping.spec != _SIGNAL_MAPPING_SPEC:
                raise Conflict("The canonical signal import mapping has been changed.")
            return mapping

        mapping = ImportMapping(
            id=new_id(),
            name=mapping_name,
            source_type=source_type,
            version=_SIGNAL_MAPPING_VERSION,
            spec=dict(_SIGNAL_MAPPING_SPEC),
            is_active=True,
            created_at=started_at,
            updated_at=started_at,
            created_by_id=actor_id,
            updated_by_id=actor_id,
            request_id=request_id,
        )
        try:
            with self.session.begin_nested():
                self.session.add(mapping)
                self.session.flush()
        except IntegrityError:
            mapping = self.session.scalar(
                select(ImportMapping).where(
                    ImportMapping.name == mapping_name,
                    ImportMapping.version == _SIGNAL_MAPPING_VERSION,
                )
            )
            if mapping is None:
                raise
        return mapping

    def _is_signal_batch(self, batch: ImportBatch) -> bool:
        mapping = self.session.get(ImportMapping, batch.mapping_id)
        return mapping is not None and mapping.name.startswith(_SIGNAL_MAPPING_NAME_PREFIX)

    def _is_signal_batch_id(self, batch_id: UUID) -> bool:
        batch = self.session.get(ImportBatch, batch_id)
        return batch is not None and self._is_signal_batch(batch)

    def _signal_batches(self) -> tuple[ImportBatch, ...]:
        statement: Select[tuple[ImportBatch]] = (
            select(ImportBatch)
            .join(ImportMapping, ImportMapping.id == ImportBatch.mapping_id)
            .where(ImportMapping.name.startswith(_SIGNAL_MAPPING_NAME_PREFIX))
            .order_by(ImportBatch.started_at, ImportBatch.id)
        )
        return tuple(self.session.execute(statement).scalars().all())

    def _signal_batch_ids(self) -> frozenset[UUID]:
        return frozenset(batch.id for batch in self._signal_batches())


IngestionReportRepository = SignalIngestionRepository
SignalReportRepository = SignalIngestionRepository
IngestionRepository = SignalIngestionRepository


def _run_content_hash(batch_id: UUID) -> str:
    return hashlib.sha256(f"signal-ingestion:{batch_id}".encode("ascii")).hexdigest()


def _mapping_name(source_type: str) -> str:
    return f"{_SIGNAL_MAPPING_NAME_PREFIX}{source_type}"


def _source_type(value: object) -> str:
    if not isinstance(value, str) or value not in _SOURCE_TYPES:
        raise ValidationError("Signal source_type must be one of api, csv, json or xlsx.")
    return value


def _optional_text(value: object, field: str, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a non-empty string when supplied.", field=field)
    if len(value) > maximum:
        raise ValidationError(f"{field} exceeds {maximum} characters.", field=field)
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValidationError(f"{field} contains an invalid control character.", field=field)
    return value


def _request_id(value: object) -> None:
    if not isinstance(value, str) or not 1 <= len(value) <= _REQUEST_ID_MAX_LENGTH:
        raise ValueError(f"request_id must be between 1 and {_REQUEST_ID_MAX_LENGTH} characters.")


def _uuid(value: object, field: str) -> None:
    if not isinstance(value, UUID):
        raise TypeError(f"{field} must be a UUID.")


def _optional_uuid(value: object, field: str) -> None:
    if value is not None:
        _uuid(value, field)


def _aware(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware.")
    return value.astimezone(UTC)


def _reason(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("A quarantine resolution requires a non-empty reason.")
    if len(value) > _RESOLUTION_MAX_LENGTH:
        raise ValidationError(
            f"A quarantine resolution reason must be at most {_RESOLUTION_MAX_LENGTH} characters."
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValidationError(
            "A quarantine resolution reason contains an invalid control character."
        )
    return value.strip()


def _rule_failed(reason: str) -> str:
    lowered = reason.lower()
    if "unknown signal family" in lowered:
        return "unknown_family"
    if "requires event type" in lowered or "unknown event type" in lowered:
        return "unknown_event_type"
    if "missing required field" in lowered:
        return "missing_required_field"
    if "unknown borrower" in lowered:
        return "unknown_borrower"
    if "unknown facility" in lowered:
        return "unknown_facility"
    return "signal_validation_failed"


def _json_safe(value: object) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(nested) for key, nested in value.items()}
    if isinstance(value, tuple | list):
        return [_json_safe(item) for item in value]
    if hasattr(value, "as_dict") and callable(value.as_dict):
        return _json_safe(value.as_dict())
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return _aware(value, "timestamp").isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return value


def _copy_json_mapping(value: Mapping[str, object]) -> dict[str, object]:
    copied = _json_safe(value)
    if not isinstance(copied, dict):
        raise TypeError("Persisted report is not a JSON object.")
    return copied


def _count(data: Mapping[str, object], key: str) -> int:
    value = data.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Persisted report field {key!r} must be a non-negative integer.")
    return value


__all__ = [
    "IngestionReportRepository",
    "IngestionRepository",
    "SignalIngestionRepository",
    "SignalReportRepository",
]
