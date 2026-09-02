"""Watermarks and recomputation requests for late signal arrivals.

The signal ingestion service deliberately depends on ports in this module
rather than on a particular persistence technology.  A deployment can supply
a database-backed implementation at composition time; the in-memory
implementations are thread-safe and are useful for local runs and offline
evaluation.  Neither implementation silently moves a watermark backwards or
creates duplicate recomputation work.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from threading import RLock
from types import MappingProxyType
from typing import Protocol, runtime_checkable
from uuid import UUID

_MAX_REASON_LENGTH = 500
_MAX_HASH_LENGTH = 128


@runtime_checkable
class WatermarkStore(Protocol):
    """Read and advance the last successfully processed date per source."""

    def get(self, source_id: UUID) -> date | None:
        """Return the current watermark for ``source_id``."""

    def advance(self, source_id: UUID, watermark: date) -> date:
        """Advance a source watermark without allowing regression."""


@dataclass(frozen=True, slots=True)
class RecomputationRequest:
    """One coalesced request to rescore a borrower's affected date range."""

    borrower_id: UUID
    start_date: date
    end_date: date
    reason: str = "late_signal"
    requested_at: datetime | None = None
    source_id: UUID | None = None

    def __post_init__(self) -> None:
        _require_uuid(self.borrower_id, "borrower_id")
        _require_date(self.start_date, "start_date")
        _require_date(self.end_date, "end_date")
        if self.start_date > self.end_date:
            raise ValueError("Recomputation start_date cannot be after end_date.")
        _require_reason(self.reason)
        if self.requested_at is not None:
            _require_aware_datetime(self.requested_at, "requested_at")
            object.__setattr__(self, "requested_at", self.requested_at.astimezone(UTC))
        if self.source_id is not None:
            _require_uuid(self.source_id, "source_id")


@dataclass(frozen=True, slots=True)
class LateArrivalRecord:
    """Auditable outcome of classifying one late event."""

    borrower_id: UUID
    event_date: date
    watermark: date
    event_hash: str
    source_id: UUID
    recomputation_queued: bool
    reason: str
    recorded_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_uuid(self.borrower_id, "borrower_id")
        _require_date(self.event_date, "event_date")
        _require_date(self.watermark, "watermark")
        if self.event_date >= self.watermark:
            raise ValueError("A late arrival must be dated before its watermark.")
        if (
            not isinstance(self.event_hash, str)
            or not 1 <= len(self.event_hash) <= _MAX_HASH_LENGTH
        ):
            raise ValueError(f"event_hash must be between 1 and {_MAX_HASH_LENGTH} characters.")
        _require_uuid(self.source_id, "source_id")
        if not isinstance(self.recomputation_queued, bool):
            raise TypeError("recomputation_queued must be a boolean.")
        _require_reason(self.reason)
        if self.recorded_at is not None:
            _require_aware_datetime(self.recorded_at, "recorded_at")
            object.__setattr__(self, "recorded_at", self.recorded_at.astimezone(UTC))


@runtime_checkable
class RecomputationQueue(Protocol):
    """Queue and retain late-arrival outcomes for the forecast worker."""

    def enqueue(
        self,
        borrower_id: UUID,
        start_date: date,
        end_date: date,
        *,
        reason: str = "late_signal",
        requested_at: datetime | None = None,
        source_id: UUID | None = None,
    ) -> RecomputationRequest:
        """Create or widen one pending request for a borrower."""

    def record_late_arrival(self, record: LateArrivalRecord) -> None:
        """Retain the classification outcome for diagnostics and audit views."""

    def record_no_forecast(self, record: LateArrivalRecord) -> None:
        """Retain a late event that had no forecast to recompute."""


class InMemoryWatermarkStore:
    """Thread-safe monotonic source watermark store.

    ``advance`` is compare-and-set-like: the largest date observed wins, so a
    replayed file cannot make a later successful ingestion appear unprocessed.
    """

    def __init__(self) -> None:
        self._watermarks: dict[UUID, date] = {}
        self._lock = RLock()

    def get(self, source_id: UUID) -> date | None:
        _require_uuid(source_id, "source_id")
        with self._lock:
            return self._watermarks.get(source_id)

    def advance(self, source_id: UUID, watermark: date) -> date:
        _require_uuid(source_id, "source_id")
        _require_date(watermark, "watermark")
        with self._lock:
            current = self._watermarks.get(source_id)
            if current is None or watermark > current:
                self._watermarks[source_id] = watermark
                return watermark
            return current

    def snapshot(self) -> Mapping[UUID, date]:
        """Capture state so an enclosing ingestion operation can roll back."""

        with self._lock:
            return MappingProxyType(dict(self._watermarks))

    def restore(self, snapshot: Mapping[UUID, date]) -> None:
        """Restore a snapshot captured by :meth:`snapshot`."""

        if not isinstance(snapshot, Mapping):
            raise TypeError("Watermark snapshot must be a mapping.")
        restored: dict[UUID, date] = {}
        for source_id, watermark in snapshot.items():
            _require_uuid(source_id, "source_id")
            _require_date(watermark, "watermark")
            restored[source_id] = watermark
        with self._lock:
            self._watermarks = restored

    @property
    def watermarks(self) -> Mapping[UUID, date]:
        """Return an immutable snapshot for diagnostics and tests."""

        with self._lock:
            return MappingProxyType(dict(self._watermarks))

    watermark = get
    current = get


