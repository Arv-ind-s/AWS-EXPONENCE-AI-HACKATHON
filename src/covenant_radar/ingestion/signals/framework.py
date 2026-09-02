"""Source-neutral signal validation and quarantine coordination.

This module stops malformed source data at the edge.  It does not know about
the database or transaction lifecycle; the application service consumes the
prepared batch and performs one atomic persistence operation.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from types import MappingProxyType
from typing import Protocol
from uuid import UUID

from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.errors import DomainError
from covenant_radar.core.ids import new_id
from covenant_radar.domain.signals import SignalEvent

_MAX_BATCH_SIZE = 10_000
_MAX_REASON_LENGTH = 500


@dataclass(frozen=True, slots=True)
class PreparedSignal:
    """A validated event together with its source row position."""

    row_number: int
    event: SignalEvent
    raw: Mapping[str, object] | None

    def __post_init__(self) -> None:
        if isinstance(self.row_number, bool) or self.row_number < 1:
            raise ValueError("Prepared signal row_number must be positive.")
        if not isinstance(self.event, SignalEvent):
            raise TypeError("PreparedSignal.event must be a SignalEvent.")
        if self.raw is not None and not isinstance(self.raw, Mapping):
            raise TypeError("PreparedSignal.raw must be a mapping or None.")


@dataclass(frozen=True, slots=True)
class QuarantinedSignal:
    """Bounded metadata for one signal row held out of ingestion."""

    batch_id: UUID
    row_number: int
    raw: Mapping[str, object] | None
    reason: str
    occurred_at: datetime
    source_id: UUID | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.batch_id, UUID):
            raise TypeError("Quarantined signal batch_id must be a UUID.")
        if (
            isinstance(self.row_number, bool)
            or not isinstance(self.row_number, int)
            or self.row_number < 1
        ):
            raise ValueError("Quarantined signal row_number must be a positive integer.")
        if self.raw is not None and not isinstance(self.raw, Mapping):
            raise TypeError("Quarantined signal raw data must be a mapping or None.")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("A quarantined signal requires a reason.")
        if len(self.reason) > _MAX_REASON_LENGTH:
            raise ValueError("A quarantined signal reason is too long.")
        if not isinstance(self.occurred_at, datetime):
            raise TypeError("Quarantined signal occurred_at must be a datetime.")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("Quarantined signal occurred_at must be timezone-aware.")
        if self.source_id is not None and not isinstance(self.source_id, UUID):
            raise TypeError("Quarantined signal source_id must be a UUID or None.")
        object.__setattr__(self, "occurred_at", self.occurred_at.astimezone(UTC))
        if self.raw is not None:
            object.__setattr__(self, "raw", MappingProxyType(dict(self.raw)))

    @property
    def message(self) -> str:
        """Compatibility/readability alias used by report presenters."""

        return self.reason


class SignalQuarantineSink(Protocol):
    """Destination for metadata of rows rejected by signal ingestion."""

    def quarantine(self, signal: QuarantinedSignal) -> None:
        """Persist or otherwise record a quarantined signal."""


class InMemorySignalQuarantine:
    """Deterministic local sink; production deployments can inject a durable adapter."""

    def __init__(self) -> None:
        self._signals: list[QuarantinedSignal] = []
        self._lock = Lock()

    @property
    def signals(self) -> tuple[QuarantinedSignal, ...]:
        with self._lock:
            return tuple(self._signals)

    @property
    def records(self) -> tuple[QuarantinedSignal, ...]:
        return self.signals

    @property
    def rows(self) -> tuple[QuarantinedSignal, ...]:
        return self.signals

    def quarantine(self, signal: QuarantinedSignal) -> None:
        if not isinstance(signal, QuarantinedSignal):
            raise TypeError("InMemorySignalQuarantine accepts QuarantinedSignal records only.")
        with self._lock:
            self._signals.append(signal)


@dataclass(frozen=True, slots=True)
class SignalBatch:
    """Validated and quarantined outcomes from one fully read source batch."""

    batch_id: UUID
    received_count: int
    prepared: tuple[PreparedSignal, ...]
    quarantined: tuple[QuarantinedSignal, ...]

    @property
    def rejected_count(self) -> int:
        return len(self.quarantined)


@dataclass(frozen=True, slots=True)
class SignalIngestionReport:
    """Reconciled result returned after the database operation succeeds."""

    batch_id: UUID
    received: int
    inserted: int
    duplicates: int
    rejected: int
    quarantined: tuple[QuarantinedSignal, ...] = ()
    source_ids: tuple[UUID, ...] = ()

    def __post_init__(self) -> None:
        counts = (self.received, self.inserted, self.duplicates, self.rejected)
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts
        ):
            raise ValueError("Signal ingestion counts must be non-negative integers.")
        if self.inserted + self.duplicates + self.rejected != self.received:
            raise ValueError("Signal ingestion counts do not reconcile with received rows.")
        if len(self.quarantined) != self.rejected:
            raise ValueError("Each rejected signal must have one quarantine record.")

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
        return len(self.quarantined)

    @property
    def reconciled(self) -> bool:
        return True

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-ready summary without exposing raw quarantined data."""

        return {
            "batch_id": str(self.batch_id),
            "received": self.received,
            "inserted": self.inserted,
            "duplicates": self.duplicates,
            "rejected": self.rejected,
            "quarantined": self.quarantined_count,
            "accepted": self.accepted,
            "reconciled": self.reconciled,
            "source_ids": [str(source_id) for source_id in self.source_ids],
        }


