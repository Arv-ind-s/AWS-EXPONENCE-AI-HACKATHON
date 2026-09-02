"""Application service for atomic signal ingestion.

The service owns the database transaction's write set.  Sources are consumed
and validated before any signal row is inserted; borrower and facility
ownership is checked in bulk against the caller's portfolio scope; valid rows
are inserted with a database-native conflict-ignore operation; and every
rejected row is sent to quarantine.  A source iterator failure or a failed
quarantine/audit write therefore leaves no partial signal batch behind.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Protocol, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from covenant_radar.audit.events import AuditEventType
from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import get_request_id, new_request_id
from covenant_radar.core.errors import AuthorizationError, ExternalServiceError
from covenant_radar.core.ids import new_id
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.covenant import Covenant, CovenantVersion
from covenant_radar.db.models.facility import Facility
from covenant_radar.db.models.forecast import Forecast
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.models.signal import SignalEvent as SignalEventModel
from covenant_radar.db.repositories.ingestion import SignalIngestionRepository
from covenant_radar.db.scoping import Scope, resolve_scope
from covenant_radar.db.session import is_database_session
from covenant_radar.domain.signals import FAMILIES, SignalEvent
from covenant_radar.ingestion.signals.framework import (
    InMemorySignalQuarantine,
    PreparedSignal,
    QuarantinedSignal,
    SignalIngestionFramework,
    SignalQuarantineSink,
)
from covenant_radar.ingestion.signals.watermark import (
    InMemoryRecomputationQueue,
    InMemoryWatermarkStore,
    LateArrivalRecord,
    RecomputationQueue,
    WatermarkStore,
)
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal, PrincipalKind, authorize

_REQUEST_ID_MAX_LENGTH = 40
_QUARANTINE_REASON_MAX_LENGTH = 500
_IDEMPOTENCY_KEY_MAX_LENGTH = 200
_TOP_REJECTION_REASON_LIMIT = 5
_DEFAULT_DOMINANT_REASON_SHARE = Decimal("0.50")
# SQLite's portable variable limit is commonly 999.  Signal rows contain
# eighteen bound columns, so a conservative chunk keeps the atomic savepoint
# valid for the local demo as well as small-file imports without changing the
# all-or-nothing semantics of ``ingest``.
_INSERT_BATCH_SIZE = 400


@dataclass(frozen=True, slots=True)
class SignalSourceLag:
    """Freshness measurement for one source represented in a run."""

    source_id: UUID
    stated_as_of_date: date | None
    lag_days: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, UUID):
            raise TypeError("Signal source lag source_id must be a UUID.")
        if isinstance(self.stated_as_of_date, datetime) or (
            self.stated_as_of_date is not None and not isinstance(self.stated_as_of_date, date)
        ):
            raise TypeError("Signal source as-of date must be a calendar date or None.")
        if self.lag_days is not None and (
            isinstance(self.lag_days, bool) or not isinstance(self.lag_days, int)
        ):
            raise TypeError("Signal source lag must be an integer or None.")

    @property
    def as_of_date(self) -> date | None:
        """Short alias for presenters that use the source terminology."""

        return self.stated_as_of_date

    def as_dict(self) -> dict[str, object]:
        return {
            "source_id": str(self.source_id),
            "stated_as_of_date": (
                self.stated_as_of_date.isoformat() if self.stated_as_of_date is not None else None
            ),
            "lag_days": self.lag_days,
        }


@dataclass(frozen=True, slots=True)
class RejectionReasonSummary:
    """An aggregated rejection reason, bounded to keep reports actionable."""

    reason: str
    count: int
    share: Decimal
    dominant: bool

    def __post_init__(self) -> None:
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("A rejection reason summary requires a reason.")
        if isinstance(self.count, bool) or not isinstance(self.count, int) or self.count < 1:
            raise ValueError("A rejection reason summary count must be positive.")
        if not isinstance(self.share, Decimal) or not 0 < self.share <= 1:
            raise ValueError("A rejection reason summary share must be between zero and one.")
        if not isinstance(self.dominant, bool):
            raise TypeError("A rejection reason summary dominant flag must be boolean.")

    def as_dict(self) -> dict[str, object]:
        return {
            "reason": self.reason,
            "count": self.count,
            "share": format(self.share, "f"),
            "dominant": self.dominant,
        }


@dataclass(frozen=True, slots=True)
class SignalIngestionReport:
    """Immutable, reconciled report for one signal ingestion run.

    The full quarantine rows remain available through the quarantine sink and
    repository.  The persisted report carries only a bounded sample and
    aggregated rejection reasons, so a malformed source cannot create an
    unbounded JSON or audit payload.
    """

    batch_id: UUID
    source_type: str
    source_reference: str | None
    generated_at: datetime
    received: int
    inserted: int
    duplicates: int
    rejected: int
    quarantined: tuple[QuarantinedSignal, ...] = field(default=(), compare=False)
    source_ids: tuple[UUID, ...] = ()
    family_volumes: Mapping[str, int] = MappingProxyType({})
    source_lag: tuple[SignalSourceLag, ...] = ()
    top_rejection_reasons: tuple[RejectionReasonSummary, ...] = ()
    dominant_reason_share: Decimal = _DEFAULT_DOMINANT_REASON_SHARE

    def __post_init__(self) -> None:
        if not isinstance(self.batch_id, UUID):
            raise TypeError("Signal ingestion report batch_id must be a UUID.")
        if not isinstance(self.source_type, str) or not self.source_type:
            raise ValueError("Signal ingestion report source_type is required.")
        if self.source_reference is not None and not isinstance(self.source_reference, str):
            raise TypeError("Signal ingestion report source_reference must be a string or None.")
        if not isinstance(self.generated_at, datetime):
            raise TypeError("Signal ingestion report generated_at must be a datetime.")
        if self.generated_at.tzinfo is None or self.generated_at.utcoffset() is None:
            raise ValueError("Signal ingestion report generated_at must be timezone-aware.")
        counts = (self.received, self.inserted, self.duplicates, self.rejected)
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts
        ):
            raise ValueError("Signal ingestion report counts must be non-negative integers.")
        if self.inserted + self.duplicates + self.rejected != self.received:
            raise ValueError("Signal ingestion report counts do not reconcile with received rows.")
        if len(self.quarantined) > self.rejected:
            raise ValueError("Signal ingestion report has more quarantine samples than rejects.")
        if not isinstance(self.family_volumes, Mapping):
            raise TypeError("Signal ingestion report family_volumes must be a mapping.")
        for family, count in self.family_volumes.items():
            if family not in FAMILIES:
                raise ValueError(f"Unknown signal family in report: {family!r}.")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError("Signal family volumes must be non-negative integers.")
        if not isinstance(self.dominant_reason_share, Decimal) or not (
            0 < self.dominant_reason_share <= 1
        ):
            raise ValueError("The dominant rejection share must be between zero and one.")
        object.__setattr__(self, "generated_at", self.generated_at.astimezone(UTC))
        object.__setattr__(self, "family_volumes", MappingProxyType(dict(self.family_volumes)))

    @property
    def accepted(self) -> int:
        return self.inserted + self.duplicates

    @property
    def inserted_count(self) -> int:
        return self.inserted

    @property
    def duplicate_count(self) -> int:
        return self.duplicates

    @property
    def rejected_count(self) -> int:
        return self.rejected

    @property
    def quarantined_count(self) -> int:
        return self.rejected

    @property
    def reconciled(self) -> bool:
        return True

    @property
    def outcomes(self) -> Mapping[str, int]:
        return MappingProxyType(
            {
                "received": self.received,
                "accepted": self.accepted,
                "inserted": self.inserted,
                "duplicates": self.duplicates,
                "rejected": self.rejected,
            }
        )

    @property
    def per_family(self) -> Mapping[str, int]:
        return self.family_volumes

    @property
    def lag(self) -> tuple[SignalSourceLag, ...]:
        return self.source_lag

    @property
    def rejection_reasons(self) -> tuple[RejectionReasonSummary, ...]:
        return self.top_rejection_reasons

    @property
    def dominant_reason_flagged(self) -> bool:
        return any(item.dominant for item in self.top_rejection_reasons)

    @property
    def probable_mapping_error(self) -> bool:
        return self.dominant_reason_flagged

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-safe report with bounded quarantine detail."""

        sample = self.quarantined[:_TOP_REJECTION_REASON_LIMIT]
        return {
            "report_type": "signal_ingestion",
            "report_version": 1,
            "batch_id": str(self.batch_id),
            "source_type": self.source_type,
            "source_reference": self.source_reference,
            "generated_at": self.generated_at.isoformat(),
            "received": self.received,
            "accepted": self.accepted,
            "inserted": self.inserted,
            "duplicates": self.duplicates,
            "rejected": self.rejected,
            "quarantined": self.quarantined_count,
            "reconciled": self.reconciled,
            "source_ids": [str(source_id) for source_id in self.source_ids],
            "outcomes": dict(self.outcomes),
            "family_volumes": dict(self.family_volumes),
            "source_lag": [item.as_dict() for item in self.source_lag],
            "top_rejection_reasons": [
                item.as_dict() for item in self.top_rejection_reasons
            ],
            "dominant_reason_share": format(self.dominant_reason_share, "f"),
            "dominant_reason_flagged": self.dominant_reason_flagged,
            "quarantine": {
                "count": self.rejected,
                "sample": [
                    {
                        "row_number": item.row_number,
                        "reason": item.reason,
                        "source_id": str(item.source_id) if item.source_id else None,
                    }
                    for item in sample
                ],
            },
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> SignalIngestionReport:
        """Reconstruct a persisted report without reading mutable quarantine rows."""

        if not isinstance(data, Mapping):
            raise TypeError("A persisted signal report must be a mapping.")
        generated_at = _report_datetime(data.get("generated_at"))
        source_ids = tuple(
            _report_uuid(value, "source_ids") for value in _report_list(data.get("source_ids", ()))
        )
        family_data = data.get("family_volumes", {})
        if not isinstance(family_data, Mapping):
            raise ValueError("Persisted signal report family_volumes must be a mapping.")
        source_lag = tuple(
            SignalSourceLag(
                source_id=_report_uuid(item.get("source_id"), "source_lag.source_id"),
                stated_as_of_date=_report_date(item.get("stated_as_of_date")),
                lag_days=_report_optional_int(item.get("lag_days"), "source_lag.lag_days"),
            )
            for item in _report_mapping_list(data.get("source_lag", ()), "source_lag")
        )
        reasons = tuple(
            RejectionReasonSummary(
                reason=_report_text(item.get("reason"), "top_rejection_reasons.reason"),
                count=_report_int(item.get("count"), "top_rejection_reasons.count"),
                share=_report_decimal(item.get("share"), "top_rejection_reasons.share"),
                dominant=_report_bool(
                    item.get("dominant", False), "top_rejection_reasons.dominant"
                ),
            )
            for item in _report_mapping_list(
                data.get("top_rejection_reasons", ()), "top_rejection_reasons"
            )
        )
        quarantine_data = data.get("quarantine", {})
        samples = ()
        if isinstance(quarantine_data, Mapping):
            samples = tuple(
                QuarantinedSignal(
                    batch_id=_report_uuid(data.get("batch_id"), "batch_id"),
                    row_number=_report_int(item.get("row_number"), "quarantine.row_number"),
                    raw=None,
                    reason=_report_text(item.get("reason"), "quarantine.reason"),
                    occurred_at=generated_at,
                    source_id=(
                        _report_uuid(item.get("source_id"), "quarantine.source_id")
                        if item.get("source_id") is not None
                        else None
                    ),
                )
                for item in _report_mapping_list(
                    quarantine_data.get("sample", ()), "quarantine.sample"
                )
            )
        return cls(
            batch_id=_report_uuid(data.get("batch_id"), "batch_id"),
            source_type=_report_text(data.get("source_type", "api"), "source_type"),
            source_reference=(
                _report_text(data["source_reference"], "source_reference")
                if data.get("source_reference") is not None
                else None
            ),
            generated_at=generated_at,
            received=_report_int(data.get("received"), "received"),
            inserted=_report_int(data.get("inserted"), "inserted"),
            duplicates=_report_int(data.get("duplicates"), "duplicates"),
            rejected=_report_int(data.get("rejected"), "rejected"),
            quarantined=samples,
            source_ids=source_ids,
            family_volumes={
                _report_text(key, "family_volumes.key"): _report_int(value, "family_volumes.value")
                for key, value in family_data.items()
            },
            source_lag=source_lag,
            top_rejection_reasons=reasons,
            dominant_reason_share=_report_decimal(
                data.get("dominant_reason_share", str(_DEFAULT_DOMINANT_REASON_SHARE)),
                "dominant_reason_share",
            ),
        )


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


class SignalIngestionService:
    """Validate, scope-check, quarantine, and persist signal batches."""

    def __init__(
        self,
        session: Session,
        *,
        audit: AuditWriter,
        clock: Clock | None = None,
        request_id: str | None = None,
        quarantine: SignalQuarantineSink | None = None,
        framework: SignalIngestionFramework | None = None,
        scope_resolver: object | None = None,
        watermark_store: WatermarkStore | None = None,
        recomputation_queue: RecomputationQueue | None = None,
        forecast_exists: Callable[[UUID], bool] | None = None,
        report_repository: SignalIngestionRepository | None = None,
        dominant_reason_share: Decimal = _DEFAULT_DOMINANT_REASON_SHARE,
    ) -> None:
        if not is_database_session(session):
            raise TypeError("SignalIngestionService requires a SQLAlchemy Session.")
        if audit is None or not callable(getattr(audit, "record", None)):
            raise TypeError("SignalIngestionService requires an append-only audit writer.")
        if clock is not None and not callable(getattr(clock, "now", None)):
            raise TypeError("SignalIngestionService clock must expose now().")
        if quarantine is not None and not callable(getattr(quarantine, "quarantine", None)):
            raise TypeError("SignalIngestionService quarantine must expose quarantine().")
        if framework is not None and not callable(getattr(framework, "prepare", None)):
            raise TypeError("SignalIngestionService framework must expose prepare().")
        if scope_resolver is not None and not callable(scope_resolver):
            raise TypeError("SignalIngestionService scope_resolver must be callable.")
        if watermark_store is not None and not isinstance(watermark_store, WatermarkStore):
            raise TypeError(
                "SignalIngestionService watermark_store must expose get() and advance()."
            )
        if recomputation_queue is not None and not isinstance(
            recomputation_queue, RecomputationQueue
        ):
            raise TypeError(
                "SignalIngestionService recomputation_queue must expose enqueue() "
                "and record methods."
            )
        if forecast_exists is not None and not callable(forecast_exists):
            raise TypeError("SignalIngestionService forecast_exists must be callable.")
        if report_repository is not None and not isinstance(
            report_repository, SignalIngestionRepository
        ):
            raise TypeError(
                "SignalIngestionService report_repository must be a SignalIngestionRepository."
            )
        if not isinstance(dominant_reason_share, Decimal) or not 0 < dominant_reason_share <= 1:
            raise ValueError("dominant_reason_share must be a Decimal between zero and one.")
        self.session = session
        self.audit = audit
        self.clock = clock or SystemClock()
        self.request_id = request_id or get_request_id() or new_request_id()
        if (
            not isinstance(self.request_id, str)
            or not 1 <= len(self.request_id) <= _REQUEST_ID_MAX_LENGTH
        ):
            raise ValueError(
                "Signal ingestion request_id must be between 1 and "
                f"{_REQUEST_ID_MAX_LENGTH} characters."
            )
        self.quarantine = quarantine or InMemorySignalQuarantine()
        self.framework = framework or SignalIngestionFramework(clock=self.clock)
        self.scope_resolver = cast(object, scope_resolver)
        self.watermark_store = watermark_store or InMemoryWatermarkStore()
        self.recomputation_queue = recomputation_queue or InMemoryRecomputationQueue()
        self.forecast_exists = forecast_exists
        self.report_repository = report_repository or SignalIngestionRepository(session)
        self.dominant_reason_share = dominant_reason_share

    def ingest(
        self,
        principal: Principal,
        events: Iterable[SignalEvent | Mapping[str, object]],
        *,
        scope: Scope | None = None,
        source_id: UUID | None = None,
        batch_id: UUID | None = None,
        request_id: str | None = None,
        idempotency_key: str | None = None,
        source_type: str = "api",
        source_reference: str | None = None,
        source_as_of_date: date | None = None,
        source_as_of_dates: Mapping[UUID, date] | None = None,
        source_as_of: date | None = None,
    ) -> SignalIngestionReport:
        """Ingest one complete source batch.

        The method deliberately does not commit.  The caller's unit of work
        remains the outer transaction boundary, while the savepoint protects
        this batch from leaving partial writes if a sink or audit adapter
        fails after the insert statement has started.
        """

        resolved_scope = self._write_context(principal, scope)
        resolved_request_id = self._request_id(request_id)
        resolved_idempotency_key = self._idempotency_key(idempotency_key)
        now = self._now()
        resolved_source_type = self._source_type(source_type)
        resolved_source_reference = self._source_reference(source_reference)
        resolved_source_as_of_date = self._source_as_of_date(source_as_of_date)
        if source_as_of is not None:
            alias_as_of_date = self._source_as_of_date(source_as_of)
            if (
                resolved_source_as_of_date is not None
                and resolved_source_as_of_date != alias_as_of_date
            ):
                raise ValueError(
                    "source_as_of and source_as_of_date must match when both are supplied."
                )
            resolved_source_as_of_date = alias_as_of_date
        resolved_source_as_of_dates = self._source_as_of_dates(source_as_of_dates)
        if source_id is not None and not isinstance(source_id, UUID):
            raise TypeError("Signal source_id must be a UUID or None.")
        if batch_id is not None and not isinstance(batch_id, UUID):
            raise TypeError("Signal batch_id must be a UUID or None.")

        # SQLAlchemy's SAVEPOINT makes this use case atomic even when a caller
        # invokes the service with an already-open Session instead of the
        # normal UnitOfWork adapter.  No rows are staged before `prepare`, so
        # an exception from a source iterator cannot leave partial data.
        with self.session.begin_nested():
            batch = self.framework.prepare(
                events,
                batch_id=batch_id,
                source_id=source_id,
                occurred_at=now,
            )
            persisted_batch = self.report_repository.begin_run(
                batch_id=batch.batch_id,
                source_type=resolved_source_type,
                source_reference=resolved_source_reference,
                started_at=now,
                actor_id=self._actor(principal),
                request_id=resolved_request_id,
            )
            if persisted_batch.report:
                return SignalIngestionReport.from_dict(persisted_batch.report)
            prepared, current_watermarks = self._classify_late_events(batch.prepared)
            rejected = list(batch.quarantined)
            in_scope = self._in_scope_borrowers(
                {prepared_signal.event.borrower_id for prepared_signal in prepared}, resolved_scope
            )
            facilities = self._in_scope_facilities(
                {
                    prepared_signal.event.facility_id
                    for prepared_signal in prepared
                    if prepared_signal.event.facility_id is not None
                },
                resolved_scope,
            )
            valid: list[PreparedSignal] = []
            for prepared_signal in prepared:
                event = prepared_signal.event
                if event.borrower_id not in in_scope:
                    rejected.append(
                        self._quarantine_record(
                            batch.batch_id,
                            prepared_signal,
                            "Unknown borrower or borrower outside the current portfolio scope.",
                            now,
                            source_id,
                        )
                    )
                    continue
                if event.facility_id is not None:
                    facility_borrower_id = facilities.get(event.facility_id)
                    if facility_borrower_id != event.borrower_id:
                        rejected.append(
                            self._quarantine_record(
                                batch.batch_id,
                                prepared_signal,
                                "Unknown facility or facility outside the current portfolio scope.",
                                now,
                                source_id,
                            )
                        )
                        continue
                valid.append(prepared_signal)

            inserted, duplicates = self._insert_valid(valid, principal, resolved_request_id, now)
            quarantine_records = tuple(rejected)
            for record in quarantine_records:
                self.quarantine.quarantine(record)
                self.report_repository.add_quarantine(
                    record,
                    actor_id=self._actor(principal),
                    request_id=resolved_request_id,
                )
                self.audit.record(
                    AuditEventType.SIGNAL_EVENT_QUARANTINED.value,
                    ("signal_batch", record.batch_id),
                    {
                        "row_number": record.row_number,
                        "reason": record.reason,
                        "source_id": str(record.source_id) if record.source_id else None,
                    },
                    actor=self._actor(principal),
                    request_id=resolved_request_id,
                )

            watermark_snapshot = self._snapshot_state(self.watermark_store)
            queue_snapshot = self._snapshot_state(self.recomputation_queue)
            try:
                late_records, recomputations, no_forecast_count = self._handle_late_events(
                    valid,
                    current_watermarks=current_watermarks,
                    requested_at=now,
                )
                for source, watermark in self._watermark_advancements(valid).items():
                    self.watermark_store.advance(source, watermark)
            except Exception:
                self._restore_state(self.watermark_store, watermark_snapshot)
                self._restore_state(self.recomputation_queue, queue_snapshot)
                raise

            reported_source_ids: set[UUID] = set()
            if source_id is not None:
                reported_source_ids.add(source_id)
            reported_source_ids.update(
                record.source_id for record in quarantine_records if record.source_id is not None
            )
            reported_source_ids.update(
                prepared.event.source_id
                for prepared in valid
                if prepared.event.source_id is not None
            )
            source_ids = tuple(sorted(reported_source_ids, key=str))
            report = self._build_report(
                batch_id=batch.batch_id,
                received=batch.received_count,
                inserted=inserted,
                duplicates=duplicates,
                quarantine_records=quarantine_records,
                source_ids=source_ids,
                prepared=valid,
                source_type=resolved_source_type,
                source_reference=resolved_source_reference,
                generated_at=now,
                source_as_of_date=resolved_source_as_of_date,
                source_as_of_dates=resolved_source_as_of_dates,
            )
            try:
                self.report_repository.complete_run(
                    report.batch_id,
                    report.as_dict(),
                    finished_at=now,
                    actor_id=self._actor(principal),
                    request_id=resolved_request_id,
                )
                self.audit.record(
                    AuditEventType.SIGNAL_INGESTION_COMPLETED.value,
                    ("signal_batch", report.batch_id),
                    {
                        "received": report.received,
                        "inserted": report.inserted,
                        "duplicates": report.duplicates,
                        "rejected": report.rejected,
                        "source_ids": [str(value) for value in report.source_ids],
                        "idempotency_key": resolved_idempotency_key,
                        "late_events": len(late_records),
                        "recomputations_queued": recomputations,
                        "late_events_without_forecast": no_forecast_count,
                        "reconciled": report.reconciled,
                        "dominant_reason_flagged": report.dominant_reason_flagged,
                    },
                    actor=self._actor(principal),
                    request_id=resolved_request_id,
                )
            except Exception:
                self._restore_state(self.watermark_store, watermark_snapshot)
                self._restore_state(self.recomputation_queue, queue_snapshot)
                raise
            return report

    ingest_signals = ingest
    ingest_batch = ingest

    def _build_report(
        self,
        *,
        batch_id: UUID,
        received: int,
        inserted: int,
        duplicates: int,
        quarantine_records: Sequence[QuarantinedSignal],
        source_ids: tuple[UUID, ...],
        prepared: Sequence[PreparedSignal],
        source_type: str,
        source_reference: str | None,
        generated_at: datetime,
        source_as_of_date: date | None,
        source_as_of_dates: Mapping[UUID, date],
    ) -> SignalIngestionReport:
        family_volumes = {family: 0 for family in FAMILIES}
        for candidate in prepared:
            family_volumes[candidate.event.family] += 1
        for record in quarantine_records:
            if record.raw is not None:
                family = record.raw.get("family")
                if isinstance(family, str) and family in family_volumes:
                    family_volumes[family] += 1

        rejection_counts = Counter(_rejection_key(record.reason) for record in quarantine_records)
        top_rejection_reasons = tuple(
            RejectionReasonSummary(
                reason=reason,
                count=count,
                share=Decimal(count) / Decimal(received) if received else Decimal("0"),
                dominant=(
                    Decimal(count) / Decimal(received) > self.dominant_reason_share
                    if received
                    else False
                ),
            )
            for reason, count in sorted(
                rejection_counts.items(), key=lambda item: (-item[1], item[0])
            )[:_TOP_REJECTION_REASON_LIMIT]
        )
        source_lag = tuple(
            SignalSourceLag(
                source_id=source_id,
                stated_as_of_date=source_as_of_dates.get(source_id, source_as_of_date),
                lag_days=(
                    (
                        generated_at.date()
                        - source_as_of_dates.get(source_id, source_as_of_date)
                    ).days
                    if source_as_of_dates.get(source_id, source_as_of_date) is not None
                    else None
                ),
            )
            for source_id in source_ids
        )
        return SignalIngestionReport(
            batch_id=batch_id,
            source_type=source_type,
            source_reference=source_reference,
            generated_at=generated_at,
            received=received,
            inserted=inserted,
            duplicates=duplicates,
            rejected=len(quarantine_records),
            quarantined=tuple(quarantine_records),
            source_ids=source_ids,
            family_volumes=family_volumes,
            source_lag=source_lag,
            top_rejection_reasons=top_rejection_reasons,
            dominant_reason_share=self.dominant_reason_share,
        )

    @staticmethod
    def _source_type(value: object) -> str:
        if not isinstance(value, str) or value not in {"api", "csv", "json", "xlsx"}:
            raise ValueError("Signal source_type must be one of api, csv, json or xlsx.")
        return value

    @staticmethod
    def _source_reference(value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Signal source_reference must be a non-empty string when supplied.")
        if len(value) > 500:
            raise ValueError("Signal source_reference must be at most 500 characters.")
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("Signal source_reference contains an invalid control character.")
        return value

    @staticmethod
    def _source_as_of_date(value: object) -> date | None:
        if value is None:
            return None
        if isinstance(value, datetime) or not isinstance(value, date):
            raise TypeError("Signal source as-of date must be a calendar date or None.")
        return value

    @classmethod
    def _source_as_of_dates(cls, value: Mapping[UUID, date] | None) -> Mapping[UUID, date]:
        if value is None:
            return MappingProxyType({})
        if not isinstance(value, Mapping):
            raise TypeError("Signal source_as_of_dates must be a mapping.")
        result: dict[UUID, date] = {}
        for source_id, as_of_date in value.items():
            if not isinstance(source_id, UUID):
                raise TypeError("Signal source_as_of_dates keys must be UUIDs.")
            resolved_date = cls._source_as_of_date(as_of_date)
            if resolved_date is None:
                raise TypeError("Signal source_as_of_dates values must be calendar dates.")
            result[source_id] = resolved_date
        return MappingProxyType(result)

    def _classify_late_events(
        self,
        prepared: Sequence[PreparedSignal],
    ) -> tuple[tuple[PreparedSignal, ...], dict[UUID, date | None]]:
        """Classify a batch against a stable snapshot of each source watermark.

        The watermark is intentionally not advanced while rows are being
        inspected.  In a batch containing both old and new dates, every row is
        compared with the watermark that existed before the batch started;
        the largest accepted event date is applied only after persistence.
        """

        current_watermarks: dict[UUID, date | None] = {}
        classified: list[PreparedSignal] = []
        for candidate in prepared:
            event = candidate.event
            event_source_id = event.source_id
            if event_source_id is None:
                # Source-less API callers predate watermarking.  They remain
                # valid ingestion events but cannot be compared to a source
                # watermark that does not exist.
                classified.append(self._with_late_flag(candidate, is_late=False))
                continue
            if event_source_id not in current_watermarks:
                watermark = self.watermark_store.get(event_source_id)
                if isinstance(watermark, datetime) or not isinstance(watermark, date | None):
                    raise TypeError("A source watermark must be a calendar date or None.")
                current_watermarks[event_source_id] = watermark
            watermark = current_watermarks[event_source_id]
            classified.append(
                self._with_late_flag(
                    candidate,
                    is_late=watermark is not None and event.event_date < watermark,
                )
            )
        return tuple(classified), current_watermarks

    @staticmethod
    def _with_late_flag(candidate: PreparedSignal, *, is_late: bool) -> PreparedSignal:
        """Return a prepared event with the service-owned late flag."""

        event = candidate.event
        if event.is_late == is_late:
            return candidate
        return PreparedSignal(
            row_number=candidate.row_number,
            event=SignalEvent(
                borrower_id=event.borrower_id,
                facility_id=event.facility_id,
                event_date=event.event_date,
                family=event.family,
                event_type=event.event_type,
                magnitude=event.magnitude,
                unit=event.unit,
                payload=event.payload,
                source_id=event.source_id,
                is_late=is_late,
                content_hash=event.content_hash,
            ),
            raw=candidate.raw,
        )

    @staticmethod
    def _snapshot_state(store: object) -> object | None:
        snapshot = getattr(store, "snapshot", None)
        return snapshot() if callable(snapshot) else None

    @staticmethod
    def _restore_state(store: object, snapshot: object | None) -> None:
        if snapshot is None:
            return
        restore = getattr(store, "restore", None)
        if not callable(restore):
            raise RuntimeError("A stateful ingestion store cannot restore its snapshot.")
        restore(snapshot)

    @staticmethod
    def _watermark_advancements(prepared: Sequence[PreparedSignal]) -> dict[UUID, date]:
        """Return the maximum accepted event date for every source in a batch."""

        advancements: dict[UUID, date] = {}
        for candidate in prepared:
            source_id = candidate.event.source_id
            if source_id is None:
                continue
            previous = advancements.get(source_id)
            if previous is None or candidate.event.event_date > previous:
                advancements[source_id] = candidate.event.event_date
        return advancements

    def _handle_late_events(
        self,
        prepared: Sequence[PreparedSignal],
        *,
        current_watermarks: Mapping[UUID, date | None],
        requested_at: datetime,
    ) -> tuple[tuple[LateArrivalRecord, ...], int, int]:
        """Queue affected ranges and record explicit no-forecast outcomes."""

        late_records: list[LateArrivalRecord] = []
        queued_count = 0
        no_forecast_count = 0
        forecast_cache: dict[UUID, bool] = {}
        for candidate in prepared:
            event = candidate.event
            source_id = event.source_id
            if not event.is_late or source_id is None:
                continue
            watermark = current_watermarks.get(source_id)
            if watermark is None:
                raise RuntimeError(
                    "A late signal was classified without a source watermark; ingestion refused."
                )
            has_forecast = forecast_cache.get(event.borrower_id)
            if has_forecast is None:
                has_forecast = self._borrower_has_forecast(event.borrower_id)
                forecast_cache[event.borrower_id] = has_forecast

            if has_forecast:
                self.recomputation_queue.enqueue(
                    event.borrower_id,
                    event.event_date,
                    watermark,
                    reason="late_signal",
                    requested_at=requested_at,
                    source_id=source_id,
                )
                queued_count += 1
                reason = "recomputation queued"
                recomputation_queued = True
            else:
                reason = "no forecast exists for borrower; recomputation not queued"
                recomputation_queued = False
                no_forecast_count += 1

            record = LateArrivalRecord(
                borrower_id=event.borrower_id,
                event_date=event.event_date,
                watermark=watermark,
                event_hash=event.hash,
                source_id=source_id,
                recomputation_queued=recomputation_queued,
                reason=reason,
                recorded_at=requested_at,
            )
            self.recomputation_queue.record_late_arrival(record)
            if not recomputation_queued:
                self.recomputation_queue.record_no_forecast(record)
            late_records.append(record)
        return tuple(late_records), queued_count, no_forecast_count

    def _borrower_has_forecast(self, borrower_id: UUID) -> bool:
        """Return whether a stored forecast exists for ``borrower_id``."""

        if self.forecast_exists is not None:
            result = self.forecast_exists(borrower_id)
            if not isinstance(result, bool):
                raise TypeError("Signal ingestion forecast_exists must return a boolean.")
            return result

        statement = (
            select(Forecast.id)
            .join(CovenantVersion, Forecast.covenant_version_id == CovenantVersion.id)
            .join(Covenant, CovenantVersion.covenant_id == Covenant.id)
            .join(Facility, Covenant.facility_id == Facility.id)
            .where(Facility.borrower_id == borrower_id)
            .limit(1)
        )
        try:
            return self.session.execute(statement).scalar_one_or_none() is not None
        except SQLAlchemyError as error:
            raise ExternalServiceError(
                "Signal late-arrival handling could not inspect borrower forecasts."
            ) from error

    def _insert_valid(
        self,
        prepared: Sequence[PreparedSignal],
        principal: Principal,
        request_id: str,
        now: datetime,
    ) -> tuple[int, int]:
        if not prepared:
            return 0, 0
        unique: dict[str, PreparedSignal] = {}
        duplicate_count = 0
        for candidate in prepared:
            key = candidate.event.hash
            if key in unique:
                duplicate_count += 1
            else:
                unique[key] = candidate

        actor_id = self._actor(principal)
        values = [
            {
                "id": new_id(),
                "borrower_id": prepared_signal.event.borrower_id,
                "facility_id": prepared_signal.event.facility_id,
                "event_date": prepared_signal.event.event_date,
                "family": prepared_signal.event.family,
                "event_type": prepared_signal.event.event_type,
                "magnitude": prepared_signal.event.magnitude,
                "unit": prepared_signal.event.unit,
                "payload": _json_safe(prepared_signal.event.payload),
                "source_id": prepared_signal.event.source_id,
                "content_hash": prepared_signal.event.hash,
                "is_late": prepared_signal.event.is_late,
                "ingested_at": now,
                "created_at": now,
                "updated_at": now,
                "created_by_id": actor_id,
                "updated_by_id": actor_id,
                "request_id": request_id,
            }
            for prepared_signal in unique.values()
        ]
        dialect = self.session.get_bind().dialect.name
        if dialect not in {"postgresql", "sqlite"}:
            raise ExternalServiceError(
                f"Signal ingestion does not support database dialect {dialect!r}."
            )
        inserted = 0
        for start in range(0, len(values), _INSERT_BATCH_SIZE):
            chunk = values[start : start + _INSERT_BATCH_SIZE]
            if dialect == "postgresql":
                statement: Any = (
                    postgresql_insert(SignalEventModel)
                    .values(chunk)
                    .on_conflict_do_nothing(index_elements=[SignalEventModel.content_hash])
                )
            else:
                statement = (
                    sqlite_insert(SignalEventModel)
                    .values(chunk)
                    .on_conflict_do_nothing(index_elements=[SignalEventModel.content_hash])
                )
            try:
                result = self.session.execute(statement)
            except SQLAlchemyError as error:
                raise ExternalServiceError(
                    "Signal batch could not be persisted atomically."
                ) from error
            rowcount = result.rowcount
            if rowcount is None or rowcount < 0:
                raise ExternalServiceError(
                    "Signal database did not return a reliable insert count."
                )
            inserted += int(rowcount)
        duplicate_count += len(values) - inserted
        return inserted, duplicate_count

    @staticmethod
    def _quarantine_record(
        batch_id: UUID,
        prepared: PreparedSignal,
        reason: str,
        occurred_at: datetime,
        source_id: UUID | None,
    ) -> QuarantinedSignal:
        return QuarantinedSignal(
            batch_id=batch_id,
            row_number=prepared.row_number,
            raw=prepared.raw,
            reason=reason[:_QUARANTINE_REASON_MAX_LENGTH],
            occurred_at=occurred_at,
            source_id=prepared.event.source_id or source_id,
        )

    def _in_scope_borrowers(self, borrower_ids: set[UUID], scope: Scope) -> set[UUID]:
        if not borrower_ids:
            return set()
        statement = (
            select(Borrower.id)
            .join(Portfolio, Portfolio.id == Borrower.portfolio_id)
            .where(Borrower.id.in_(borrower_ids), scope.predicate(Portfolio.path))
        )
        return set(self.session.execute(statement).scalars().all())

    def _in_scope_facilities(self, facility_ids: set[UUID], scope: Scope) -> dict[UUID, UUID]:
        if not facility_ids:
            return {}
        statement = (
            select(Facility.id, Facility.borrower_id)
            .join(Borrower, Borrower.id == Facility.borrower_id)
            .join(Portfolio, Portfolio.id == Borrower.portfolio_id)
            .where(Facility.id.in_(facility_ids), scope.predicate(Portfolio.path))
        )
        return {
            facility_id: borrower_id
            for facility_id, borrower_id in self.session.execute(statement).all()
        }

    def _write_context(self, principal: Principal, scope: Scope | None) -> Scope:
        if not isinstance(principal, Principal):
            raise AuthorizationError("An authenticated principal is required.")
        authorize(principal, Permission.INGEST_DATA)
        if scope is None:
            resolver = self.scope_resolver
            resolved = (
                resolver(principal)
                if callable(resolver)
                else resolve_scope(principal, self.session)
            )
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
            raise ValueError("Signal ingestion clock must return a timezone-aware datetime.")
        return now.astimezone(UTC)

    def _request_id(self, request_id: str | None) -> str:
        value = request_id or self.request_id
        if not isinstance(value, str) or not 1 <= len(value) <= _REQUEST_ID_MAX_LENGTH:
            raise ValueError(
                "Signal ingestion request_id must be between 1 and "
                f"{_REQUEST_ID_MAX_LENGTH} characters."
            )
        return value

    @staticmethod
    def _idempotency_key(value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not 1 <= len(value) <= _IDEMPOTENCY_KEY_MAX_LENGTH:
            raise ValueError(
                "Signal ingestion idempotency_key must be between 1 and "
                f"{_IDEMPOTENCY_KEY_MAX_LENGTH} characters."
            )
        if not value.strip():
            raise ValueError("Signal ingestion idempotency_key must not be blank.")
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError(
                "Signal ingestion idempotency_key contains an invalid control character."
            )
        return value

    @staticmethod
    def _actor(principal: Principal) -> UUID | None:
        return principal.id if principal.kind is PrincipalKind.USER else None


def _json_safe(value: object) -> object:
    """Convert domain values to values accepted by both JSONB and SQLite JSON."""

    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
        return {str(key): _json_safe(nested) for key, nested in value.items()}
    if isinstance(value, tuple | list):
        return [_json_safe(item) for item in value]
    return value


def _report_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Persisted report field {field!r} must be a non-empty string.")
    return value


def _report_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Persisted report field {field!r} must be a non-negative integer.")
    return value


def _report_optional_int(value: object, field: str) -> int | None:
    if value is None:
        return None
    return _report_int(value, field)


def _report_bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"Persisted report field {field!r} must be boolean.")
    return value


def _report_decimal(value: object, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (ArithmeticError, ValueError) as error:
        raise ValueError(f"Persisted report field {field!r} must be a decimal.") from error
    if not parsed.is_finite():
        raise ValueError(f"Persisted report field {field!r} must be finite.")
    return parsed


def _report_uuid(value: object, field: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        raise ValueError(f"Persisted report field {field!r} must contain a UUID.")
    try:
        return UUID(value)
    except ValueError as error:
        raise ValueError(f"Persisted report field {field!r} must contain a UUID.") from error


def _report_date(value: object) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Persisted report dates must be ISO date strings.")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("Persisted report dates must be ISO date strings.") from error


def _report_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Persisted signal report generated_at must be an ISO datetime.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("Persisted signal report generated_at must be an ISO datetime.") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Persisted signal report generated_at must be timezone-aware.")
    return parsed.astimezone(UTC)


def _report_list(value: object) -> tuple[object, ...]:
    if isinstance(value, str | bytes | bytearray) or not isinstance(value, Sequence):
        raise ValueError("Persisted report list fields must be sequences.")
    return tuple(value)


def _report_mapping_list(value: object, field: str) -> tuple[Mapping[str, object], ...]:
    items = _report_list(value)
    result: list[Mapping[str, object]] = []
    for item in items:
        if not isinstance(item, Mapping):
            raise ValueError(f"Persisted report field {field!r} must contain objects.")
        result.append(item)
    return tuple(result)


def _rejection_key(reason: str) -> str:
    """Collapse row-specific values into stable operator-facing categories."""

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
    return reason


# The short name is useful to adapters that already use the generic service
# vocabulary; both names intentionally point to the same implementation.
IngestionService = SignalIngestionService

__all__ = [
    "AuditWriter",
    "IngestionService",
    "RejectionReasonSummary",
    "SignalIngestionReport",
    "SignalIngestionService",
    "SignalSourceLag",
]