class InMemoryRecomputationQueue:
    """Thread-safe, borrower-keyed recomputation queue.

    Requests are intentionally retained until ``drain`` is called by the
    recomputation worker.  Enqueuing a second request for the same borrower
    widens the existing date range instead of adding another work item.
    """

    def __init__(self) -> None:
        self._requests: dict[UUID, RecomputationRequest] = {}
        self._late_arrivals: list[LateArrivalRecord] = []
        self._no_forecast: list[LateArrivalRecord] = []
        self._lock = RLock()

    def enqueue(
        self,
        borrower_id: UUID,
        start_date: date,
        end_date: date,
        *,
        reason: str = "late_signal",
        requested_at: datetime | None = None,
        source_id: UUID | None = None,
    ) -> RecomputationRequest:
        candidate = RecomputationRequest(
            borrower_id=borrower_id,
            start_date=start_date,
            end_date=end_date,
            reason=reason,
            requested_at=requested_at,
            source_id=source_id,
        )
        with self._lock:
            existing = self._requests.get(candidate.borrower_id)
            if existing is None:
                self._requests[candidate.borrower_id] = candidate
                return candidate
            widened = replace(
                existing,
                start_date=min(existing.start_date, candidate.start_date),
                end_date=max(existing.end_date, candidate.end_date),
            )
            self._requests[candidate.borrower_id] = widened
            return widened

    def record_late_arrival(self, record: LateArrivalRecord) -> None:
        if not isinstance(record, LateArrivalRecord):
            raise TypeError("record_late_arrival requires a LateArrivalRecord.")
        with self._lock:
            self._late_arrivals.append(record)

    def record_no_forecast(self, record: LateArrivalRecord) -> None:
        if not isinstance(record, LateArrivalRecord):
            raise TypeError("record_no_forecast requires a LateArrivalRecord.")
        with self._lock:
            self._no_forecast.append(record)

    @property
    def requests(self) -> tuple[RecomputationRequest, ...]:
        """Return pending requests in deterministic borrower-id order."""

        with self._lock:
            return tuple(self._requests[key] for key in sorted(self._requests, key=str))

    @property
    def recomputation_requests(self) -> tuple[RecomputationRequest, ...]:
        """Readable alias for integrations and operational diagnostics."""

        return self.requests

    @property
    def late_arrivals(self) -> tuple[LateArrivalRecord, ...]:
        with self._lock:
            return tuple(self._late_arrivals)

    @property
    def no_forecast_records(self) -> tuple[LateArrivalRecord, ...]:
        with self._lock:
            return tuple(self._no_forecast)

    def drain(self) -> tuple[RecomputationRequest, ...]:
        """Atomically return and remove all pending recomputation requests."""

        with self._lock:
            result = tuple(self._requests[key] for key in sorted(self._requests, key=str))
            self._requests.clear()
            return result

    def snapshot(
        self,
    ) -> tuple[
        Mapping[UUID, RecomputationRequest],
        tuple[LateArrivalRecord, ...],
        tuple[LateArrivalRecord, ...],
    ]:
        """Capture queue state for an enclosing atomic ingestion operation."""

        with self._lock:
            return (
                MappingProxyType(dict(self._requests)),
                tuple(self._late_arrivals),
                tuple(self._no_forecast),
            )

    def restore(
        self,
        snapshot: tuple[
            Mapping[UUID, RecomputationRequest],
            tuple[LateArrivalRecord, ...],
            tuple[LateArrivalRecord, ...],
        ],
    ) -> None:
        """Restore a snapshot captured by :meth:`snapshot`."""

        if not isinstance(snapshot, tuple) or len(snapshot) != 3:
            raise TypeError("Recomputation queue snapshot has an invalid shape.")
        requests, late_arrivals, no_forecast = snapshot
        if not isinstance(requests, Mapping):
            raise TypeError("Recomputation queue requests snapshot must be a mapping.")
        if not isinstance(late_arrivals, tuple) or not isinstance(no_forecast, tuple):
            raise TypeError("Recomputation queue records snapshot must contain tuples.")
        with self._lock:
            self._requests = dict(requests)
            self._late_arrivals = list(late_arrivals)
            self._no_forecast = list(no_forecast)


def _require_uuid(value: object, field: str) -> None:
    if not isinstance(value, UUID):
        raise TypeError(f"{field} must be a UUID.")


def _require_date(value: object, field: str) -> None:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError(f"{field} must be a calendar date.")


def _require_aware_datetime(value: object, field: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware.")


def _require_reason(value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("A recomputation outcome requires a non-empty reason.")
    if len(value) > _MAX_REASON_LENGTH:
        raise ValueError(f"A recomputation reason must be at most {_MAX_REASON_LENGTH} characters.")


# Explicit aliases keep the port vocabulary discoverable to adapters that use
# "repository" or "request queue" terminology.
SourceWatermarkStore = WatermarkStore
InMemorySourceWatermarkStore = InMemoryWatermarkStore
RecomputationRequestQueue = RecomputationQueue
InMemoryRecomputationRequestQueue = InMemoryRecomputationQueue


__all__ = [
    "InMemoryRecomputationQueue",
    "InMemoryRecomputationRequestQueue",
    "InMemorySourceWatermarkStore",
    "InMemoryWatermarkStore",
    "LateArrivalRecord",
    "RecomputationQueue",
    "RecomputationRequest",
    "RecomputationRequestQueue",
    "SourceWatermarkStore",
    "WatermarkStore",
]