class SignalIngestionFramework:
    """Read and validate one source batch without performing persistence."""

    def __init__(self, *, clock: Clock | None = None) -> None:
        if clock is not None and not callable(getattr(clock, "now", None)):
            raise TypeError("SignalIngestionFramework clock must expose now().")
        self.clock = clock or SystemClock()

    def prepare(
        self,
        events: Iterable[SignalEvent | Mapping[str, object]],
        *,
        batch_id: UUID | None = None,
        source_id: UUID | None = None,
        occurred_at: datetime | None = None,
    ) -> SignalBatch:
        """Consume and validate a source exactly once.

        Validation failures become quarantine records and do not stop later
        rows.  An exception raised by the source iterator itself is allowed to
        propagate so the caller's transaction can roll back the whole batch.
        """

        if not isinstance(events, Iterable):
            raise TypeError("Signal events must be an iterable of event objects.")
        resolved_batch_id = batch_id or new_id()
        if not isinstance(resolved_batch_id, UUID):
            raise TypeError("Signal batch_id must be a UUID.")
        resolved_time = occurred_at or self.clock.now()
        _ensure_aware(resolved_time)
        prepared: list[PreparedSignal] = []
        quarantined: list[QuarantinedSignal] = []
        received = 0
        for row_number, raw in enumerate(events, start=1):
            received += 1
            if received > _MAX_BATCH_SIZE:
                raise ValueError(f"A signal batch cannot contain more than {_MAX_BATCH_SIZE} rows.")
            try:
                event = (
                    raw
                    if isinstance(raw, SignalEvent)
                    else SignalEvent.from_mapping(raw, source_id=source_id)
                )
                if source_id is not None and event.source_id is None:
                    event = SignalEvent(
                        borrower_id=event.borrower_id,
                        facility_id=event.facility_id,
                        event_date=event.event_date,
                        family=event.family,
                        event_type=event.event_type,
                        magnitude=event.magnitude,
                        unit=event.unit,
                        payload=event.payload,
                        source_id=source_id,
                        is_late=event.is_late,
                    )
            except (DomainError, TypeError, ValueError) as error:
                quarantined.append(
                    QuarantinedSignal(
                        batch_id=resolved_batch_id,
                        row_number=row_number,
                        raw=_raw_mapping(raw),
                        reason=str(error)[:_MAX_REASON_LENGTH],
                        occurred_at=resolved_time,
                        source_id=source_id,
                    )
                )
                continue
            prepared.append(
                PreparedSignal(row_number=row_number, event=event, raw=_raw_mapping(raw))
            )
        return SignalBatch(
            batch_id=resolved_batch_id,
            received_count=received,
            prepared=tuple(prepared),
            quarantined=tuple(quarantined),
        )

    def validate(
        self,
        event: SignalEvent | Mapping[str, object],
        *,
        source_id: UUID | None = None,
    ) -> SignalEvent:
        """Validate one row for source adapters that stream one-at-a-time."""

        if isinstance(event, SignalEvent):
            return event
        return SignalEvent.from_mapping(event, source_id=source_id)

    normalise = validate


def _ensure_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Signal ingestion timestamps must be timezone-aware.")


def _raw_mapping(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    return MappingProxyType(dict(value))


__all__ = [
    "InMemorySignalQuarantine",
    "PreparedSignal",
    "QuarantinedSignal",
    "SignalBatch",
    "SignalIngestionFramework",
    "SignalIngestionReport",
    "SignalQuarantineSink",
]
